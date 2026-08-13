"""The surface abstraction -- the seam between *how we perceive and act* and *what flow was recorded*.

This is the file that answers requirement 3.7. Everything above it (the discovery
loop, the replay engine, the escalation machinery) talks only to this interface.
Nothing above it imports Playwright, mentions a browser, or knows what a DOM is.

The bet being made: a control can be addressed by **role, accessible name, and
containment**, and that vocabulary is not browser-specific. Windows UI Automation
exposes ControlType, Name and a tree; macOS AX exposes AXRole, AXTitle and
AXChildren. So extending to a desktop application means writing a second class
that satisfies this protocol -- not touching the artifact schema, the replay
engine, or the error taxonomy.

What is deliberately *not* in this interface:

  * No CSS or XPath. Those are browser-only, so admitting them here would let
    browser assumptions leak into stored artifacts and quietly kill the desktop
    story.
  * No pixel coordinates. A screenshot-driven model would produce them naturally,
    but they cannot survive a window resize, so they must not reach an artifact.
  * No `sleep`. Waiting is expressed as waiting *for a condition*; a duration is
    a guess about someone else's machine.

The one thing this interface does concede to reality is `frame_path`, because a
document that contains other documents is not a browser quirk -- it is the same
shape as a desktop window containing panes, and pretending it away would make
the abstraction dishonest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from capability.locator import Locator
from capability.schema import Condition


class SurfaceError(Exception):
    """Something went wrong at the surface layer itself, not in the flow."""


class FrameNotFound(SurfaceError):
    def __init__(self, frame_path: list[str], available: list[str]) -> None:
        self.frame_path = frame_path
        self.available = available
        super().__init__(
            f"no frame at path {frame_path!r}; available at that level: {available!r}"
        )


@dataclass
class StrategyAttempt:
    """One locator strategy tried, and what it matched.

    Kept per attempt so a resolution failure can report the whole ladder it walked
    rather than only the last rung. When replay breaks six months from now, "tried
    role+name 'Search' (0 matches), then row-scoped (3 matches, ambiguous)" is the
    difference between a five-minute fix and an afternoon.
    """

    kind: str
    described: str
    match_count: int
    chosen: bool = False
    note: str | None = None


@dataclass
class Resolution:
    """The outcome of trying to find one control."""

    locator: Locator
    attempts: list[StrategyAttempt] = field(default_factory=list)
    handle: object | None = None

    @property
    def resolved(self) -> bool:
        return self.handle is not None

    @property
    def ambiguous(self) -> bool:
        """True when a strategy matched several elements and none was chosen.

        Surfaced separately from "not found" because the two demand opposite
        responses: nothing matched usually means the screen is not what we
        expected, while several matched means the screen is fine and our
        description of the control is no longer specific enough.
        """
        return not self.resolved and any(a.match_count > 1 for a in self.attempts)

    def explain(self) -> str:
        if not self.attempts:
            return "no strategies were attempted"
        return "; ".join(
            f"{a.kind}({a.described}) -> {a.match_count} match(es)"
            + (f" [{a.note}]" if a.note else "")
            for a in self.attempts
        )


@dataclass
class FramePerception:
    """What one frame currently looks like."""

    path: list[str]
    url: str
    title: str
    #: The accessibility tree, rendered as indented text. This is what the LLM
    #: reads during discovery -- not HTML, and not a screenshot. It is compact,
    #: it names controls the way the artifact will name them, and it contains no
    #: styling noise.
    aria: str
    #: Visible text, used for the cheap text-presence conditions that carry most
    #: of the outcome detection on a legacy app.
    text: str


@dataclass
class Perception:
    """A full observation of the surface at one moment."""

    frames: list[FramePerception]
    #: Populated only when something went wrong. Screenshots are the "richer
    #: signal on failure" of requirement 3.5, and they are expensive and can
    #: contain regulated data, so they are not taken on the happy path.
    screenshot_path: str | None = None

    def frame(self, path: list[str]) -> FramePerception | None:
        return next((f for f in self.frames if f.path == path), None)

    def render(self, max_chars_per_frame: int = 6_000) -> str:
        """Flatten to the text an LLM sees. Truncated to keep prompts bounded."""
        parts: list[str] = []
        for f in self.frames:
            label = "/".join(f.path) if f.path else "(top document)"
            body = f.aria[:max_chars_per_frame]
            if len(f.aria) > max_chars_per_frame:
                body += "\n... [truncated]"
            parts.append(f"=== FRAME {label} === url={f.url}\n{body}")
        return "\n\n".join(parts)


@runtime_checkable
class Surface(Protocol):
    """Everything the layers above are allowed to do to a live application.

    Small on purpose. Each additional verb here is one more thing a future
    desktop backend must implement, so the set is kept to what the artifact
    schema can actually express.
    """

    def goto(self, url: str) -> None: ...

    def resolve(self, locator: Locator) -> Resolution: ...

    def click(self, locator: Locator) -> Resolution: ...

    def fill(self, locator: Locator, value: str) -> Resolution: ...

    def select(self, locator: Locator, value: str) -> Resolution: ...

    def press(self, locator: Locator, key: str) -> Resolution: ...

    def text_of(self, locator: Locator) -> tuple[str | None, Resolution]: ...

    def check(self, condition: Condition) -> bool: ...

    def wait_for(self, condition: Condition, timeout_ms: int) -> bool: ...

    def wait_until_absent(self, condition: Condition, timeout_ms: int) -> bool: ...

    def perceive(self) -> Perception: ...

    def screenshot(self, path: str) -> str | None: ...

    def current_url(self) -> str: ...
