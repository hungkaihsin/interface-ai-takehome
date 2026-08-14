"""Hand-author the member-savings-lookup capability and write it to disk.

Exists so the replay engine could be built and tested against a known-good artifact
before the discovery loop existed -- debugging a non-deterministic model and a new
executor at once is avoidable. Also the schema's worked example.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from capability.locator import (
    ContainerScope,
    Locator,
    RoleInContainerStrategy,
    RoleNameStrategy,
)
from capability.schema import (
    BusinessOutcome,
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
    RecoverableCondition,
    Step,
    SurfaceBinding,
    TextPresent,
    Transform,
)

BASE = "http://127.0.0.1:5001"


def member_number_field() -> Locator:
    return Locator(
        frame_path=["main"],
        describes="the Member Number input on the inquiry screen",
        strategies=[
            # Tier 1 works here: the search form uses a real <label for> binding.
            RoleNameStrategy(role="textbox", name="Member Number"),
            # Tier 2 fallback in case a re-skin drops the label binding but keeps
            # the caption cell -- which is the usual shape of vendor drift.
            RoleInContainerStrategy(
                role="textbox",
                container=ContainerScope(role="row", name="Member Number"),
            ),
        ],
    )


def search_button() -> Locator:
    return Locator(
        frame_path=["main"],
        describes="the Search button on the inquiry screen",
        strategies=[RoleNameStrategy(role="button", name="Search")],
    )


def savings_balance_cell() -> Locator:
    """The balance: third cell of the row that says SAVINGS.

    Nothing near this number has an id or class. Anchoring to the row's meaning
    rather than the table's shape survives a vendor adding a column above.
    """
    return Locator(
        frame_path=["main"],
        describes="the Current Balance cell of the SAVINGS row",
        strategies=[
            RoleInContainerStrategy(
                role="cell",
                container=ContainerScope(
                    role="row", name="SAVINGS", name_match="contains", index=0
                ),
                index=2,
            )
        ],
    )


def build() -> CapabilityArtifact:
    return CapabilityArtifact(
        capability_id="member_savings_lookup",
        version=1,
        name="Look up a member's savings balance",
        description=(
            "Given a member number, open that member's record in the MeridianCU "
            "back-office console and return the current balance of their primary "
            "savings account."
        ),
        surface=SurfaceBinding(
            kind="web",
            app_id="meridian-backoffice-v4",
            tenant_id="meridian-cu",
            entry_url=f"{BASE}/shell",
        ),
        inputs=[
            ParamSpec(
                name="member_id",
                type="string",
                description="The member number as printed on the member's card.",
                pattern=r"^\d{5}$",
                example="10001",
            )
        ],
        outputs=[
            OutputSpec(
                name="savings_balance",
                type="money",
                description="Current balance of the member's primary savings account.",
                source=Extraction(
                    locator=savings_balance_cell(),
                    transform=[Transform(kind="strip"), Transform(kind="to_number")],
                ),
            )
        ],
        steps=[
            Step(
                id="s1",
                intent="Open the back-office console, which lands on member inquiry.",
                # /shell, not /search: navigating straight to an inner page renders
                # it as the whole document and destroys the frame shell.
                action=NavigateAction(url=f"{BASE}/shell"),
                risk="safe",
                checkpoint=Checkpoint(
                    condition=ElementPresent(locator=member_number_field()),
                    description="The inquiry form is on screen.",
                ),
            ),
            Step(
                id="s2",
                intent="Type the member number into the Member Number field.",
                action=FillAction(
                    target=member_number_field(), value="{{ member_id }}"
                ),
                risk="safe",
            ),
            Step(
                id="s3",
                intent="Submit the search.",
                action=ClickAction(target=search_button()),
                risk="safe",
                # No checkpoint on purpose: four screens are valid after this click,
                # so asserting the happy path would turn three answers into
                # failures. The taxonomy below reads the result instead.
            ),
        ],
        success=Checkpoint(
            condition=ElementPresent(locator=savings_balance_cell()),
            description="The member record is displayed and shows a SAVINGS row.",
            timeout_ms=10_000,
        ),
        business_outcomes=[
            BusinessOutcome(
                code="MEMBER_NOT_FOUND",
                detect=TextPresent(frame_path=["main"], text="No member found"),
                description=(
                    "No member exists with that number. A legitimate answer for "
                    "the caller, not a failure."
                ),
            ),
            BusinessOutcome(
                code="PERMISSION_DENIED",
                detect=TextPresent(
                    frame_path=["main"], text="Entitlement check failed"
                ),
                description=(
                    "The record exists but this operator profile may not view it. "
                    "The caller needs to know this specifically, so it can route "
                    "the request to someone entitled."
                ),
            ),
        ],
        recoverables=[
            RecoverableCondition(
                code="MAINTENANCE_INTERSTITIAL",
                detect=TextPresent(
                    frame_path=["main"], text="Scheduled maintenance window"
                ),
                description=(
                    "A maintenance notice can appear between the search and the "
                    "record. Acknowledging it continues to the same destination."
                ),
                remedy=[
                    Step(
                        id="r1",
                        intent="Acknowledge the maintenance notice.",
                        action=ClickAction(
                            target=Locator(
                                frame_path=["main"],
                                describes="the Acknowledge button on the notice",
                                strategies=[
                                    RoleNameStrategy(
                                        role="button", name="Acknowledge"
                                    )
                                ],
                            )
                        ),
                        risk="safe",
                    )
                ],
                max_attempts=2,
            ),
            RecoverableCondition(
                code="SESSION_EXPIRED",
                detect=TextPresent(
                    frame_path=[], text="Your session has expired", match="contains"
                ),
                description=(
                    "The operator session timed out mid-flow. Signing on again and "
                    "retrying the step is the standard remedy; if it recurs, the "
                    "run escalates rather than looping."
                ),
                remedy=[
                    Step(
                        id="r2",
                        intent="Sign back on as the automation operator.",
                        action=NavigateAction(url=f"{BASE}/login"),
                        risk="safe",
                    )
                ],
                max_attempts=1,
            ),
        ],
        policy=Policy(
            allowed_origins=[BASE],
            has_irreversible_steps=False,
            approved_for_unattended=True,
        ),
        provenance=Provenance(
            discovered_at=datetime.now(timezone.utc),
            model="hand-authored",
            discovery_run_id="seed-0001",
            llm_turns=0,
            notes=(
                "Hand-written seed artifact used to develop and test the replay "
                "engine before the discovery loop existed. The discovery run emits "
                "this same schema."
            ),
        ),
    )


if __name__ == "__main__":
    artifact = build()
    out = Path("artifacts") / f"{artifact.capability_id}.v{artifact.version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2) + "\n")
    print(f"wrote {out}")
    print(f"  inputs : {[p.name for p in artifact.inputs]}")
    print(f"  outputs: {[o.name for o in artifact.outputs]}")
    print(f"  steps  : {len(artifact.steps)}")
    print(f"  outcomes declared: {[o.code for o in artifact.business_outcomes]}")
    print(f"  recoverables     : {[r.code for r in artifact.recoverables]}")
