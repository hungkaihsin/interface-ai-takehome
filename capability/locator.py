"""How a control on a screen is identified.

Everything is expressed in accessibility-tree terms: a role, an accessible name, and
optionally a container to scope the search. No CSS, no XPath, no coordinates --
those are browser-only or layout-dependent, and this vocabulary is the one a desktop
accessibility API also speaks.

Strategies are tried in order, cleanest first, because real legacy screens contain
both well-labelled and anonymous controls.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

#: Closed set: a bad artifact should fail to load, not fail halfway through a
#: banking action.
Role = Literal[
    "button",
    "textbox",
    "combobox",
    "link",
    "checkbox",
    "radio",
    "cell",
    "row",
    "table",
    "heading",
    "option",
    "alert",
]


class ContainerScope(BaseModel):
    """An ancestor to search inside, identified the same way as anything else."""

    role: Role
    name: str
    #: Substring by default: a row's accessible name is the concatenation of its
    #: cells, so exact matching is unusable here.
    name_match: Literal["exact", "contains"] = "contains"
    #: Which matching container, when several match. Unset asserts there is only
    #: one, and replay fails loudly if that stops being true.
    index: int | None = None


class RoleNameStrategy(BaseModel):
    """Tier 1: role plus accessible name -- `button "Search"`. Preferred."""

    kind: Literal["role_name"] = "role_name"
    role: Role
    name: str
    name_match: Literal["exact", "contains"] = "exact"


class RoleInContainerStrategy(BaseModel):
    """Tier 2: role, scoped to a container that does have a name.

    The sub-account form exposes a bare `textbox` inside `row "Nickname"` -- the
    control is anonymous, its neighbourhood is not. `tr:nth-child(2) input` encodes
    position and silently retargets when a row is inserted above; the row's label
    encodes meaning and survives.
    """

    kind: Literal["role_in_container"] = "role_in_container"
    role: Role
    container: ContainerScope
    #: Which match inside the container. Scoped to the container so it never
    #: becomes a page-wide ordinal. None asserts uniqueness -- defaulting to 0
    #: would silently re-introduce "take the first".
    index: int | None = None


class RoleOrdinalStrategy(BaseModel):
    """Tier 3: role plus position. Last resort, and must justify itself in text a
    reviewer will read."""

    kind: Literal["role_ordinal"] = "role_ordinal"
    role: Role
    index: int
    justification: str


LocatorStrategy = Annotated[
    Union[RoleNameStrategy, RoleInContainerStrategy, RoleOrdinalStrategy],
    Field(discriminator="kind"),
]


class Locator(BaseModel):
    """A control's address: which frame it lives in, then how to find it there.

    `frame_path` is mandatory because the target app's top-level document holds no
    controls at all. A desktop backend reads the same field as a window hierarchy.

    Uniqueness rule: a strategy resolves only if it matches exactly one element.
    Taking the first of several is how automation reads the wrong row the day a
    member opens a second savings account -- silently, with no error. Genuine
    multiplicity must be declared with an explicit index.
    """

    frame_path: list[str] = Field(
        default_factory=list,
        description="Frame names from the top document inward; empty means top.",
    )
    strategies: list[LocatorStrategy] = Field(
        min_length=1,
        description="Tried in order; the first that resolves uniquely wins.",
    )
    #: So a reviewer can read the artifact without opening the app, and failures
    #: can name what they were looking for in human terms.
    describes: str = Field(
        description="Plain-language description, e.g. 'the Member Number field'."
    )
