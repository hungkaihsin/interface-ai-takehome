"""What a replay returns.

Three statuses, structurally separate, because "it worked" and "no such member" must
not be expressible the same way:

    SUCCESS           goal reached; `outputs` populated
    BUSINESS_OUTCOME  a legitimate answer that is not the happy path
    FAILURE           something is broken; `error` says step, expected, observed

Recovering is not a fourth status -- a run that dismissed an interstitial and then
read the balance succeeded. Recoveries are recorded so the evidence shows the detour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReplayStatus(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class FailureKind(StrEnum):
    """Why a run broke, grouped by what an operator should do about it.

    Retry: TIMEOUT. Fix the call: INVALID_INPUT. Fix the artifact:
    LOCATOR_UNRESOLVED, AMBIGUOUS_LOCATOR, CHECKPOINT_FAILED. Fix the system:
    APP_ERROR, SURFACE_ERROR. Review policy: POLICY_VIOLATION, APPROVAL_REQUIRED.
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
    #: Kept even on success: a step whose tier-1 strategy quietly stopped working
    #: and has been carried by tier 2 for a month is a drift signal.
    resolution: str | None = None
    checkpoint_passed: bool | None = None
    duration_ms: int = 0


class RecoveryRecord(BaseModel):
    code: str
    description: str
    attempt: int
    succeeded: bool


class ReplayError(BaseModel):
    """A hard failure, described well enough to debug without reproducing it."""

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

    #: Redacted, so a result is self-describing in an evidence file.
    inputs: dict[str, Any] = Field(default_factory=dict)

    outputs: dict[str, Any] | None = None
    outcome: OutcomeReport | None = None
    error: ReplayError | None = None

    steps: list[StepRecord] = Field(default_factory=list)
    recoveries: list[RecoveryRecord] = Field(default_factory=list)

    log_path: str | None = None
    screenshot_path: str | None = None

    #: A capability that needed a person is not the same product as one that ran
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
