"""Deterministic replay: the production execution path.

No LLM is imported anywhere in this package. That absence is the design claim, and
a reviewer can check it by unsetting every API key and watching replay still work.

Two rules the implementation turns on:

Never look once. After an action the screen may legitimately be any of several
states, or not have arrived yet, so the engine polls the whole known state space
until one member matches. Checking immediately was measured to be intermittently
correct, which is worse than consistently wrong.

Restarting is gated on reversibility. Recovering a lost session means replaying
from the top, which is safe only while every executed step was reversible. If a
risky step has committed, the engine escalates instead of risking a duplicate.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from capability.schema import (
    BusinessOutcome,
    CapabilityArtifact,
    ClickAction,
    FillAction,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    PressAction,
    RecoverableCondition,
    SelectAction,
    Step,
    WaitForAction,
)
from capability.schema import TEMPLATE_PATTERN
from observability.log import RunLog, new_run_id
from observability.redaction import Redactor
from surface.base import Resolution, Surface, SurfaceError

from .result import (
    FailureKind,
    OutcomeReport,
    RecoveryRecord,
    ReplayError,
    ReplayResult,
    ReplayStatus,
    StepRecord,
)

#: How long to wait for the screen to become one of the states we know about.
SETTLE_TIMEOUT_MS = 12_000

#: Hard ceiling on step+recovery iterations for one run. A loop guard, not a policy.
MAX_ITERATIONS = 60


class EscalationHandler(Protocol):
    """Returns True if a human resolved it and replay should continue.

    A protocol so the engine has no opinion about how a human is reached.
    """

    def handle(self, context: Any) -> bool: ...


class PolicyViolation(Exception):
    def __init__(self, what: str) -> None:
        self.what = what
        super().__init__(what)


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        session_provider: Any | None = None,
        escalation: EscalationHandler | None = None,
        evidence_dir: str = "evidence",
        echo: bool = False,
    ) -> None:
        self.surface = surface
        self.session_provider = session_provider
        self.escalation = escalation
        self.evidence_dir = evidence_dir
        self.echo = echo

    # -- public entry point ------------------------------------------------

    def run(
        self, artifact: CapabilityArtifact, inputs: dict[str, Any]
    ) -> ReplayResult:
        started = datetime.now(timezone.utc)
        run_id = new_run_id(f"replay-{artifact.capability_id}")

        redactor = Redactor()
        if self.session_provider is not None:
            redactor.add_literals(self.session_provider.secrets())
        redactor.add_literals(
            inputs.get(p.name) for p in artifact.inputs if p.sensitive
        )

        log = RunLog(run_id, self.evidence_dir, redactor, echo=self.echo)
        log.event(
            "replay.start",
            capability=artifact.capability_id,
            version=artifact.version,
            schema_version=artifact.schema_version,
            tenant=artifact.surface.tenant_id,
            inputs=inputs,
        )

        state = _RunState(artifact, inputs, log, redactor, run_id, started)
        try:
            self._preflight(state)
            self._establish_session(state)
            result = self._execute(state)
        except _Reject as exc:
            # No screenshot: the run never got far enough for anything to be on
            # screen worth capturing.
            result = state.fail(exc.kind, expected=exc.expected, observed=exc.observed)
        except PolicyViolation as exc:
            result = state.fail(
                FailureKind.POLICY_VIOLATION,
                expected="an action permitted by the capability's policy",
                observed=exc.what,
            )
        except SurfaceError as exc:
            result = state.fail(
                FailureKind.SURFACE_ERROR,
                expected="a responsive application surface",
                observed=str(exc),
                screenshot=self._capture(run_id, "surface-error"),
            )

        log.event("replay.finish", status=result.status, summary=result.summary())
        return result

    # -- phases ------------------------------------------------------------

    def _preflight(self, state: "_RunState") -> None:
        """Everything checkable before a browser is touched.

        A policy breach caught here never touches the institution's app at all.
        """
        artifact = state.artifact

        for spec in artifact.inputs:
            if spec.name not in state.inputs:
                if spec.required:
                    raise _Reject(
                        FailureKind.INVALID_INPUT,
                        f"required input {spec.name!r}",
                        "not supplied",
                    )
                continue
            _validate_param(spec, state.inputs[spec.name])

        unknown = set(state.inputs) - {p.name for p in artifact.inputs}
        if unknown:
            raise _Reject(
                FailureKind.INVALID_INPUT,
                "only declared inputs",
                f"unexpected argument(s): {sorted(unknown)}",
            )

        _assert_origin_allowed(artifact.surface.entry_url, artifact)

        if artifact.policy.has_irreversible_steps and not artifact.policy.approved_for_unattended:
            raise _Reject(
                FailureKind.APPROVAL_REQUIRED,
                "an approved capability for unattended replay",
                "capability performs irreversible actions and is not approved",
            )

        state.log.event("replay.preflight_ok", inputs_validated=len(artifact.inputs))

    def _establish_session(self, state: "_RunState") -> None:
        if self.session_provider is None:
            return
        self.session_provider.establish(self.surface)
        state.log.event("session.established")

    def _execute(self, state: "_RunState") -> ReplayResult:
        """Walk the steps, then classify the final screen.

        One loop, not two phases: the last step may land on a recoverable
        obstruction that has to be cleared and re-classified, and recovery can
        rewind to the first step, so both share a cursor.
        """
        artifact = state.artifact
        index = 0
        iterations = 0

        while True:
            iterations += 1
            if iterations > MAX_ITERATIONS:
                # Per-condition attempt caps should make this unreachable. If it
                # fires, a bounded failure beats a hung automation holding a
                # session open against a banking app.
                return state.fail(
                    FailureKind.UNRECOVERED_CONDITION,
                    expected="the flow to reach a terminal state",
                    observed=f"exceeded {MAX_ITERATIONS} execution iterations",
                    step_id=artifact.steps[index].id if index < len(artifact.steps) else None,
                )

            if index < len(artifact.steps):
                outcome = self._run_step(state, artifact.steps[index])
                if outcome.kind == "terminal":
                    return outcome.result  # type: ignore[return-value]
                index = 0 if outcome.kind == "restart" else index + 1
                continue

            verdict = self._settle(state, artifact.success.timeout_ms)

            if isinstance(verdict, BusinessOutcome):
                return state.business_outcome(verdict)

            if isinstance(verdict, RecoverableCondition):
                outcome = self._recover(state, verdict, artifact.steps[-1])
                if outcome.kind == "terminal":
                    return outcome.result  # type: ignore[return-value]
                if outcome.kind == "restart":
                    index = 0
                continue

            if verdict == "success":
                return self._extract_outputs(state)

            # Settled into something this capability does not recognise: the
            # definition of stuck, so offer it to a human before failing.
            return self._escalate_or_fail(
                state,
                artifact.steps[-1] if artifact.steps else None,
                reason=(
                    f"expected {artifact.success.description!r} but the screen is "
                    "not a state this capability declares"
                ),
                kind=FailureKind.UNKNOWN_STATE,
            )

    # -- one step ----------------------------------------------------------

    def _run_step(self, state: "_RunState", step: Step) -> "_StepOutcome":
        started = time.monotonic()
        artifact = state.artifact

        if step.action.kind not in artifact.policy.allowed_actions:
            raise PolicyViolation(
                f"step {step.id} uses {step.action.kind!r}, not in policy allowlist"
            )
        if step.risk == "risky" and not artifact.policy.approved_for_unattended:
            raise PolicyViolation(
                f"step {step.id} is irreversible and the capability is not approved"
            )

        try:
            resolution = self._act(state, step)
        except SurfaceError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert driver errors into results
            return _terminal(
                state.fail(
                    FailureKind.SURFACE_ERROR,
                    expected=step.intent,
                    observed=f"{type(exc).__name__}: {exc}",
                    step_id=step.id,
                    screenshot=self._capture(state.run_id, f"{step.id}-driver-error"),
                )
            )

        if step.risk == "risky":
            state.committed_irreversible = True

        # A control we could not find may mean the screen became a known outcome.
        # Classify before blaming the locator.
        if resolution is not None and not resolution.resolved:
            verdict = self._settle(state, SETTLE_TIMEOUT_MS // 3)
            if isinstance(verdict, BusinessOutcome):
                return _terminal(state.business_outcome(verdict))
            if isinstance(verdict, RecoverableCondition):
                return self._recover(state, verdict, step)
            kind = (
                FailureKind.AMBIGUOUS_LOCATOR
                if resolution.ambiguous
                else FailureKind.LOCATOR_UNRESOLVED
            )
            return _terminal(
                state.fail(
                    kind,
                    expected=f"to find {resolution.locator.describes}",
                    observed=resolution.explain(),
                    step_id=step.id,
                    screenshot=self._capture(state.run_id, f"{step.id}-unresolved"),
                )
            )

        record = StepRecord(
            step_id=step.id,
            intent=step.intent,
            action=step.action.kind,
            resolution=resolution.explain() if resolution else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        if step.checkpoint is not None:
            passed = self.surface.wait_for(
                step.checkpoint.condition, step.checkpoint.timeout_ms
            )
            record.checkpoint_passed = passed
            state.steps.append(record)
            state.log.event(
                "step.done", step=step.id, intent=step.intent, checkpoint=passed
            )
            if not passed:
                verdict = self._settle(state, SETTLE_TIMEOUT_MS // 3)
                if isinstance(verdict, BusinessOutcome):
                    return _terminal(state.business_outcome(verdict))
                if isinstance(verdict, RecoverableCondition):
                    return self._recover(state, verdict, step)
                return _terminal(
                    state.fail(
                        FailureKind.CHECKPOINT_FAILED,
                        expected=step.checkpoint.description,
                        observed=self._describe_screen(state),
                        step_id=step.id,
                        screenshot=self._capture(
                            state.run_id, f"{step.id}-checkpoint-failed"
                        ),
                    )
                )
            return _continue()

        state.steps.append(record)
        state.log.event("step.done", step=step.id, intent=step.intent)
        return _continue()

    def _act(self, state: "_RunState", step: Step) -> Resolution | None:
        action = step.action
        surface = self.surface

        if isinstance(action, NavigateAction):
            url = state.render(action.url)
            _assert_origin_allowed(url, state.artifact)
            surface.goto(url)
            state.log.event("action.navigate", step=step.id, url=url)
            return None

        if isinstance(action, WaitForAction):
            ok = surface.wait_for(action.condition, action.timeout_ms)
            state.log.event("action.wait_for", step=step.id, satisfied=ok)
            return None

        if isinstance(action, FillAction):
            value = state.render(action.value)
            state.log.event(
                "action.fill", step=step.id, target=action.target.describes, value=value
            )
            return surface.fill(action.target, value)

        if isinstance(action, ClickAction):
            state.log.event("action.click", step=step.id, target=action.target.describes)
            return surface.click(action.target)

        if isinstance(action, SelectAction):
            value = state.render(action.value)
            state.log.event("action.select", step=step.id, value=value)
            return surface.select(action.target, value)

        if isinstance(action, PressAction):
            state.log.event("action.press", step=step.id, key=action.key)
            return surface.press(action.target, action.key)

        raise PolicyViolation(f"unsupported action {action.kind!r}")

    # -- classification ----------------------------------------------------

    def _settle(
        self, state: "_RunState", timeout_ms: int
    ) -> BusinessOutcome | RecoverableCondition | str:
        """Poll until the screen is a state this capability knows about.

        Outcomes are checked before success: a screen saying "no member found" must
        never be read as a slow-loading record.
        """
        artifact = state.artifact
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for outcome in artifact.business_outcomes:
                if self.surface.check(outcome.detect):
                    return outcome
            for condition in artifact.recoverables:
                if state.attempts_left(condition) and self.surface.check(
                    condition.detect
                ):
                    return condition
            if self.surface.check(artifact.success.condition):
                return "success"
            if time.monotonic() >= deadline:
                return "unknown"
            time.sleep(0.15)

    def _recover(
        self, state: "_RunState", condition: RecoverableCondition, step: Step
    ) -> "_StepOutcome":
        attempt = state.note_attempt(condition)
        state.log.event(
            "recovery.start", code=condition.code, attempt=attempt, at_step=step.id
        )

        if condition.reestablish_session:
            # Only safe while nothing irreversible has committed -- otherwise a
            # retry could open a second account.
            if state.committed_irreversible:
                return _terminal(
                    self._escalate_or_fail(
                        state,
                        step,
                        reason=(
                            "session expired after an irreversible step had already "
                            "committed; restarting could duplicate it"
                        ),
                        kind=FailureKind.UNRECOVERED_CONDITION,
                    )
                )
            if self.session_provider is None:
                return _terminal(
                    state.fail(
                        FailureKind.UNRECOVERED_CONDITION,
                        expected="a way to re-establish the operator session",
                        observed="no session provider is configured",
                        step_id=step.id,
                    )
                )
            self.session_provider.establish(self.surface)
            state.recoveries.append(
                RecoveryRecord(
                    code=condition.code,
                    description=condition.description,
                    attempt=attempt,
                    succeeded=True,
                )
            )
            state.log.event("recovery.session_reestablished", code=condition.code)
            return _restart()

        for remedy_step in condition.remedy:
            try:
                self._act(state, remedy_step)
            except Exception as exc:  # noqa: BLE001
                state.log.event("recovery.error", code=condition.code, error=str(exc))
                break

        # Wait for the obstruction to actually go, rather than reading the screen
        # the remedy is still navigating away from.
        cleared = self.surface.wait_until_absent(condition.detect, 6_000)
        state.recoveries.append(
            RecoveryRecord(
                code=condition.code,
                description=condition.description,
                attempt=attempt,
                succeeded=cleared,
            )
        )
        state.log.event("recovery.done", code=condition.code, cleared=cleared)

        if cleared:
            return _continue()

        if state.attempts_left(condition):
            return self._recover(state, condition, step)

        return _terminal(
            self._escalate_or_fail(
                state,
                step,
                reason=f"{condition.code} did not clear after {attempt} attempt(s)",
                kind=FailureKind.UNRECOVERED_CONDITION,
            )
        )

    def _escalate_or_fail(
        self, state: "_RunState", step: Step | None, reason: str, kind: FailureKind
    ) -> ReplayResult:
        """Try a human before giving up.

        Offered, never assumed: with no console configured this is an ordinary
        failure, so unattended replay never depends on somebody being awake.
        """
        step_id = step.id if step is not None else None
        if self.escalation is not None:
            from .escalation import InterventionRequest

            request = InterventionRequest(
                run_id=state.run_id,
                capability_id=state.artifact.capability_id,
                goal=state.artifact.description,
                step_id=step_id,
                step_intent=step.intent if step is not None else None,
                reason=reason,
                screenshot_path=self._capture(
                    state.run_id, f"{step_id or 'final'}-stuck"
                ),
                perception=self._describe_screen(state),
            )
            state.log.event("escalation.raised", step=step_id, reason=reason)
            # Lend the console this run's log, so the handoff lands in the same
            # evidence file as the automation's own steps.
            if getattr(self.escalation, "log", "missing") is None:
                self.escalation.log = state.log  # type: ignore[attr-defined]
            resumed = self.escalation.handle(request)
            state.escalated = True
            state.log.event("escalation.returned", resumed=resumed)
            if resumed:
                verdict = self._settle(state, SETTLE_TIMEOUT_MS)
                if isinstance(verdict, BusinessOutcome):
                    return state.business_outcome(verdict)
                if verdict == "success":
                    return self._extract_outputs(state)

        return state.fail(
            kind,
            expected="a recoverable state or human resolution",
            observed=reason,
            step_id=step_id,
            screenshot=self._capture(state.run_id, f"{step_id or 'final'}-stuck"),
        )

    # -- outputs -----------------------------------------------------------

    def _extract_outputs(self, state: "_RunState") -> ReplayResult:
        values: dict[str, Any] = {}
        for spec in state.artifact.outputs:
            raw, resolution = self.surface.text_of(spec.source.locator)
            if raw is None:
                if spec.required:
                    return state.fail(
                        FailureKind.LOCATOR_UNRESOLVED,
                        expected=f"to read {spec.name} from {spec.source.locator.describes}",
                        observed=resolution.explain(),
                        screenshot=self._capture(state.run_id, f"output-{spec.name}"),
                    )
                continue
            values[spec.name] = _coerce(_transform(raw, spec), spec)

        state.log.event("outputs.extracted", outputs=values)
        return state.success(values)

    # -- helpers -----------------------------------------------------------

    def _capture(self, run_id: str, label: str) -> str | None:
        return self.surface.screenshot(f"{run_id}-{label}")

    def _describe_screen(self, state: "_RunState") -> str:
        """What lands in `observed` on a failure: readable at 3am, safe to store."""
        try:
            perception = self.surface.perceive()
        except Exception as exc:  # noqa: BLE001
            return f"could not perceive surface: {exc}"
        frames = []
        for frame in perception.frames:
            if not frame.text.strip():
                continue
            label = "/".join(frame.path) or "(top)"
            frames.append(f"[{label}] {' '.join(frame.text.split())[:300]}")
        return state.redactor.text(" | ".join(frames))[:900] or "(blank screen)"


# ---------------------------------------------------------------------------
# run state
# ---------------------------------------------------------------------------


class _Reject(Exception):
    def __init__(self, kind: FailureKind, expected: str, observed: str) -> None:
        self.kind, self.expected, self.observed = kind, expected, observed
        super().__init__(observed)


class _RunState:
    def __init__(
        self,
        artifact: CapabilityArtifact,
        inputs: dict[str, Any],
        log: RunLog,
        redactor: Redactor,
        run_id: str,
        started: datetime,
    ) -> None:
        self.artifact = artifact
        self.inputs = inputs
        self.log = log
        self.redactor = redactor
        self.run_id = run_id
        self.started = started
        self.steps: list[StepRecord] = []
        self.recoveries: list[RecoveryRecord] = []
        self.attempts: dict[str, int] = {}
        self.committed_irreversible = False
        self.escalated = False

    def render(self, template: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in self.inputs:
                raise _Reject(
                    FailureKind.INVALID_INPUT, f"a value for {name!r}", "not supplied"
                )
            return str(self.inputs[name])

        return TEMPLATE_PATTERN.sub(substitute, template)

    def attempts_left(self, condition: RecoverableCondition) -> bool:
        return self.attempts.get(condition.code, 0) < condition.max_attempts

    def note_attempt(self, condition: RecoverableCondition) -> int:
        self.attempts[condition.code] = self.attempts.get(condition.code, 0) + 1
        return self.attempts[condition.code]

    # -- terminal results -------------------------------------------------

    def _base(self, status: ReplayStatus) -> dict[str, Any]:
        finished = datetime.now(timezone.utc)
        return {
            "status": status,
            "capability_id": self.artifact.capability_id,
            "capability_version": self.artifact.version,
            "run_id": self.run_id,
            "started_at": self.started,
            "finished_at": finished,
            "duration_ms": int((finished - self.started).total_seconds() * 1000),
            "inputs": self.redactor.value(dict(self.inputs)),
            "steps": self.steps,
            "recoveries": self.recoveries,
            "log_path": str(self.log.path),
            "escalated": self.escalated,
        }

    def success(self, outputs: dict[str, Any]) -> ReplayResult:
        return ReplayResult(**self._base(ReplayStatus.SUCCESS), outputs=outputs)

    def business_outcome(self, outcome: BusinessOutcome) -> ReplayResult:
        self.log.event("outcome.detected", code=outcome.code)
        return ReplayResult(
            **self._base(ReplayStatus.BUSINESS_OUTCOME),
            outcome=OutcomeReport(code=outcome.code, description=outcome.description),
        )

    def fail(
        self,
        kind: FailureKind,
        expected: str,
        observed: str,
        step_id: str | None = None,
        screenshot: str | None = None,
    ) -> ReplayResult:
        intent = next((s.intent for s in self.artifact.steps if s.id == step_id), None)
        error = ReplayError(
            kind=kind,
            step_id=step_id,
            step_intent=intent,
            expected=expected,
            observed=self.redactor.text(observed),
        )
        self.log.event("failure", **error.model_dump(mode="json"))
        return ReplayResult(
            **self._base(ReplayStatus.FAILURE), error=error, screenshot_path=screenshot
        )


class _StepOutcome:
    def __init__(self, kind: str, result: ReplayResult | None = None) -> None:
        self.kind = kind
        self.result = result


def _continue() -> _StepOutcome:
    return _StepOutcome("continue")


def _restart() -> _StepOutcome:
    return _StepOutcome("restart")


def _terminal(result: ReplayResult) -> _StepOutcome:
    return _StepOutcome("terminal", result)


# ---------------------------------------------------------------------------
# input validation, transforms, coercion
# ---------------------------------------------------------------------------


def _validate_param(spec: ParamSpec, value: Any) -> None:
    text = str(value)
    if spec.type in ("number", "money", "integer"):
        try:
            float(text)
        except ValueError:
            raise _Reject(
                FailureKind.INVALID_INPUT, f"{spec.name} to be numeric", repr(value)
            ) from None
    if spec.pattern and not re.fullmatch(spec.pattern, text):
        # The offending value is not echoed -- it may be regulated data.
        raise _Reject(
            FailureKind.INVALID_INPUT,
            f"{spec.name} matching {spec.pattern}",
            f"a {len(text)}-character value that does not match",
        )


def _assert_origin_allowed(url: str, artifact: CapabilityArtifact) -> None:
    if not any(url.startswith(origin) for origin in artifact.policy.allowed_origins):
        raise PolicyViolation(
            f"{url!r} is outside the allowlist {artifact.policy.allowed_origins}"
        )


def _transform(raw: str, spec: OutputSpec) -> str:
    value = raw
    for transform in spec.source.transform:
        if transform.kind == "strip":
            value = value.strip()
        elif transform.kind == "strip_currency":
            value = re.sub(r"[^\d.\-]", "", value)
        elif transform.kind == "to_number":
            value = re.sub(r"[,\s$]", "", value).strip()
        elif transform.kind == "regex_extract" and transform.pattern:
            match = re.search(transform.pattern, value)
            value = match.group(1) if match and match.groups() else (match.group(0) if match else "")
    return value


def _coerce(value: str, spec: OutputSpec) -> Any:
    if spec.type in ("number", "money"):
        return float(value)
    if spec.type == "integer":
        return int(float(value))
    if spec.type == "boolean":
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return value
