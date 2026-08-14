"""Replay a saved capability: the production path, no model involved.

    python -m scripts.demo_replay --member 10001
    python -m scripts.demo_replay --all

Runs with GOOGLE_API_KEY unset. If this needed a model the whole design claim
would be false, and deleting the key is the easiest way to check.
"""

from __future__ import annotations

import argparse
import sys

from capability.schema import CapabilityArtifact
from replay.engine import ReplayEngine
from surface.session import MeridianSessionProvider
from surface.web import WebSurface

DEFAULT_ARTIFACT = "artifacts/member_savings_lookup.v1.json"

#: Each member id drives the app into a different runtime condition. The comment
#: is the class the result contract should place it in.
TOUR = [
    ("10001", "happy path"),
    ("10002", "happy path, different member"),
    ("99999", "business outcome: no such member"),
    ("20001", "business outcome: permission denied"),
    ("30001", "recoverable: maintenance interstitial"),
    ("40001", "recoverable: slow load"),
    ("50001", "hard failure: core banking error"),
    ("abc", "rejected before the browser moves: bad input"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT)
    parser.add_argument("--member", help="a single member number to look up")
    parser.add_argument("--all", action="store_true", help="run the full tour")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    artifact = CapabilityArtifact.model_validate_json(open(args.artifact).read())
    cases = TOUR if args.all else [(args.member or "10001", "requested")]

    print(f"\ncapability : {artifact.capability_id} v{artifact.version}")
    print(f"contract   : ({[p.name for p in artifact.inputs]}) -> "
          f"{[o.name for o in artifact.outputs]}")
    print(f"outcomes   : {[o.code for o in artifact.business_outcomes] or '(none declared)'}")
    print()
    print(f"  {'input':8} {'status':17} {'result':44} note")
    print(f"  {'-'*8} {'-'*17} {'-'*44} {'-'*28}")

    failures = 0
    with WebSurface(headless=not args.headed) as surface:
        engine = ReplayEngine(surface, session_provider=MeridianSessionProvider())
        for member_id, note in cases:
            result = engine.run(artifact, {"member_id": member_id})
            detail = result.summary()
            if result.recoveries:
                detail += f"  [recovered: {[r.code for r in result.recoveries]}]"
            print(f"  {member_id:8} {result.status.value:17} {detail[:44]:44} {note}")
            if result.status.value == "failure" and member_id in ("10001", "10002"):
                failures += 1

    print()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
