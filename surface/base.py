"""The surface abstraction: how we perceive and act, separated from what was recorded.

Nothing above this module imports Playwright or knows what a DOM is. Controls are
addressed by accessibility role, name and containment -- a vocabulary that also
exists on Windows UI Automation and macOS AX, so a desktop backend is a second
implementation of this protocol rather than a change to the artifact schema.

CSS, XPath and pixel coordinates are absent by design: admitting them here would let
browser assumptions reach stored artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from capability.locator import Locator
from capability.schema import Condition


class SurfaceError(Exception):
    """A problem at the surface layer itself, not in the recorded flow."""


class FrameNotFound(SurfaceError):
    def __init__(self, frame_path: list[str], available: list[str]) -> None:
        self.frame_path = frame_path
        self.available = available
        super().__init__(
            f"no frame at path {frame_path!r}; available at that level: {available!r}"
        )


@dataclass
class StrategyAttempt:
    """One locator strategy tried, and what it matched."""

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
        """Several matches, none chosen.

        Reported separately from "not found" because the two mean opposite things:
        nothing matched suggests the screen is wrong, several matched suggests our
        description is no longer specific enough.
        """
        return not self.resolved and any(a.match_count > 1 for a in self.attempts)

    def explain(self) -> str:
        """The whole ladder that was walked, for a debuggable failure message."""
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
    #: Accessibility tree as indented text -- what the LLM reads. Not HTML, not a
    #: screenshot: compact, and it names controls the way the artifact will.
    aria: str
    text: str


@dataclass
class Perception:
    """A full observation of the surface at one moment."""

    frames: list[FramePerception]
    screenshot_path: str | None = None

    def frame(self, path: list[str]) -> FramePerception | None:
        return next((f for f in self.frames if f.path == path), None)

    def render(self, max_chars_per_frame: int = 6_000) -> str:
        """Flatten to the text an LLM sees, truncated to keep prompts bounded."""
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
    """Everything the layers above may do to a live application.

    Kept small: each verb is one more thing a desktop backend must implement.
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
