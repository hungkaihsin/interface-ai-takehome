"""A browser backend for `Surface`, built on Playwright.

The only file in the project that knows browsers exist. Swapping it for a desktop
backend means satisfying the same protocol against an OS accessibility API; nothing
above this file changes, and no stored artifact needs re-recording.

Two implementation choices worth defending:

**Everything goes through `get_by_role`.** Playwright offers `locator("css=...")`
and it would often be shorter. It is never used here, because a CSS selector that
works would tempt the discovery loop into recording one, and a stored artifact full
of CSS is one that can never run against a desktop app. The constraint is enforced
by not providing the capability.

**Resolution is strict about uniqueness.** Playwright's own convention is that a
locator matching several elements resolves to the first on action. That default is
rejected here: a strategy that matches three rows has not identified a control, and
acting on the first one is how automation reads the wrong member's balance without
ever raising an error.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import (
    Browser,
    Error as PWError,
    Frame,
    Locator as PWLocator,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from capability.locator import (
    Locator,
    RoleInContainerStrategy,
    RoleNameStrategy,
    RoleOrdinalStrategy,
)
from capability.schema import Condition, ElementPresent, TextPresent, UrlMatches

from .base import (
    FrameNotFound,
    FramePerception,
    Perception,
    Resolution,
    StrategyAttempt,
    SurfaceError,
)

DEFAULT_ACTION_TIMEOUT_MS = 8_000

#: Conditions are evaluated inside polling loops, so a missing frame must fail fast.
#: Waiting the full action timeout on every poll turns a 12-second settle budget into
#: minutes of dead time -- measured, not theorised.
CONDITION_FRAME_TIMEOUT_MS = 1_200


class WebSurface:
    """A live browser session that satisfies `Surface`."""

    def __init__(
        self,
        headless: bool = True,
        action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
        evidence_dir: str | Path = "evidence",
    ) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self.headless = headless
        self.action_timeout_ms = action_timeout_ms
        self.evidence_dir = Path(evidence_dir)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "WebSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._page = context.new_page()
        self._page.set_default_timeout(self.action_timeout_ms)
        return self

    def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - teardown must not mask a result
                    pass
        self._page = self._browser = self._pw = None

    def __enter__(self) -> "WebSurface":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise SurfaceError("surface not started; call start() first")
        return self._page

    # -- frames -----------------------------------------------------------

    def _frame(self, frame_path: list[str], timeout_ms: int = 5_000) -> Frame:
        """Walk the frame path from the top document inward, waiting for it to exist.

        Walked by name at each level rather than looked up globally, because two
        frames in different parts of a legacy console can share a name and a global
        lookup would return whichever loaded first.

        The wait is not optional. A frame-based app tears down and rebuilds its
        child frames on every navigation, so for a short window after any click the
        named frame either does not exist yet or exists but is detached. Resolving
        against that window produces "Frame was detached" -- a transient condition,
        not a broken flow -- so we poll for a live frame instead of failing on the
        first look. Detached frames are filtered out explicitly rather than trusted
        to disappear, because Playwright keeps returning them for a moment.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        seen: list[str] = []
        while True:
            frame = self.page.main_frame
            found = True
            for name in frame_path:
                children = [f for f in frame.child_frames if not f.is_detached()]
                seen = [f.name for f in children]
                match = next((f for f in children if f.name == name), None)
                if match is None:
                    found = False
                    break
                frame = match
            if found and not frame.is_detached():
                return frame
            if time.monotonic() >= deadline:
                raise FrameNotFound(frame_path, seen)
            time.sleep(0.1)

    @staticmethod
    def _count(candidate: PWLocator, retries: int = 2) -> int:
        """Count matches, tolerating a frame detaching mid-count.

        Same transient as above, caught one level lower: the frame can survive the
        lookup and detach before the count returns.
        """
        for attempt in range(retries + 1):
            try:
                return candidate.count()
            except PWError as exc:
                if "detached" not in str(exc).lower() or attempt == retries:
                    raise
                time.sleep(0.2)
        return 0

    # -- locator resolution ----------------------------------------------

    def _candidate(self, frame: Frame, strategy: object) -> tuple[PWLocator, str]:
        """Turn one stored strategy into a live Playwright locator."""
        if isinstance(strategy, RoleNameStrategy):
            loc = frame.get_by_role(
                strategy.role, name=strategy.name, exact=strategy.name_match == "exact"
            )
            return loc, f"{strategy.role}[name={strategy.name!r}]"

        if isinstance(strategy, RoleInContainerStrategy):
            c = strategy.container
            containers = frame.get_by_role(
                c.role, name=c.name, exact=c.name_match == "exact"
            )
            if c.index is not None:
                container = containers.nth(c.index)
            else:
                found = self._count(containers)
                if found != 1:
                    raise _NotUnique(
                        f"container {c.role}[name~={c.name!r}] matched {found}"
                    )
                container = containers.first
            inner = container.get_by_role(strategy.role)
            if strategy.index is not None:
                inner = inner.nth(strategy.index)
            desc = (
                f"{strategy.role} in {c.role}[name~={c.name!r}]"
                + (f"[{strategy.index}]" if strategy.index is not None else "")
            )
            return inner, desc

        if isinstance(strategy, RoleOrdinalStrategy):
            loc = frame.get_by_role(strategy.role).nth(strategy.index)
            return loc, f"{strategy.role}[{strategy.index}]"

        raise SurfaceError(f"unsupported locator strategy: {type(strategy).__name__}")

    def resolve(self, locator: Locator, frame_timeout_ms: int = 5_000) -> Resolution:
        result = Resolution(locator=locator)
        try:
            frame = self._frame(locator.frame_path, timeout_ms=frame_timeout_ms)
        except FrameNotFound as exc:
            result.attempts.append(
                StrategyAttempt(
                    kind="frame", described="/".join(locator.frame_path) or "(top)",
                    match_count=0, note=str(exc),
                )
            )
            return result

        for strategy in locator.strategies:
            try:
                candidate, described = self._candidate(frame, strategy)
            except _NotUnique as exc:
                result.attempts.append(
                    StrategyAttempt(kind=strategy.kind, described=str(exc),
                                    match_count=2, note="ambiguous container")
                )
                continue

            try:
                count = self._count(candidate)
            except Exception as exc:  # noqa: BLE001 - a bad strategy must not abort the ladder
                result.attempts.append(
                    StrategyAttempt(kind=strategy.kind, described=described,
                                    match_count=0, note=f"error: {exc}")
                )
                continue

            # An explicit index has already narrowed the selection, so one match
            # is expected; without one, several matches means the description is
            # no longer specific enough and we must not guess.
            if count == 1:
                result.attempts.append(
                    StrategyAttempt(strategy.kind, described, count, chosen=True)
                )
                result.handle = candidate
                return result

            result.attempts.append(
                StrategyAttempt(
                    strategy.kind, described, count,
                    note="ambiguous - refusing to guess" if count > 1 else "no match",
                )
            )

        return result

    # -- actions ----------------------------------------------------------

    def _act(self, locator: Locator, verb: str, *args: object) -> Resolution:
        resolution = self.resolve(locator)
        if not resolution.resolved:
            return resolution
        handle: PWLocator = resolution.handle  # type: ignore[assignment]
        getattr(handle, verb)(*args, timeout=self.action_timeout_ms)
        return resolution

    def goto(self, url: str) -> None:
        # "load" rather than "domcontentloaded": this app puts every screen
        # inside iframes, and domcontentloaded fires before they exist.
        self.page.goto(url, wait_until="load")

    def click(self, locator: Locator) -> Resolution:
        return self._act(locator, "click")

    def fill(self, locator: Locator, value: str) -> Resolution:
        return self._act(locator, "fill", value)

    def select(self, locator: Locator, value: str) -> Resolution:
        resolution = self.resolve(locator)
        if resolution.resolved:
            handle: PWLocator = resolution.handle  # type: ignore[assignment]
            handle.select_option(value, timeout=self.action_timeout_ms)
        return resolution

    def press(self, locator: Locator, key: str) -> Resolution:
        return self._act(locator, "press", key)

    def text_of(self, locator: Locator) -> tuple[str | None, Resolution]:
        resolution = self.resolve(locator)
        if not resolution.resolved:
            return None, resolution
        handle: PWLocator = resolution.handle  # type: ignore[assignment]
        return handle.inner_text(timeout=self.action_timeout_ms), resolution

    # -- conditions -------------------------------------------------------

    def _frame_text(self, frame_path: list[str]) -> str:
        try:
            return self._frame(frame_path, timeout_ms=CONDITION_FRAME_TIMEOUT_MS).locator("body").inner_text(timeout=2_000)
        except (FrameNotFound, PWTimeout, Exception):  # noqa: BLE001
            return ""

    def _all_frame_texts(self) -> list[str]:
        texts: list[str] = []
        for frame in self.page.frames:
            try:
                texts.append(frame.locator("body").inner_text(timeout=2_000))
            except Exception:  # noqa: BLE001 - a detached frame is not an error here
                continue
        return texts

    def check(self, condition: Condition) -> bool:
        if isinstance(condition, TextPresent):
            haystacks = (
                self._all_frame_texts()
                if condition.scope == "any_frame"
                else [self._frame_text(condition.frame_path)]
            )
            return any(self._text_matches(condition, h) for h in haystacks)

        if isinstance(condition, ElementPresent):
            return self.resolve(
                condition.locator, frame_timeout_ms=CONDITION_FRAME_TIMEOUT_MS
            ).resolved

        if isinstance(condition, UrlMatches):
            try:
                url = self._frame(
                    condition.frame_path, timeout_ms=CONDITION_FRAME_TIMEOUT_MS
                ).url
            except FrameNotFound:
                return False
            return re.search(condition.pattern, url) is not None

        raise SurfaceError(f"unsupported condition: {type(condition).__name__}")

    @staticmethod
    def _text_matches(condition: TextPresent, haystack: str) -> bool:
        if condition.match == "regex":
            return re.search(condition.text, haystack) is not None
        if condition.match == "exact":
            return condition.text.strip() == haystack.strip()
        return condition.text.lower() in haystack.lower()

    def wait_until_absent(self, condition: Condition, timeout_ms: int) -> bool:
        """Poll until a condition stops holding.

        The mirror of `wait_for`, and needed for exactly one job: deciding whether
        a remedy actually cleared an obstruction. Checking once immediately after
        clicking "Acknowledge" reads the old screen, because the navigation it
        triggers has not landed yet -- which made a working remedy look like a
        failed one and burned a second attempt against the retry budget.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if not self.check(condition):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.15)

    def wait_for(self, condition: Condition, timeout_ms: int) -> bool:
        """Poll a condition until it holds or the budget runs out.

        Polling rather than an event subscription because the conditions are
        composed from stored data, not from a selector Playwright can watch. The
        cost is a short delay before noticing; the benefit is that waiting works
        identically for every condition type, including on a desktop backend that
        has no event stream at all.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self.check(condition):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.15)

    # -- observation ------------------------------------------------------

    def perceive(self) -> Perception:
        frames: list[FramePerception] = []
        for frame in self.page.frames:
            path = _frame_path_of(frame)
            try:
                aria = frame.locator("body").aria_snapshot(timeout=3_000)
                text = frame.locator("body").inner_text(timeout=3_000)
            except Exception:  # noqa: BLE001 - skip frames that detached mid-read
                continue
            frames.append(
                FramePerception(
                    path=path, url=frame.url, title=frame.title(), aria=aria, text=text
                )
            )
        return Perception(frames=frames)

    def screenshot(self, name: str) -> str | None:
        """Capture a full-page PNG. Best-effort: evidence must never break a run."""
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self.evidence_dir / f"{name}.png"
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001
            return None

    def current_url(self) -> str:
        return self.page.url


class _NotUnique(Exception):
    """Internal: a container scope matched something other than exactly one node."""


def _frame_path_of(frame: Frame) -> list[str]:
    """Reconstruct a frame's name path back to the top document."""
    path: list[str] = []
    node: Frame | None = frame
    while node is not None and node.parent_frame is not None:
        path.append(node.name)
        node = node.parent_frame
    return list(reversed(path))
