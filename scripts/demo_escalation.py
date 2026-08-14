"""Demonstrate escalation and handoff.

Takes the real capability and removes its knowledge of the maintenance
interstitial, simulating the commonest way a capability goes stale: it was recorded
before anyone had seen that screen.

Two endings, because "a human was asked" has two honest ones:
  A  the human resolves it       -> SUCCESS, escalated=True
  B  the human cannot resolve it -> FAILURE, escalated=True  (member 50001)

    python -m scripts.demo_escalation [--headed]
"""

from __future__ import annotations

import argparse
import sys

from capability.locator import Locator, RoleNameStrategy
from capability.schema import CapabilityArtifact
from replay.engine import ReplayEngine
from replay.escalation import (
    RefusingOperatorConsole,
    ScriptedOperatorConsole,
    TerminalOperatorConsole,
)
from surface.session import MeridianSessionProvider
from surface.web import WebSurface

ARTIFACT = "artifacts/member_savings_lookup.v1.json"
ACKNOWLEDGE = Locator(
    frame_path=["main"],
    describes="the Acknowledge button on the maintenance notice",
    strategies=[RoleNameStrategy(role="button", name="Acknowledge")],
)


def artifact_without_interstitial_handling() -> CapabilityArtifact:
    """The same capability, recorded before the maintenance notice existed."""
    artifact = CapabilityArtifact.model_validate_json(open(ARTIFACT).read())
    artifact = artifact.model_copy(
        update={
            "recoverables": [
                r for r in artifact.recoverables if r.code != "MAINTENANCE_INTERSTITIAL"
            ]
        }
    )
    return artifact


def operator_acknowledges(surface) -> None:
    """What the human does: one click, on the session the automation was using."""
    resolution = surface.click(ACKNOWLEDGE)
    if not resolution.resolved:
        raise RuntimeError(f"could not acknowledge: {resolution.explain()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headed",
        action="store_true",
        help="open a real browser window and hand control to you personally",
    )
    args = parser.parse_args()

    stale = artifact_without_interstitial_handling()
    full = CapabilityArtifact.model_validate_json(open(ARTIFACT).read())

    with WebSurface(headless=not args.headed) as surface:
        session = MeridianSessionProvider()

        print("\n" + "#" * 74)
        print("# SCENARIO A -- the human resolves it, the run completes")
        print("#" * 74)
        console = (
            TerminalOperatorConsole(surface=surface)
            if args.headed
            else ScriptedOperatorConsole(
                surface=surface,
                actions=operator_acknowledges,
                note="acknowledged the maintenance notice and returned control",
            )
        )
        engine = ReplayEngine(surface, session_provider=session, escalation=console)
        result = engine.run(stale, {"member_id": "30001"})
        print(f"\n  -> status    : {result.status.value}")
        print(f"  -> escalated : {result.escalated}")
        print(f"  -> outputs   : {result.outputs}")
        print(f"  -> evidence  : {result.log_path}")

        print("\n" + "#" * 74)
        print("# SCENARIO B -- the human looks and cannot fix it either")
        print("#" * 74)
        engine = ReplayEngine(
            surface,
            session_provider=session,
            escalation=RefusingOperatorConsole(),
        )
        result = engine.run(full, {"member_id": "50001"})
        print(f"\n  -> status    : {result.status.value}")
        print(f"  -> escalated : {result.escalated}")
        assert result.error is not None
        print(f"  -> failure   : {result.error.kind}")
        print(f"  -> observed  : {result.error.observed[:150]}")
        print(f"  -> evidence  : {result.log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
