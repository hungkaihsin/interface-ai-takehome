"""Turning a successful discovery run into a capability artifact.

The transcript is a conversation; the artifact is a contract. What crosses over is
only what replay needs -- the actions that worked, their locators, the values that
turned out to be parameters, and the values that turned out to be outputs.

Parameterisation is by value matching at record time, so a concrete member number
is substituted out rather than scrubbed later. Reads become outputs, not steps.

Discovered artifacts are drafts: discovery sees only the happy path, so it cannot
know which steps are irreversible or which business outcomes exist. A machine
proposes; a human classifies risk and approves.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from capability.locator import Locator
from capability.schema import (
    CapabilityArtifact,
    Checkpoint,
    ClickAction,
    ElementPresent,
    Extraction,
    FillAction,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    Policy,
    Provenance,
    SelectAction,
    Step,
    SurfaceBinding,
    Transform,
    ValueType,
)

from .agent import DiscoveryResult

REVIEW_NOTE = (
    "DRAFT recorded by an LLM discovery run. Before unattended use a reviewer must "
    "(1) classify any irreversible steps as risky, (2) declare the business "
    "outcomes this flow can legitimately return, and (3) set approved_for_unattended."
)


def _looks_like_money(text: str) -> bool:
    cleaned = text.replace(",", "").replace("$", "").strip()
    try:
        float(cleaned)
    except ValueError:
        return False
    return "." in cleaned


def _infer_type(text: str) -> ValueType:
    """Guess from the one sample observed. A convenience for the reviewer to
    correct, not a claim -- which is why it lands in a draft."""
    return "money" if _looks_like_money(text) else "string"


def to_artifact(
    result: DiscoveryResult,
    capability_id: str,
    surface: SurfaceBinding,
    params: list[ParamSpec],
    param_values: dict[str, str],
    allowed_origins: list[str],
    version: int = 1,
    name: str | None = None,
) -> CapabilityArtifact:
    if not result.succeeded:
        raise ValueError("refusing to record an artifact from an unsuccessful run")

    # Reverse map from the concrete value used during discovery back to its
    # parameter name, so typed values become templates.
    by_value = {str(v): k for k, v in param_values.items()}

    steps: list[Step] = []
    counter = 0

    # Discovery starts with the browser already on the entry screen, so the model
    # never navigates there and the trajectory silently depends on where it began.
    # At replay time nothing guarantees that, so the flow must establish its own
    # starting state.
    first = result.actions[0] if result.actions else None
    if not (first and first.kind == "navigate" and first.url == surface.entry_url):
        counter += 1
        steps.append(
            Step(
                id=f"s{counter}",
                intent="Open the application at its entry point.",
                action=NavigateAction(url=surface.entry_url),
                risk="safe",
            )
        )

    for action in result.actions:
        if action.kind == "read":
            continue
        counter += 1
        step_id = f"s{counter}"

        if action.kind == "navigate":
            steps.append(
                Step(
                    id=step_id,
                    intent=action.intent,
                    action=NavigateAction(url=action.url or ""),
                    risk="safe",
                )
            )
        elif action.kind == "click":
            assert action.locator is not None
            steps.append(
                Step(
                    id=step_id,
                    intent=action.intent,
                    action=ClickAction(target=action.locator),
                    risk="safe",
                )
            )
        elif action.kind in ("fill", "select"):
            assert action.locator is not None
            raw = action.value or ""
            templated = f"{{{{ {by_value[raw]} }}}}" if raw in by_value else raw
            build = FillAction if action.kind == "fill" else SelectAction
            steps.append(
                Step(
                    id=step_id,
                    intent=(
                        action.intent.replace(repr(raw), f"the {by_value[raw]}")
                        if raw in by_value
                        else action.intent
                    ),
                    action=build(target=action.locator, value=templated),
                    risk="safe",
                )
            )

    outputs: list[OutputSpec] = []
    for action in result.actions:
        if action.kind != "read" or action.locator is None or not action.output_name:
            continue
        observed = action.observed_text or ""
        action.locator = canonicalise_output_locator(action.locator, observed)
        value_type = _infer_type(observed)
        outputs.append(
            OutputSpec(
                name=action.output_name,
                type=value_type,
                description=f"Discovered from {action.locator.describes}.",
                source=Extraction(
                    locator=action.locator,
                    transform=[Transform(kind="strip")]
                    + ([Transform(kind="to_number")] if value_type == "money" else []),
                ),
            )
        )

    success = _success_checkpoint(result, outputs)

    return CapabilityArtifact(
        capability_id=capability_id,
        version=version,
        name=name or (result.summary or capability_id),
        description=result.summary or result.goal,
        surface=surface,
        inputs=params,
        outputs=outputs,
        steps=steps,
        success=success,
        business_outcomes=[],  # not discoverable from one happy-path run; see REVIEW_NOTE
        recoverables=[],
        policy=Policy(
            allowed_origins=allowed_origins,
            has_irreversible_steps=False,
            approved_for_unattended=False,
        ),
        provenance=Provenance(
            discovered_at=datetime.now(timezone.utc),
            model=result.model,
            discovery_run_id=result.run_id,
            llm_turns=result.turns,
            notes=REVIEW_NOTE,
        ),
    )


def canonicalise_output_locator(locator: Locator, observed: str) -> Locator:
    """Strip strategies that identify a value by the value itself.

    Asked to read a balance of 18432.19, a model will readily target
    `cell name "18432.19"` -- perfect for the member it was recorded on, useless
    for anyone else. Fixed here rather than in the prompt: guidance is advice, and
    this failure is invisible until a different customer is queried.

    If every strategy is self-referential the locator is left alone -- a bad
    locator that is detected and reported beats one we quietly emptied.
    """
    observed = (observed or "").strip()
    if not observed:
        return locator

    kept = [
        strategy
        for strategy in locator.strategies
        if observed not in (getattr(strategy, "name", "") or "")
    ]
    if not kept:
        return locator

    # A container named by the row's whole text is just as member-specific as a
    # cell named by its value. Narrow it to the token describing what the row *is*
    # rather than what it holds. A heuristic, and safe because `--verify-with`
    # replays against a different input and catches a wrong guess in seconds.
    narrowed: list[object] = []
    for strategy in kept:
        container = getattr(strategy, "container", None)
        if container is not None and observed in (container.name or ""):
            token = _stable_token(container.name)
            if token:
                strategy = strategy.model_copy(
                    update={"container": container.model_copy(update={"name": token})}
                )
        narrowed.append(strategy)

    if narrowed == locator.strategies:
        return locator
    return locator.model_copy(update={"strategies": narrowed})


def _stable_token(name: str) -> str | None:
    """The longest purely alphabetic word in a row name, e.g. 'SAVINGS'.

    Words with digits are rejected: account numbers, balances and dates are exactly
    what differs between members.
    """
    words = [w.strip(",:;()") for w in (name or "").split()]
    alphabetic = [w for w in words if w.isalpha() and len(w) >= 4]
    return max(alphabetic, key=len) if alphabetic else None


def self_referential_locators(result: DiscoveryResult) -> list[str]:
    """Report locators that identify a value by the value itself.

    Worth detecting because the mistake produces a *passing* discovery run --
    nothing looks wrong until the capability is invoked for someone else.
    """
    problems: list[str] = []
    for action in result.actions:
        if action.kind != "read" or action.locator is None:
            continue
        observed = (action.observed_text or "").strip()
        if not observed:
            continue
        for strategy in action.locator.strategies:
            name = getattr(strategy, "name", "") or ""
            container = getattr(strategy, "container", None)
            container_name = getattr(container, "name", "") if container else ""
            if observed and (observed in name or observed in container_name):
                problems.append(
                    f"output {action.output_name!r} is targeted by a locator that "
                    f"contains its own observed value {observed!r}"
                )
    return problems


def _success_checkpoint(
    result: DiscoveryResult, outputs: list[OutputSpec]
) -> Checkpoint:
    """Assert on the presence of the thing we came for.

    The model supplies prose; the checkable condition is that the first declared
    output is readable. Prose alone would "succeed" whenever the model felt good.
    """
    description = (
        result.success_condition or "The flow reached its goal."
    ).strip()
    if outputs:
        return Checkpoint(
            condition=ElementPresent(locator=outputs[0].source.locator),
            description=description,
            timeout_ms=10_000,
        )
    raise ValueError(
        "the run declared success but read no values, so there is nothing to assert"
    )


def save(artifact: CapabilityArtifact, directory: str | Path = "artifacts") -> Path:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{artifact.capability_id}.v{artifact.version}.json"
    path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2) + "\n")
    return path
