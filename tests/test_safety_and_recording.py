"""Redaction, and the recorder's defences against an overfitted discovery run.

These are the two places where a silent failure would be worst: regulated data
reaching a file nobody re-reads, and a capability that looks correct because it was
tested on the one input it was recorded from. Both are cheap to test and expensive
to discover in production, which is exactly the ratio that justifies a test.

Run: .venv/bin/pytest tests/ -v
"""

from __future__ import annotations

from capability.locator import (
    ContainerScope,
    Locator,
    RoleInContainerStrategy,
    RoleNameStrategy,
)
from discovery.agent import DiscoveryResult, RecordedAction
from discovery.recorder import (
    canonicalise_output_locator,
    self_referential_locators,
)
from observability.redaction import Redactor


# -- redaction -------------------------------------------------------------


def test_redacts_the_regulated_fields_a_bank_screen_shows() -> None:
    screen = (
        "Name Dolores Ashgrove SSN 900-31-4402 Account SAV-4471-00812 "
        "Email d.ashgrove@example.invalid Phone (209) 555-0142"
    )
    out = Redactor().text(screen)
    for secret in ("900-31-4402", "SAV-4471-00812", "d.ashgrove@example.invalid",
                   "(209) 555-0142"):
        assert secret not in out
    assert "[redacted:ssn]" in out and "[redacted:account]" in out


def test_redacts_credentials_and_api_keys() -> None:
    redactor = Redactor()
    redactor.add_literal("hunter2xyz")
    out = redactor.text("pw=hunter2xyz key=AIzaSyB0000000000000000000000000000000")
    assert "hunter2xyz" not in out
    assert "AIzaSy" not in out


def test_redaction_reaches_into_nested_structures() -> None:
    """Log payloads are dicts of lists, so a top-level-only sweep would miss most."""
    out = Redactor().value(
        {"steps": [{"observed": "SSN 900-31-4402"}], "n": 3, "ok": True}
    )
    assert "900-31-4402" not in str(out)
    assert out["n"] == 3 and out["ok"] is True


def test_very_short_literals_are_not_redacted() -> None:
    """Redacting a 1-character value would blank out half of every log line."""
    redactor = Redactor()
    redactor.add_literal("7")
    assert redactor.text("step 7 of 7") == "step 7 of 7"


def test_names_are_a_known_gap_not_a_silent_one() -> None:
    """Documents the stated limit: a person's name matches no pattern.

    Asserted rather than merely written down, so that if pattern-based name
    redaction is ever added, this test fails and the REPORT's limitations section
    gets updated with it.
    """
    assert "Dolores Ashgrove" in Redactor().text("Name Dolores Ashgrove")


# -- recorder defences -----------------------------------------------------


def _read_action(name: str, container: str, observed: str) -> RecordedAction:
    return RecordedAction(
        kind="read",
        output_name="savings_balance",
        observed_text=observed,
        locator=Locator(
            frame_path=["main"],
            describes="the balance cell",
            strategies=[
                RoleNameStrategy(role="cell", name=name),
                RoleInContainerStrategy(
                    role="cell",
                    container=ContainerScope(role="row", name=container),
                    index=2,
                ),
            ],
        ),
    )


def test_drops_a_strategy_that_names_the_value_it_returns() -> None:
    """The overfitting a real discovery run produced: `cell name "18432.19"`."""
    action = _read_action("18432.19", "SAVINGS", "18432.19")
    cleaned = canonicalise_output_locator(action.locator, "18432.19")
    kinds = [s.kind for s in cleaned.strategies]
    assert "role_name" not in kinds
    assert kinds == ["role_in_container"]


def test_narrows_a_container_named_by_the_whole_row() -> None:
    """`SAV-4471-00812 SAVINGS 18432.19 ... ` identifies exactly one member."""
    row = "SAV-4471-00812 SAVINGS 18432.19 1998-04-17 OPEN"
    cleaned = canonicalise_output_locator(
        _read_action("18432.19", row, "18432.19").locator, "18432.19"
    )
    container = cleaned.strategies[0].container
    assert container.name == "SAVINGS"
    assert "18432.19" not in container.name


def test_leaves_a_already_stable_locator_alone() -> None:
    action = _read_action("Current Balance", "SAVINGS", "18432.19")
    cleaned = canonicalise_output_locator(action.locator, "18432.19")
    assert cleaned.strategies == action.locator.strategies


def test_detects_self_referential_locators_for_reporting() -> None:
    result = DiscoveryResult(goal="g", succeeded=True, run_id="r", turns=1)
    result.actions = [_read_action("18432.19", "SAVINGS", "18432.19")]
    problems = self_referential_locators(result)
    assert problems and "savings_balance" in problems[0]


def test_clean_run_reports_no_problems() -> None:
    result = DiscoveryResult(goal="g", succeeded=True, run_id="r", turns=1)
    result.actions = [_read_action("Current Balance", "SAVINGS", "18432.19")]
    assert self_referential_locators(result) == []


# -- provider error classification ----------------------------------------
#
# Both arrive as HTTP 429, and treating them the same means backing off for
# minutes against a quota that only resets at midnight. Observed for real: the
# free tier allows 20 gemini-2.5-flash requests PER DAY, and the loop spent four
# minutes retrying it before giving up.


def test_per_day_quota_is_a_hard_failure_not_a_retry() -> None:
    from discovery.agent import classify_provider_error

    message = (
        "429 RESOURCE_EXHAUSTED. quotaId: "
        "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '20'"
    )
    assert classify_provider_error(message) == ("quota_exhausted", None)


def test_per_minute_rate_limit_uses_the_providers_own_delay() -> None:
    """The API tells us how long to wait; guessing is strictly worse."""
    from discovery.agent import classify_provider_error

    kind, wait = classify_provider_error("429 RESOURCE_EXHAUSTED 'retryDelay': '8s'")
    assert kind == "rate_limited"
    assert wait == 8


def test_rate_limit_without_a_delay_falls_back_to_backoff() -> None:
    from discovery.agent import classify_provider_error

    assert classify_provider_error("503 UNAVAILABLE") == ("rate_limited", 0)


def test_ordinary_errors_are_not_treated_as_rate_limits() -> None:
    from discovery.agent import classify_provider_error

    assert classify_provider_error("400 INVALID_ARGUMENT")[0] == "other"
