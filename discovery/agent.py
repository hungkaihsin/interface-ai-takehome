"""The LLM observe / decide / act loop.

The only part of the system that calls a model, and the only non-deterministic
part. Everything it produces is handed to code that is neither.

The model reads accessibility trees, not screenshots, so the action it picks is
already expressible as a stored locator -- a vision loop returns coordinates, which
cannot survive into an artifact. Safety sits between decide and act: a refused tool
call is fed back as an observation so the model re-routes rather than dying.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types

from capability.locator import (
    ContainerScope,
    Locator,
    RoleInContainerStrategy,
    RoleNameStrategy,
)
from observability.log import RunLog, new_run_id
from observability.redaction import Redactor
from surface.base import Surface

from .tools import SYSTEM_INSTRUCTION, TOOLSET

DEFAULT_MODEL = "gemini-2.5-flash"

#: Generous enough for a multi-screen flow, tight enough that a confused model
#: cannot spend the afternoon clicking.
MAX_TURNS = 24
MAX_CONSECUTIVE_ERRORS = 4
MAX_WALL_SECONDS = 600

#: The free tier's per-minute window is short, so a few doubling waits clear it.
RETRY_ATTEMPTS = 5
RETRY_BASE_SECONDS = 8


class QuotaExhausted(RuntimeError):
    """Quota spent for the period; waiting will not help.

    Both this and a rate limit arrive as HTTP 429, but a per-minute cap clears in
    seconds and a per-day cap clears at midnight. The same recoverable-versus-hard
    distinction the result contract draws, applied to our own dependency.
    """


def classify_provider_error(message: str) -> tuple[str, int | None]:
    """Sort into rate_limited / quota_exhausted / other, with the provider's own
    suggested wait when it supplied one."""
    if "PerDay" in message:
        return "quota_exhausted", None

    if any(
        marker in message
        for marker in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500")
    ):
        match = re.search(r"retryDelay\D{0,6}(\d+)s", message)
        return "rate_limited", int(match.group(1)) if match else 0

    return "other", None


@dataclass
class RecordedAction:
    """One model action that actually worked, in artifact vocabulary."""

    kind: str
    locator: Locator | None
    value: str | None = None
    url: str | None = None
    output_name: str | None = None
    observed_text: str | None = None
    intent: str = ""


@dataclass
class DiscoveryResult:
    goal: str
    succeeded: bool
    run_id: str
    turns: int
    actions: list[RecordedAction] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    success_condition: str | None = None
    summary: str | None = None
    give_up_reason: str | None = None
    stopped_because: str = ""
    model: str = DEFAULT_MODEL
    log_path: str | None = None


class DiscoveryAgent:
    def __init__(
        self,
        surface: Surface,
        allowed_origins: list[str],
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        evidence_dir: str = "evidence",
        echo: bool = True,
    ) -> None:
        key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Discovery needs a model; replay does not."
            )
        self.client = genai.Client(api_key=key)
        self.surface = surface
        self.allowed_origins = allowed_origins
        self.model = model
        self.evidence_dir = evidence_dir
        self.echo = echo

    # -- the loop ---------------------------------------------------------

    def discover(self, goal: str, entry_url: str) -> DiscoveryResult:
        run_id = new_run_id("discovery")
        log = RunLog(run_id, self.evidence_dir, Redactor(), echo=False)
        log.event("discovery.start", goal=goal, entry_url=entry_url, model=self.model)

        self._assert_allowed(entry_url)
        self.surface.goto(entry_url)

        history: list[types.Content] = [
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            f"GOAL: {goal}\n\n"
                            f"You are at {entry_url}.\n\n"
                            f"{self._observation()}"
                        )
                    )
                ],
            )
        ]

        result = DiscoveryResult(
            goal=goal, succeeded=False, run_id=run_id, turns=0, model=self.model
        )
        result.log_path = str(log.path)

        started = time.monotonic()
        consecutive_errors = 0

        for turn in range(1, MAX_TURNS + 1):
            result.turns = turn

            if time.monotonic() - started > MAX_WALL_SECONDS:
                result.stopped_because = f"wall-clock budget of {MAX_WALL_SECONDS}s"
                break
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                result.stopped_because = (
                    f"{consecutive_errors} consecutive unusable actions -- dead end"
                )
                break

            call = self._next_call(history, log, turn)
            if call is None:
                # Keep roles alternating, so one empty turn does not corrupt the
                # conversation and empty every turn after it.
                consecutive_errors += 1
                history.append(
                    types.Content(
                        role="model", parts=[types.Part(text="(no action taken)")]
                    )
                )
                history.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                text=(
                                    "You did not call a tool. Call exactly one tool "
                                    "now. If the goal is already achieved and every "
                                    "requested value has been read, call finish.\n\n"
                                    f"{self._observation()}"
                                )
                            )
                        ],
                    )
                )
                continue

            if call.name == "finish":
                args = dict(call.args or {})
                result.succeeded = True
                result.success_condition = args.get("success_condition")
                result.summary = args.get("summary")
                result.stopped_because = "model called finish"
                log.event("discovery.finish", **args)
                break

            if call.name == "give_up":
                args = dict(call.args or {})
                result.give_up_reason = args.get("reason")
                result.stopped_because = "model called give_up"
                log.event("discovery.give_up", **args)
                break

            outcome, recorded = self._execute(call, log, turn)
            if recorded is not None:
                result.actions.append(recorded)
                if recorded.output_name and recorded.observed_text is not None:
                    result.outputs[recorded.output_name] = recorded.observed_text
                consecutive_errors = 0
            else:
                consecutive_errors += 1

            # The new screen goes inside the function response, not a separate text
            # part: acting on a UI returns an observation, and mixing the two broke
            # call/response alternation and emptied every following turn.
            history.append(
                types.Content(role="model", parts=[types.Part(function_call=call)])
            )
            history.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=call.name,
                                response={
                                    "result": outcome,
                                    "screen_now": self._observation(),
                                },
                            )
                        )
                    ],
                )
            )
        else:
            result.stopped_because = f"reached the {MAX_TURNS}-turn limit"

        log.event(
            "discovery.end",
            succeeded=result.succeeded,
            turns=result.turns,
            stopped_because=result.stopped_because,
            outputs=result.outputs,
        )
        return result

    # -- model call --------------------------------------------------------

    def _next_call(
        self, history: list[types.Content], log: RunLog, turn: int
    ) -> types.FunctionCall | None:
        """One model turn, with backoff on transient provider failures.

        A rate limit is a recoverable condition, not a dead end. Without this the
        loop counted each 429 toward its failure budget and reported "dead end"
        for what was a free-tier quota window.
        """
        delay = RETRY_BASE_SECONDS
        for attempt in range(RETRY_ATTEMPTS):
            call, retry_after = self._attempt_call(history, log, turn, attempt)
            if retry_after is None:
                return call
            # The provider usually says how long to wait; its number beats ours.
            wait = retry_after if retry_after > 0 else delay
            log.event("model.rate_limited", turn=turn, attempt=attempt, sleeping=wait)
            if self.echo:
                print(f"  [turn {turn}] rate limited; waiting {wait}s")
            time.sleep(wait)
            delay *= 2
        return None

    def _attempt_call(
        self, history: list[types.Content], log: RunLog, turn: int, attempt: int
    ) -> tuple[types.FunctionCall | None, bool]:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=history,
                config=types.GenerateContentConfig(
                    tools=[TOOLSET],
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.0,
                    # Each turn is a small decision over a fully-rendered screen,
                    # not a puzzle, so reasoning bought nothing here.
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a provider hiccup is not a crash
            message = str(exc)
            classification, retry_after = classify_provider_error(message)
            log.event(
                "model.error",
                turn=turn,
                classification=classification,
                retry_after=retry_after,
                # Long enough to keep the quota metric and limit -- the part that
                # says what to do about it.
                error=message[:1200],
            )
            if classification == "quota_exhausted":
                # Retrying a daily quota is not resilience, it is four minutes of
                # pretending.
                raise QuotaExhausted(message) from exc
            return None, retry_after if classification == "rate_limited" else None

        candidates = response.candidates or []
        for candidate in candidates:
            for part in (candidate.content.parts or []) if candidate.content else []:
                if getattr(part, "function_call", None):
                    call = part.function_call
                    log.event(
                        "model.call",
                        turn=turn,
                        tool=call.name,
                        args=dict(call.args or {}),
                    )
                    if self.echo:
                        print(f"  [turn {turn}] {call.name}({dict(call.args or {})})")
                    return call, None

        text = (response.text or "").strip() if hasattr(response, "text") else ""
        log.event("model.no_tool_call", turn=turn, text=text[:400])
        if self.echo:
            print(f"  [turn {turn}] (no tool call) {text[:120]}")
        return None, None

    # -- execution ---------------------------------------------------------

    def _execute(
        self, call: types.FunctionCall, log: RunLog, turn: int
    ) -> tuple[str, RecordedAction | None]:
        args = dict(call.args or {})
        name = call.name

        try:
            if name == "navigate":
                url = str(args.get("url", ""))
                self._assert_allowed(url)
                self.surface.goto(url)
                return "navigated", RecordedAction(
                    kind="navigate", locator=None, url=url, intent=f"Open {url}."
                )

            locator = self._locator_from(args)
            if locator is None:
                return (
                    "ERROR: could not build a target from those arguments. Supply "
                    "frame and role, plus either name or container_name.",
                    None,
                )

            if name == "click":
                resolution = self.surface.click(locator)
                if not resolution.resolved:
                    return f"ERROR: no such control. {resolution.explain()}", None
                return "clicked", RecordedAction(
                    kind="click",
                    locator=locator,
                    intent=f"Click {locator.describes}.",
                )

            if name in ("fill", "select"):
                value = str(args.get("value", ""))
                act = self.surface.fill if name == "fill" else self.surface.select
                resolution = act(locator, value)
                if not resolution.resolved:
                    return f"ERROR: no such control. {resolution.explain()}", None
                return f"{name} ok", RecordedAction(
                    kind=name,
                    locator=locator,
                    value=value,
                    intent=f"Enter {value!r} into {locator.describes}."
                    if name == "fill"
                    else f"Choose {value!r} in {locator.describes}.",
                )

            if name == "read_text":
                text, resolution = self.surface.text_of(locator)
                if text is None:
                    return f"ERROR: no such control. {resolution.explain()}", None
                output_name = str(args.get("output_name") or "value")
                return f"read: {text!r}", RecordedAction(
                    kind="read",
                    locator=locator,
                    output_name=output_name,
                    observed_text=text,
                    intent=f"Read {output_name} from {locator.describes}.",
                )

            return f"ERROR: unknown tool {name!r}", None

        except PermissionError as exc:
            # Reported to the model rather than raised, so it can re-route. The
            # refusal is still in the log.
            log.event("safety.refused", turn=turn, tool=name, reason=str(exc))
            return f"REFUSED by safety policy: {exc}", None
        except Exception as exc:  # noqa: BLE001
            log.event("action.error", turn=turn, tool=name, error=str(exc)[:300])
            return f"ERROR: {type(exc).__name__}: {exc}", None

    # -- helpers -----------------------------------------------------------

    def _locator_from(self, args: dict[str, Any]) -> Locator | None:
        """Turn the model's arguments into a `Locator`.

        No interpretation step: a direct mapping onto the strategy tiers the schema
        already has, because the tools speak the same vocabulary.
        """
        frame = str(args.get("frame") or "").strip()
        frame_path = [] if frame in ("", "top", "(top)") else [frame]
        role = str(args.get("role") or "").strip()
        name = str(args.get("name") or "").strip()
        container_role = str(args.get("container_role") or "").strip()
        container_name = str(args.get("container_name") or "").strip()
        index = args.get("index")

        if not role:
            return None

        strategies: list[Any] = []
        if name:
            strategies.append(RoleNameStrategy(role=role, name=name))
        if container_name:
            strategies.append(
                RoleInContainerStrategy(
                    role=role,
                    container=ContainerScope(
                        role=container_role or "row",
                        name=container_name,
                        name_match="contains",
                    ),
                    index=int(index) if index is not None else None,
                )
            )
        if not strategies:
            return None

        described = name or f"{role} in {container_name!r}"
        return Locator(
            frame_path=frame_path,
            strategies=strategies,
            describes=f"the {described} {role}".replace("  ", " "),
        )

    def _assert_allowed(self, url: str) -> None:
        if not any(url.startswith(origin) for origin in self.allowed_origins):
            raise PermissionError(
                f"{url!r} is outside the allowlist {self.allowed_origins}"
            )

    def _observation(self) -> str:
        perception = self.surface.perceive()
        return "CURRENT SCREEN:\n" + perception.render()
