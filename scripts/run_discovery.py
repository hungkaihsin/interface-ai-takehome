"""Run a real LLM discovery pass and record the result as a capability.

    .venv/bin/python -m scripts.run_discovery \
        --goal "Look up member 10001 and read their current savings balance" \
        --param member_id=10001 \
        --capability member_savings_lookup_discovered

Needs GOOGLE_API_KEY. This is the one command in the project that costs money and
the one whose output is not reproducible byte-for-byte -- which is exactly why the
artifact it emits is handed to a deterministic engine afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from capability.schema import ParamSpec, SurfaceBinding
from discovery.agent import DiscoveryAgent, QuotaExhausted
from discovery.recorder import save, self_referential_locators, to_artifact
from surface.session import MeridianSessionProvider
from surface.web import WebSurface

BASE = "http://127.0.0.1:5001"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A parameter this run exercises. Its value becomes {{ NAME }} in the artifact.",
    )
    parser.add_argument("--capability", default="member_savings_lookup_discovered")
    parser.add_argument("--entry", default=f"{BASE}/shell")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--verify-with",
        metavar="NAME=VALUE",
        help="After recording, replay the draft with a DIFFERENT value to test "
             "whether it generalises. Strongly recommended.",
    )
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set. Put it in .env (see .env.example).")
        return 2

    param_values: dict[str, str] = {}
    for item in args.param:
        key, _, value = item.partition("=")
        param_values[key.strip()] = value.strip()

    params = [
        ParamSpec(
            name=name,
            type="string",
            description=f"Value supplied per invocation (discovered as {name}).",
            example=value,
        )
        for name, value in param_values.items()
    ]

    with WebSurface(headless=not args.headed) as surface:
        # Signing on is the platform's job, not the capability's -- so it happens
        # before discovery starts and never becomes a recorded step.
        MeridianSessionProvider(base_url=BASE).establish(surface)

        agent = DiscoveryAgent(
            surface=surface,
            allowed_origins=[BASE],
            model=args.model,
        )
        print(f"\ngoal: {args.goal}\n")
        try:
            result = agent.discover(goal=args.goal, entry_url=args.entry)
        except QuotaExhausted:
            # Actionable rather than cryptic: a daily quota is not something the
            # user can wait out in this session, so say what to do instead.
            print(
                f"\n  The free-tier DAILY quota for {args.model!r} is spent.\n"
                "  Backing off will not help -- daily quotas reset at midnight "
                "Pacific.\n\n"
                "  Options:\n"
                "    1. Re-run with a model that has its own quota:\n"
                "         --model gemini-2.5-flash-lite\n"
                "    2. Wait for the reset.\n"
                "    3. Skip discovery entirely -- a recorded run is already in\n"
                "       evidence/, and replay never needs a key:\n"
                "         .venv/bin/python -m scripts.demo_replay --all\n"
            )
            return 2

        print(f"\n  succeeded  : {result.succeeded}")
        print(f"  turns      : {result.turns}")
        print(f"  stopped    : {result.stopped_because}")
        print(f"  outputs    : {result.outputs}")
        print(f"  log        : {result.log_path}")

        if not result.succeeded:
            print(f"\n  give-up reason: {result.give_up_reason}")
            return 1

        artifact = to_artifact(
            result=result,
            capability_id=args.capability,
            surface=SurfaceBinding(
                kind="web",
                app_id="meridian-backoffice-v4",
                tenant_id="meridian-cu",
                entry_url=args.entry,
            ),
            params=params,
            param_values=param_values,
            allowed_origins=[BASE],
        )
        path = save(artifact)
        print(f"\n  artifact   : {path}")
        print(f"  steps      : {[s.action.kind for s in artifact.steps]}")
        print(f"  inputs     : {[p.name for p in artifact.inputs]}")
        print(f"  outputs    : {[(o.name, o.type) for o in artifact.outputs]}")
        print(f"  approved   : {artifact.policy.approved_for_unattended}  (drafts are not)")

        # No concrete argument may survive into the executable part of the file.
        # `inputs[].example` is excluded deliberately: it is catalogue
        # documentation for a calling agent, and in production it would be a
        # synthetic sample rather than a real member's number.
        executable = json.dumps(
            {
                "steps": artifact.model_dump(mode="json")["steps"],
                "outputs": artifact.model_dump(mode="json")["outputs"],
            }
        )
        for value in param_values.values():
            leaked = value in executable
            print(
                f"  concrete value {value!r} in steps/outputs: "
                f"{'!! LEAKED !!' if leaked else 'absent (parameterised)'}"
            )

        problems = self_referential_locators(result)
        if problems:
            print("\n  ** LOCATOR STABILITY WARNINGS **")
            for problem in problems:
                print(f"    - {problem}")

        if args.verify_with:
            _verify(surface, artifact, args.verify_with)

    return 0


def _verify(surface, artifact, verify_arg: str) -> None:
    """Replay the fresh draft with a DIFFERENT argument, deterministically.

    This is the check that a discovery run cannot perform on itself. A recorded
    flow always replays for the case it was recorded on; the only question that
    matters is whether it works for a different one. Running it here, immediately,
    turns "the model produced something" into "the model produced something that
    generalises" -- or catches, in seconds, the class of overfitting that would
    otherwise surface on a real member's account.
    """
    from replay.engine import ReplayEngine

    name, _, value = verify_arg.partition("=")
    print("\n" + "-" * 70)
    print(f"  VERIFICATION: replaying the draft with {name}={value} (no model)")
    print("-" * 70)

    engine = ReplayEngine(surface, session_provider=MeridianSessionProvider(base_url=BASE))
    result = engine.run(artifact, {name.strip(): value.strip()})
    print(f"  status   : {result.status.value}")
    print(f"  outputs  : {result.outputs}")
    if result.error:
        print(f"  failure  : {result.error.kind}")
        print(f"  expected : {result.error.expected}")
        print(f"  observed : {result.error.observed[:200]}")
        print(
            "\n  VERDICT: the draft does NOT generalise. It replays for the member "
            "it was\n  recorded on and fails for another, so it must not be approved."
        )
    else:
        print(
            "\n  VERDICT: the draft generalises to a different input. It is still a "
            "draft --\n  a reviewer must still declare business outcomes and classify risk."
        )


if __name__ == "__main__":
    sys.exit(main())
