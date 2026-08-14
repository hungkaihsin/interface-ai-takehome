"""Human-in-the-loop escalation and handoff.

"Stuck" is a defined state, not a feeling: the surface settled into something the
capability does not declare, a remedy exhausted its attempt budget, or a session was
lost after an irreversible step committed.

The human works in the *same* browser context -- nothing is torn down, so session,
cookies and half-filled forms survive. Control transfer is a context manager, so
exactly one side can act and no locking is needed. Navigations during the takeover
are observed from the live page rather than self-reported.

The operator console is mocked (a terminal prompt, or a scripted stand-in so the
evidence is reproducible). Both go through the identical handoff path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol


class ControlHolder(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class InterventionRequest:
    """Everything a human needs to act, assembled at the moment of being stuck."""

    run_id: str
    capability_id: str
    goal: str
    step_id: str | None
    step_intent: str | None
    reason: str
    #: Redacted: an intervention request is a message that leaves the process.
    perception: str
    screenshot_path: str | None = None
    raised_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def render(self) -> str:
        return "\n".join(
            [
                "=" * 74,
                "  INTERVENTION REQUIRED",
                "=" * 74,
                f"  run        : {self.run_id}",
                f"  capability : {self.capability_id}",
                f"  goal       : {self.goal}",
                f"  stuck at   : {self.step_id or '(after final step)'}"
                + (f" -- {self.step_intent}" if self.step_intent else ""),
                f"  because    : {self.reason}",
                f"  screenshot : {self.screenshot_path or '(none captured)'}",
                "-" * 74,
                "  what the screen says now:",
                f"    {self.perception[:600]}",
                "=" * 74,
            ]
        )


@dataclass
class HandoffRecord:
    """An observed account of one takeover."""

    run_id: str
    started_at: datetime
    ended_at: datetime | None = None
    holder: ControlHolder = ControlHolder.HUMAN
    url_before: str | None = None
    url_after: str | None = None
    #: Observed by listening to the live page, not reported by the operator.
    navigations: list[str] = field(default_factory=list)
    operator_note: str | None = None
    resumed: bool = False

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


class SessionHandoff:
    """Transfers control of a live surface to a human and records what happened.

    A context manager so control comes back whatever the human does, including
    raising.
    """

    def __init__(self, surface: Any, run_id: str, log: Any | None = None) -> None:
        self.surface = surface
        self.run_id = run_id
        self.log = log
        self.record = HandoffRecord(
            run_id=run_id, started_at=datetime.now(timezone.utc)
        )
        self._listener: Callable[[Any], None] | None = None

    def __enter__(self) -> HandoffRecord:
        page = getattr(self.surface, "page", None)
        if page is not None:
            self.record.url_before = page.url

            def on_navigated(frame: Any) -> None:
                try:
                    self.record.navigations.append(f"{frame.name or 'top'} -> {frame.url}")
                except Exception:  # noqa: BLE001
                    pass

            self._listener = on_navigated
            page.on("framenavigated", on_navigated)

        if self.log is not None:
            self.log.event(
                "control.transferred", to=ControlHolder.HUMAN, run=self.run_id
            )
        return self.record

    def __exit__(self, *exc: object) -> None:
        page = getattr(self.surface, "page", None)
        if page is not None and self._listener is not None:
            # Wait for the navigation the human just triggered, or the record
            # misses the very click that resolved the intervention. Must be
            # wait_for_timeout, not time.sleep: Playwright's sync API only
            # dispatches events while inside a Playwright call.
            deadline = time.monotonic() + 2.0
            while not self.record.navigations and time.monotonic() < deadline:
                try:
                    page.wait_for_timeout(50)
                except Exception:  # noqa: BLE001
                    break
            try:
                page.remove_listener("framenavigated", self._listener)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.record.url_after = page.url
            except Exception:  # noqa: BLE001
                pass
        self.record.ended_at = datetime.now(timezone.utc)
        self.record.holder = ControlHolder.AUTOMATION
        if self.log is not None:
            self.log.event(
                "control.returned",
                to=ControlHolder.AUTOMATION,
                resumed=self.record.resumed,
                navigations=self.record.navigations,
                note=self.record.operator_note,
                duration_ms=self.record.duration_ms,
            )


class OperatorConsole(Protocol):
    """How an intervention request reaches a person. Mocked at this seam."""

    def handle(self, request: InterventionRequest) -> bool: ...


@dataclass
class TerminalOperatorConsole:
    """A real human, at the real browser window, on the real session.

    Needs `headless=False`; says so rather than pretending the handoff happened.
    """

    surface: Any
    log: Any | None = None

    def handle(self, request: InterventionRequest) -> bool:
        print("\n" + request.render())
        if getattr(self.surface, "headless", True):
            print(
                "  NOTE: the browser is headless, so there is no window to take "
                "over.\n  Re-run with --headed for a real handoff."
            )
        with SessionHandoff(self.surface, request.run_id, self.log) as record:
            print(
                "\n  You now have control of the live session.\n"
                "  Do whatever is needed in the browser window, then come back here.\n"
            )
            answer = input("  Resume automation? [y/N] ").strip().lower()
            record.operator_note = input("  What did you do? (one line) ").strip() or None
            record.resumed = answer.startswith("y")
        return record.resumed


@dataclass
class ScriptedOperatorConsole:
    """A stand-in operator performing a fixed remedy, so evidence is reproducible.

    Not a shortcut around the mechanism: it acquires control through the same
    `SessionHandoff` and is recorded by the same observer. Swap it for
    `TerminalOperatorConsole` and the engine cannot tell the difference.
    """

    surface: Any
    actions: Callable[[Any], None]
    note: str = "scripted operator remedy"
    resume: bool = True
    log: Any | None = None

    def handle(self, request: InterventionRequest) -> bool:
        print(request.render())
        with SessionHandoff(self.surface, request.run_id, self.log) as record:
            try:
                self.actions(self.surface)
                record.operator_note = self.note
                record.resumed = self.resume
            except Exception as exc:  # noqa: BLE001
                record.operator_note = f"operator action failed: {exc}"
                record.resumed = False
        print(
            f"  [operator] {record.operator_note} "
            f"({len(record.navigations)} navigation(s), {record.duration_ms}ms)"
        )
        return record.resumed


@dataclass
class RefusingOperatorConsole:
    """An operator who looks and declines -- a core banking outage is nobody's
    clicking error. The run ends as a clean failure that still records the
    consultation."""

    note: str = "reviewed; not resolvable from the UI -- raising to the platform team"
    log: Any | None = None

    def handle(self, request: InterventionRequest) -> bool:
        print(request.render())
        print(f"  [operator] {self.note}")
        return False
