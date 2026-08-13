"""The capability artifact -- the typed, versioned recipe a successful run leaves behind.

This is the centre of the system. Everything upstream (the LLM discovery loop)
exists to produce one of these; everything downstream (deterministic replay, the
safety layer, escalation) exists to consume one. The brief calls the schema "a
focal point of the evaluation", so the shaping choices are argued in place.

The four decisions worth defending:

1. **It is a contract, not a macro.** A recording of clicks would be enough to
   re-run one flow. It would not be enough for an AI agent to *call* -- an agent
   needs to know what arguments to pass, what it gets back, and what the possible
   answers are, without reading the steps. So inputs, outputs, success condition
   and business outcomes are all declared at the top level, and the step list is
   an implementation detail underneath them.

2. **Expected outcomes are declared data, not exception handling.** `business_outcomes`
   sits beside `success` as a first-class field. "No such member" is an answer the
   caller asked for, and the artifact says up front which answers this capability
   can return. This is the brief's named "most common design mistake", and the
   schema is where we refuse to make it -- a taxonomy that lives only in `try/except`
   is invisible to the agent deciding whether to call the capability at all.

3. **Two independent version numbers.** `schema_version` versions the *format*, so
   an old artifact stays loadable by a newer engine. `version` versions *this
   capability*, so a re-recording after a vendor upgrade is a new revision of the
   same named thing rather than a different capability. Conflating them would mean
   a format change silently invalidating every stored recipe.

4. **Values are templates, not literals.** A step fills `{{ member_id }}`, never
   `10001`. That is what makes the artifact a parameterised capability instead of
   a transcript of one afternoon -- and it is also a privacy property, because the
   concrete member ID never enters the stored file.
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
    """Is this text on the screen (in this frame)?

    The workhorse. Legacy apps signal almost everything through prose -- "No member
    found", "Entitlement check failed" -- and that prose is far more stable than
    the markup wrapped around it.
    """

    kind: Literal["text_present"] = "text_present"
    frame_path: list[str] = Field(default_factory=list)
    text: str
    match: Literal["exact", "contains", "regex"] = "contains"
    #: Where to look. "frame" checks exactly `frame_path`; "any_frame" checks
    #: every frame in the document.
    #:
    #: The escape hatch exists because some conditions genuinely are not
    #: frame-stable. A session timeout lands in the inner frame when an inner
    #: action triggered it, and replaces the whole document when a top-level
    #: navigation did -- so pinning it to one path would make detection depend on
    #: which step happened to expire. Conditions that *are* frame-stable, like a
    #: "no member found" result, stay pinned, because a broad scan would also
    #: match that text appearing in a nav panel or a help page.
    scope: Literal["frame", "any_frame"] = "frame"


class ElementPresent(BaseModel):
    """Does a control we can address actually resolve on screen?"""

    kind: Literal["element_present"] = "element_present"
    locator: Locator


class UrlMatches(BaseModel):
    """Does the frame's URL match a pattern?

    Weakest of the three and used only as corroboration: a URL says where the
    browser went, not what rendered. A session-timeout bounce and a successful
    load can both leave you somewhere plausible.
    """

    kind: Literal["url_matches"] = "url_matches"
    frame_path: list[str] = Field(default_factory=list)
    pattern: str


Condition = Annotated[
    Union[TextPresent, ElementPresent, UrlMatches], Field(discriminator="kind")
]


class Checkpoint(BaseModel):
    """An assertion that we actually reached the state we expected.

    The brief's glossary defines this as confirming you arrived rather than
    assuming the click worked -- and the distinction has teeth, because in this
    app a click that silently lands on the login screen after a session timeout
    looks exactly like a click that worked, right up until you read the page.
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
    #: Validated before the browser is ever opened. Catching a malformed member
    #: number here costs nothing; catching it three screens in costs a session.
    pattern: str | None = None
    example: Any | None = None
    #: Marks an argument as regulated data. Such values are resolved into the live
    #: session but never written to a log line or an evidence file.
    sensitive: bool = False


class Transform(BaseModel):
    """A pure, declarative cleanup applied to scraped text.

    Declarative rather than a code hook on purpose: an artifact is a document that
    a compliance reviewer should be able to read, and an embedded lambda would make
    it an executable of unknown behaviour.
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
    """Wait for a condition rather than a duration.

    There is no `sleep` action anywhere in this schema, and that is deliberate:
    fixed sleeps are the standard source of flaky automation. The 3-second stall
    the target app injects is absorbed by waiting for the *record* to appear, which
    is correct whether the stall is 3 seconds or 8.
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

#: Reversibility, judged at record time and enforced at replay time.
#:
#: "safe" means doing it twice is indistinguishable from doing it once -- reads,
#: navigation, typing into a field. "risky" means it changes state the institution
#: or its member can see: opening an account, moving money, altering a record.
#: The split is about *reversibility*, not about how dangerous it sounds, because
#: reversibility is the property that decides whether an automatic retry is safe.
RiskClass = Literal["safe", "risky"]


class Step(BaseModel):
    id: str
    #: Why this step exists, in plain language, captured at discovery time. This
    #: is what a human reviewer reads and what a failure message quotes; without
    #: it, a failed artifact is a pile of roles and names.
    intent: str
    action: Action
    risk: RiskClass = "safe"
    #: Verified immediately after the step. Per-step checkpoints are what let a
    #: failure name the step that broke rather than reporting that the run ended
    #: somewhere unexpected.
    checkpoint: Checkpoint | None = None


# ---------------------------------------------------------------------------
# the three-way result taxonomy
# ---------------------------------------------------------------------------


class BusinessOutcome(BaseModel):
    """A legitimate non-happy answer this capability can return.

    Declared per capability rather than globally, because what counts as an
    expected answer is capability-specific: "no such member" is a fine result for
    a lookup and a broken precondition for a transfer.
    """

    code: str
    detect: Condition
    description: str
    #: Whether hitting this ends the run. Almost always true; false leaves room
    #: for an outcome that is worth reporting but not worth stopping for.
    terminal: bool = True


class RecoverableCondition(BaseModel):
    """A known obstruction with a known remedy.

    The remedy is itself a list of steps, so recovery reuses the ordinary executor
    rather than a parallel code path. `max_attempts` is what stops a remedy that
    does not actually clear the obstruction from looping forever -- exhausting it
    converts the condition into a hard failure, which is the honest outcome.
    """

    code: str
    detect: Condition
    description: str
    remedy: list[Step]
    max_attempts: int = 2
    #: Set when recovery requires a fresh authenticated session.
    #:
    #: The artifact declares *that* a session is needed and never *how* to get
    #: one, because obtaining one means credentials, and credentials must never
    #: live in a stored capability. The engine satisfies this by calling the
    #: platform's session provider, so the same artifact works for any tenant
    #: whose credentials are configured elsewhere.
    reestablish_session: bool = False


# ---------------------------------------------------------------------------
# policy, provenance, surface
# ---------------------------------------------------------------------------


class Policy(BaseModel):
    """The guardrail envelope, stored *with* the capability.

    Carried in the artifact rather than only in global config so that the
    permissions travel with the thing being permitted: a capability that can open
    accounts declares that about itself, and a reviewer approving it sees the
    blast radius on the same page as the steps.
    """

    allowed_origins: list[str] = Field(
        description="Origins the run may touch. Anything else aborts."
    )
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["navigate", "click", "fill", "select", "press", "wait_for"]
    )
    #: True if any step is risky. Derived at build time, stored explicitly so a
    #: catalogue can filter on it without walking the step list.
    has_irreversible_steps: bool = False
    #: Unattended replay is refused unless this is set. The gate for the risky
    #: class: block by default, let a human turn it on per capability.
    approved_for_unattended: bool = False


class SurfaceBinding(BaseModel):
    """What kind of surface this was recorded against, and where it starts.

    `kind` is the seam for heterogeneity. The steps above name roles, names and
    containers -- vocabulary a desktop accessibility API supplies just as a browser
    does -- so extending to desktop means writing a second backend behind this
    field, not reshaping the artifact.

    `tenant_id` and `app_id` are the seam for multi-tenancy: `app_id` identifies
    the vendor product, `tenant_id` the institution running it. Two tenants on the
    same product share an `app_id`, which is what makes "record once, reuse across
    tenants" expressible at all.
    """

    kind: Literal["web", "desktop"] = "web"
    app_id: str
    tenant_id: str
    entry_url: str


class Provenance(BaseModel):
    """How this artifact came to exist.

    Recorded because a replayable capability with no origin story is unreviewable:
    a compliance officer needs to know it was machine-discovered, by which model,
    on which date, against which app version.
    """

    discovered_at: datetime
    model: str
    discovery_run_id: str
    #: Number of model turns the discovery took. A crude but real signal -- a flow
    #: that needed 30 turns to find is one to look at before trusting.
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

    #: Optional pointer to a base capability this one specialises. Not implemented
    #: -- it exists so the multi-tenant story has somewhere to land without a
    #: schema change: a tenant whose vendor app differs slightly would publish an
    #: artifact that extends the base and overrides only the steps that differ.
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
        """Every `{{ param }}` must name a declared input.

        Checked at load time so a broken artifact is rejected before it opens a
        browser against a banking app, rather than failing partway through a flow
        with half its steps already committed.
        """
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
        """The declared risk flag must match what the steps actually do.

        Without this, `has_irreversible_steps` is a comment. With it, an artifact
        that opens an account cannot claim to be read-only -- and since that flag
        is what the approval gate reads, letting the two drift would quietly
        disable the guardrail.
        """
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
