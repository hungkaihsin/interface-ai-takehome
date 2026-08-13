"""What a replay returns to its caller.

This type is the answer to the brief's named trap. The glossary says conflating a
business outcome with a failure is the most common design mistake, so the split is
made structural: `status` has three values, and there is no way to express "it
worked" and "no such member" with the same one.

    SUCCESS           the goal was reached; `outputs` is populated
    BUSINESS_OUTCOME  a legitimate answer that is not the happy path; `outcome`
                      says which one, by a code the caller can branch on
    FAILURE           something is broken; `error` says what step, what was
                      expected, what was observed

Recoverable conditions are deliberately *not* a fourth status. Recovering is
something that happens on the way to one of the three, not an outcome in itself --
a run that dismissed an interstitial and then read the balance succeeded. They are
recorded in `recoveries` so the evidence shows the detour, but they never surface
as the answer.

Why the caller cares about the difference, concretely: an agent receiving
BUSINESS_OUTCOME/MEMBER_NOT_FOUND should tell the member their number is wrong. An
agent receiving FAILURE should retry or escalate to a human, and an ops team should
be paged if it keeps happening. Collapsing the two means either paging someone
every time a customer mistypes an account number, or silently telling a member
"not found" when the core banking link is down.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ReplayStatus(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class FailureKind(StrEnum):
    """Why a run broke. Chosen so the right response differs for each.

    Grouped by what an operator should do: retry (TIMEOUT, TRANSIENT), fix the
    call (INVALID_INPUT), fix the artifact (LOCATOR_UNRESOLVED, AMBIGUOUS_LOCATOR,
    CHECKPOINT_FAILED), fix the system (APP_ERROR), or review policy
    (POLICY_VIOLATION, APPROVAL_REQUIRED).
    """

    INVALID_INPUT = "invalid_input"
    POLICY_VIOLATION = "policy_violation"
    APPROVAL_REQUIRED = "approval_required"
    LOCATOR_UNRESOLVED = "locator_unresolved"
    AMBIGUOUS_LOCATOR = "ambiguous_locator"
    CHECKPOINT_FAILED = "checkpoint_failed"
    UNRECOVERED_CONDITION = "unrecovered_condition"
    APP_ERROR = "app_error"
    TIMEOUT = "timeout"
    SURFACE_ERROR = "surface_error"
    UNKNOWN_STATE = "unknown_state"


class StepRecord(BaseModel):
    """One executed step, as it actually went."""

    step_id: str
    intent: str
    action: str
    #: The locator ladder that was walked, in plain text. Kept even on success,
    #: because "tier 1 stopped working and tier 2 has been carrying this step for
    #: a month" is a drift signal that is invisible if only failures are recorded.
    resolution: str | None = None
    checkpoint_passed: bool | None = None
    duration_ms: int = 0


class RecoveryRecord(BaseModel):
    code: str
    description: str
    attempt: int
    succeeded: bool


class ReplayError(BaseModel):
    """A hard failure, described well enough to debug without reproducing it.

    The three fields the brief asks for by name -- what step, what was expected,
    what was observed -- are separate fields rather than one message string, so
    they survive being read by a machine as well as a human.
    """

    kind: FailureKind
    step_id: str | None
    step_intent: str | None
    expected: str
    observed: str
    detail: str | None = None


class OutcomeReport(BaseModel):
    code: str
    description: str


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    capability_version: int
    run_id: str

    started_at: datetime
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0

    #: Arguments as supplied, after redaction. Present so a result is
    #: self-describing in an evidence file, without re-exposing regulated values.
    inputs: dict[str, Any] = Field(default_factory=dict)

    outputs: dict[str, Any] | None = None
    outcome: OutcomeReport | None = None
    error: ReplayError | None = None

    steps: list[StepRecord] = Field(default_factory=list)
    recoveries: list[RecoveryRecord] = Field(default_factory=list)

    log_path: str | None = None
    screenshot_path: str | None = None

    #: True when a human took over mid-run. Reported to the caller because a
    #: capability that needed a person is not the same product as one that ran
    #: unattended, even when both end in SUCCESS.
    escalated: bool = False

    def summary(self) -> str:
        if self.status is ReplayStatus.SUCCESS:
            return f"SUCCESS outputs={self.outputs}"
        if self.status is ReplayStatus.BUSINESS_OUTCOME:
            assert self.outcome is not None
            return f"BUSINESS_OUTCOME {self.outcome.code}"
        assert self.error is not None
        return (
            f"FAILURE {self.error.kind} at step {self.error.step_id}: "
            f"expected {self.error.expected!r}, observed {self.error.observed!r}"
        )


ReplayStatusLiteral = Literal["success", "business_outcome", "failure"]
