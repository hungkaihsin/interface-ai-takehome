"""The capability artifact: the typed, versioned recipe a successful run leaves behind.

Discovery produces one of these; deterministic replay, safety and escalation all
consume one.

Shaped as a contract rather than a macro -- inputs, outputs, success condition and
expected business outcomes are top-level, so a calling agent can decide whether to
invoke it without reading a single step. `schema_version` versions the format,
`version` versions the capability. Step values are templates (`{{ member_id }}`),
which is what makes it reusable and keeps concrete member data out of the file.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

from .locator import Locator

SCHEMA_VERSION = "1.0"

#: `{{ name }}` references an input parameter. Resolved at replay time, never stored.
TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# ---------------------------------------------------------------------------
# conditions -- how the system reads the state of a screen
# ---------------------------------------------------------------------------


class TextPresent(BaseModel):
    """Is this text on screen? The workhorse -- legacy apps signal almost everything
    through prose, which is far more stable than the markup around it."""

    kind: Literal["text_present"] = "text_present"
    frame_path: list[str] = Field(default_factory=list)
    text: str
    match: Literal["exact", "contains", "regex"] = "contains"
    #: "any_frame" for conditions that are not frame-stable -- a session timeout
    #: lands in the inner frame or replaces the whole document depending on what
    #: triggered it. Frame-stable conditions stay pinned so a nav panel mentioning
    #: the same words cannot match.
    scope: Literal["frame", "any_frame"] = "frame"


class ElementPresent(BaseModel):
    """Does a control we can address actually resolve on screen?"""

    kind: Literal["element_present"] = "element_present"
    locator: Locator


class UrlMatches(BaseModel):
    """Corroboration only: a URL says where the browser went, not what rendered."""

    kind: Literal["url_matches"] = "url_matches"
    frame_path: list[str] = Field(default_factory=list)
    pattern: str


Condition = Annotated[
    Union[TextPresent, ElementPresent, UrlMatches], Field(discriminator="kind")
]


class Checkpoint(BaseModel):
    """Confirms we arrived, rather than assuming the click worked.

    A click that silently lands on the login screen after a timeout looks exactly
    like a click that worked, right up until you read the page.
    """

    condition: Condition
    description: str
    timeout_ms: int = 5_000


# ---------------------------------------------------------------------------
# typed inputs and outputs -- the agent-facing half of the contract
# ---------------------------------------------------------------------------

ValueType = Literal["string", "number", "integer", "boolean", "money"]


class ParamSpec(BaseModel):
    """One input the calling agent must supply."""

    name: str
    type: ValueType
    description: str
    required: bool = True
    #: Validated before the browser opens -- catching a malformed member number
    #: three screens in costs a session.
    pattern: str | None = None
    example: Any | None = None
    #: Marks an argument as regulated data. Such values are resolved into the live
    #: session but never written to a log line or an evidence file.
    sensitive: bool = False


class Transform(BaseModel):
    """Declarative cleanup for scraped text.

    Declarative rather than a code hook: an artifact is a document a compliance
    reviewer reads, and an embedded lambda would make it an executable.
    """

    kind: Literal["strip", "to_number", "strip_currency", "regex_extract"]
    pattern: str | None = None


class Extraction(BaseModel):
    """Where an output value is read from on screen."""

    locator: Locator
    transform: list[Transform] = Field(default_factory=list)


class OutputSpec(BaseModel):
    """One value the capability returns to its caller."""

    name: str
    type: ValueType
    description: str
    source: Extraction
    required: bool = True
    #: True for anything regulated (SSN, full account number). The value is
    #: returned to the caller in memory but redacted everywhere it is persisted.
    sensitive: bool = False


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


class NavigateAction(BaseModel):
    kind: Literal["navigate"] = "navigate"
    url: str


class ClickAction(BaseModel):
    kind: Literal["click"] = "click"
    target: Locator


class FillAction(BaseModel):
    kind: Literal["fill"] = "fill"
    target: Locator
    #: Template string, e.g. "{{ member_id }}".
    value: str


class SelectAction(BaseModel):
    kind: Literal["select"] = "select"
    target: Locator
    value: str


class PressAction(BaseModel):
    kind: Literal["press"] = "press"
    target: Locator
    key: str


class WaitForAction(BaseModel):
    """Wait for a condition, never a duration.

    There is no sleep action in this schema. Waiting for the record to appear is
    correct whether the stall is 3 seconds or 8.
    """

    kind: Literal["wait_for"] = "wait_for"
    condition: Condition
    timeout_ms: int = 10_000


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        FillAction,
        SelectAction,
        PressAction,
        WaitForAction,
    ],
    Field(discriminator="kind"),
]

#: "safe" means doing it twice is indistinguishable from doing it once. "risky"
#: means it changes state the institution or member can see. The split is about
#: reversibility, because that is what decides whether a retry is safe.
RiskClass = Literal["safe", "risky"]


class Step(BaseModel):
    id: str
    #: Why this step exists. What a reviewer reads and a failure message quotes.
    intent: str
    action: Action
    risk: RiskClass = "safe"
    #: Lets a failure name the step that broke, rather than reporting that the run
    #: ended somewhere unexpected.
    checkpoint: Checkpoint | None = None


# ---------------------------------------------------------------------------
# the three-way result taxonomy
# ---------------------------------------------------------------------------


class BusinessOutcome(BaseModel):
    """A legitimate non-happy answer this capability can return.

    Per capability, not global: "no such member" is a fine result for a lookup and
    a broken precondition for a transfer.
    """

    code: str
    detect: Condition
    description: str
    #: Whether hitting this ends the run. Almost always true; false leaves room
    #: for an outcome that is worth reporting but not worth stopping for.
    terminal: bool = True


class RecoverableCondition(BaseModel):
    """A known obstruction with a known remedy.

    The remedy is a list of steps, so recovery reuses the ordinary executor.
    Exhausting `max_attempts` converts the condition into a hard failure.
    """

    code: str
    detect: Condition
    description: str
    remedy: list[Step]
    max_attempts: int = 2
    #: Declares *that* a session is needed, never *how* to get one -- credentials
    #: must not live in a stored capability. The engine calls the platform's
    #: session provider.
    reestablish_session: bool = False


# ---------------------------------------------------------------------------
# policy, provenance, surface
# ---------------------------------------------------------------------------


class Policy(BaseModel):
    """The guardrail envelope, stored with the capability.

    Permissions travel with the thing being permitted, so a reviewer sees the blast
    radius on the same page as the steps.
    """

    allowed_origins: list[str] = Field(
        description="Origins the run may touch. Anything else aborts."
    )
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["navigate", "click", "fill", "select", "press", "wait_for"]
    )
    #: Stored explicitly so a catalogue can filter without walking the step list.
    has_irreversible_steps: bool = False
    #: Unattended replay is refused unless set: block by default, let a human turn
    #: it on per capability.
    approved_for_unattended: bool = False


class SurfaceBinding(BaseModel):
    """What kind of surface this was recorded against, and where it starts.

    `kind` is the heterogeneity seam: extending to desktop means a second backend
    behind this field, not a reshaped artifact. `app_id` identifies the vendor
    product and `tenant_id` the institution, which is what makes "record once,
    reuse across tenants" expressible.
    """

    kind: Literal["web", "desktop"] = "web"
    app_id: str
    tenant_id: str
    entry_url: str


class Provenance(BaseModel):
    """How this artifact came to exist -- a capability with no origin story is
    unreviewable."""

    discovered_at: datetime
    model: str
    discovery_run_id: str
    #: A flow that needed 30 turns to find is one to look at before trusting.
    llm_turns: int
    notes: str | None = None


# ---------------------------------------------------------------------------
# the artifact
# ---------------------------------------------------------------------------


class CapabilityArtifact(BaseModel):
    """A complete, replayable, agent-invocable capability."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(description="Stable name, e.g. member_savings_lookup.")
    version: int = Field(ge=1, description="Revision of this capability.")
    name: str
    description: str

    surface: SurfaceBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)

    steps: list[Step]
    success: Checkpoint
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoverables: list[RecoverableCondition] = Field(default_factory=list)

    policy: Policy
    provenance: Provenance

    #: Not implemented. Present so cross-tenant specialisation has somewhere to
    #: land without a schema change: a tenant overrides only the steps that differ.
    extends: str | None = None

    # -- validation -------------------------------------------------------

    @model_validator(mode="after")
    def _check_step_ids_unique(self) -> "CapabilityArtifact":
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id}")
            seen.add(step.id)
        return self

    @model_validator(mode="after")
    def _check_templates_resolve(self) -> "CapabilityArtifact":
        """Rejected at load time, before a browser opens against a banking app and
        half the steps commit."""
        declared = {p.name for p in self.inputs}
        for step in self.steps:
            value = getattr(step.action, "value", None)
            if not isinstance(value, str):
                continue
            for ref in TEMPLATE_PATTERN.findall(value):
                if ref not in declared:
                    raise ValueError(
                        f"step {step.id} references undeclared input {{{{ {ref} }}}}"
                    )
        return self

    @model_validator(mode="after")
    def _check_policy_matches_steps(self) -> "CapabilityArtifact":
        """Without this the risk flag is a comment, and since the approval gate
        reads it, drift would quietly disable the guardrail."""
        actual = any(s.risk == "risky" for s in self.steps)
        if actual and not self.policy.has_irreversible_steps:
            raise ValueError(
                "artifact contains risky steps but policy.has_irreversible_steps is False"
            )
        for step in self.steps:
            if step.action.kind not in self.policy.allowed_actions:
                raise ValueError(
                    f"step {step.id} uses action {step.action.kind!r}, "
                    "which policy.allowed_actions does not permit"
                )
        return self

    def input_by_name(self, name: str) -> ParamSpec | None:
        return next((p for p in self.inputs if p.name == name), None)
