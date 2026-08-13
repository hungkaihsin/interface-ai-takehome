"""The artifact schema's guardrails.

These tests are deliberately narrow. They do not test pydantic -- they test the
four rules we added *on top of* pydantic, each of which encodes a decision that
would otherwise be a comment in a docstring:

  * a step cannot reference an input the capability never declared,
  * step ids are unique, so a failure can name the step that broke,
  * an artifact cannot understate its own blast radius,
  * an artifact cannot use an action its own policy forbids.

The third is the one that matters most. `policy.has_irreversible_steps` is what an
approval gate reads before allowing unattended replay. If an artifact could carry
a risky step while declaring itself reversible, that gate would be decorative.

Run: .venv/bin/pytest tests/ -v
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from capability.schema import CapabilityArtifact
from scripts.build_example_artifact import build

ARTIFACT_PATH = Path("artifacts/member_savings_lookup.v1.json")


@pytest.fixture
def artifact_dict() -> dict:
    """The known-good artifact, as plain JSON data we can corrupt per test."""
    return copy.deepcopy(json.loads(json.dumps(build().model_dump(mode="json"))))


# -- the good case ---------------------------------------------------------


def test_reference_artifact_builds_and_round_trips(artifact_dict: dict) -> None:
    """A valid artifact survives a serialize/deserialize cycle unchanged.

    Round-tripping matters because the artifact's whole job is to be written to
    disk by one process and read by another; a field that silently fails to
    serialize would only surface in production replay.
    """
    loaded = CapabilityArtifact.model_validate(artifact_dict)
    again = CapabilityArtifact.model_validate(
        json.loads(json.dumps(loaded.model_dump(mode="json")))
    )
    assert again.model_dump(mode="json") == loaded.model_dump(mode="json")
    assert again.capability_id == "member_savings_lookup"
    assert again.schema_version == "1.0"


def test_contract_is_readable_without_reading_steps(artifact_dict: dict) -> None:
    """An agent can decide whether to call this without parsing the step list.

    This is the "contract, not macro" claim from the schema docstring, asserted
    rather than merely stated.
    """
    a = CapabilityArtifact.model_validate(artifact_dict)
    assert [p.name for p in a.inputs] == ["member_id"]
    assert [o.name for o in a.outputs] == ["savings_balance"]
    assert {o.code for o in a.business_outcomes} == {
        "MEMBER_NOT_FOUND",
        "PERMISSION_DENIED",
    }
    assert a.success.description


def test_no_concrete_member_id_is_stored(artifact_dict: dict) -> None:
    """The recorded flow is parameterised, so no member's data is in the file.

    Both a reuse property and a privacy property: the artifact works for every
    member precisely because it contains none of them.
    """
    raw = json.dumps(artifact_dict)
    assert "{{ member_id }}" in raw
    for example_value in ("10001", "10002", "900-31-4402"):
        assert example_value not in raw.replace('"example": "10001"', "")


# -- the guardrails --------------------------------------------------------


def test_rejects_undeclared_input_reference(artifact_dict: dict) -> None:
    artifact_dict["steps"][1]["action"]["value"] = "{{ ssn }}"
    with pytest.raises(ValidationError, match="undeclared input"):
        CapabilityArtifact.model_validate(artifact_dict)


def test_rejects_duplicate_step_ids(artifact_dict: dict) -> None:
    artifact_dict["steps"][2]["id"] = artifact_dict["steps"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate step id"):
        CapabilityArtifact.model_validate(artifact_dict)


def test_rejects_risky_step_declared_reversible(artifact_dict: dict) -> None:
    """The guardrail that keeps the approval gate honest."""
    artifact_dict["steps"][2]["risk"] = "risky"
    assert artifact_dict["policy"]["has_irreversible_steps"] is False
    with pytest.raises(ValidationError, match="has_irreversible_steps"):
        CapabilityArtifact.model_validate(artifact_dict)


def test_accepts_risky_step_when_policy_admits_it(artifact_dict: dict) -> None:
    """The same artifact is fine once it tells the truth about itself."""
    artifact_dict["steps"][2]["risk"] = "risky"
    artifact_dict["policy"]["has_irreversible_steps"] = True
    a = CapabilityArtifact.model_validate(artifact_dict)
    assert a.policy.has_irreversible_steps is True


def test_rejects_action_outside_policy_allowlist(artifact_dict: dict) -> None:
    artifact_dict["policy"]["allowed_actions"] = ["navigate", "fill"]
    with pytest.raises(ValidationError, match="allowed_actions does not permit"):
        CapabilityArtifact.model_validate(artifact_dict)


def test_rejects_unknown_accessibility_role(artifact_dict: dict) -> None:
    """Roles are a closed set, so an artifact cannot name a control type the
    executor has no handler for. Better to fail at load than midway through."""
    target = artifact_dict["steps"][1]["action"]["target"]
    target["strategies"][0]["role"] = "frobnicator"
    with pytest.raises(ValidationError):
        CapabilityArtifact.model_validate(artifact_dict)


def test_rejects_locator_with_no_strategies(artifact_dict: dict) -> None:
    """A locator that describes nothing cannot resolve to anything."""
    artifact_dict["steps"][1]["action"]["target"]["strategies"] = []
    with pytest.raises(ValidationError):
        CapabilityArtifact.model_validate(artifact_dict)


# -- the artifact on disk --------------------------------------------------


@pytest.mark.skipif(
    not ARTIFACT_PATH.exists(),
    reason="run `python -m scripts.build_example_artifact` first",
)
def test_artifact_on_disk_is_loadable() -> None:
    """The committed artifact still validates against the current schema.

    This is the regression that catches a schema change quietly orphaning every
    recipe already recorded -- the exact failure `schema_version` exists to make
    visible.
    """
    a = CapabilityArtifact.model_validate_json(ARTIFACT_PATH.read_text())
    assert a.schema_version == "1.0"
    assert a.version >= 1
