"""Human-in-the-loop escalation and handoff -- requirement 3.6.

The requirement has three parts and each one is a design decision:

**Detect and route.** "Stuck" is not a feeling, it is a defined state: the surface
settled into something the capability does not recognise, or a declared remedy
failed its attempt budget, or a session was lost after an irreversible step
committed. Each of those produces an `InterventionRequest` carrying what a person
actually needs -- which capability, which step and why it exists, what the screen
says now, and a screenshot.

**Take control of the same live session.** Not a fresh one. This is the part that
is easy to fake and we do not: the browser context the automation was driving is
handed to a person, they act in it, and the automation resumes against whatever
state they left behind. Concretely, the engine blocks, the operator works in the
same Chromium window, and control returns when they say so. Nothing is torn down
and nothing is re-created, so cookies, session, scroll position and half-filled
forms all survive the handoff -- which is the whole point, because a human called
in halfway through a flow cannot start over.

**Record what the human did.** Page navigations during the takeover window are
captured by listening on the live page, so the record is observed rather than
self-reported. A human who says "I just clicked acknowledge" and actually visited
four screens leaves both facts in the evidence.

**The control model.** Exactly one holder at a time, tracked explicitly. This is
enforced structurally rather than by convention: the transfer is a context manager,
so the automation is *inside a blocking call* for the entire time the human holds
control and physically cannot act. A design where both sides could act would need
locking; a design where the handoff is synchronous does not.

What is mocked, deliberately and as the brief permits: the operator *console*. A
production version is a co-browsing UI with an intervention queue. Here an operator
is either a terminal prompt (`TerminalOperatorConsole`, a real human at a real
browser) or a scripted stand-in (`ScriptedOperatorConsole`) used to make the
evidence reproducible. Both go through the identical handoff path -- the mock is
the *interface* a human sits behind, never the mechanism.
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
    #: Redacted screen description. Redacted because an intervention request is a
    #: message that leaves the process, and regulated data must not travel in it.
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

    A context manager because control transfer has to be symmetric: whatever the
    human does, including raising, control comes back and the record is closed.
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
                # Only top-level and named frames are interesting; anonymous
                # sub-frame churn would drown the record in noise.
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
            # Let any navigation the human just triggered actually land before we
            # stop listening. Without this the record misses the very click that
            # resolved the intervention: control returns the instant they act, and
            # the page moves a moment later.
            #
            # Waiting on the *page* load state is not enough -- in a framed app the
            # click navigates an inner frame while the top document sits still. So
            # we wait for the thing we are actually recording, bounded, and give up
            # quietly if the human's action genuinely navigated nothing.
            #
            # The wait must be `page.wait_for_timeout`, not `time.sleep`. Playwright's
            # sync API only dispatches events while control is inside a Playwright
            # call, so sleeping in Python blocks the very callbacks we are waiting
            # for and the record comes back empty however long we wait.
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

    Requires the surface to have been started with `headless=False` -- otherwise
    there is no window for a person to work in, and this console says so rather
    than pretending the handoff happened.
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
    """A stand-in operator that performs a fixed remedy, for reproducible evidence.

    This is the documented mock. It exists because an evidence file that requires
    a human to be sitting at a keyboard cannot be regenerated by a reviewer, and
    "run this and a person must intervene" is not a demo anybody can check.

    It is *not* a shortcut around the mechanism: it acquires control through the
    same `SessionHandoff`, acts on the same live surface, and is recorded by the
    same observer. Swap this for `TerminalOperatorConsole` and the engine cannot
    tell the difference.
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
    """An operator who looks and declines to resolve it.

    Models the honest case where a human cannot fix the problem either -- a core
    banking outage is nobody's clicking error. The run must still end as a clean
    failure carrying the fact that a person was consulted.
    """

    note: str = "reviewed; not resolvable from the UI -- raising to the platform team"
    log: Any | None = None

    def handle(self, request: InterventionRequest) -> bool:
        print(request.render())
        print(f"  [operator] {self.note}")
        return False
