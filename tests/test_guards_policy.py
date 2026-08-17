"""Production rollout policy: the rule that a release never reaches everyone at once.

The policy lives in ``core.guards`` rather than inside a tool so both stores
enforce it identically, so these tests exercise it directly — a policy that is
only correct when reached through one particular tool is not a policy.
"""

from __future__ import annotations

import pytest

from storepilot.core.errors import ValidationError
from storepilot.core.guards import (
    PRODUCTION_POLICY,
    RolloutPolicy,
    _load_policy,
    is_production_track,
    resolve_track,
)

# --- Track resolution --------------------------------------------------------


@pytest.mark.parametrize("track", [None, "", "   "])
def test_missing_track_defaults_to_internal_never_production(track: str | None) -> None:
    assert resolve_track(track) == "internal"


@pytest.mark.parametrize("name", ["prod", "PROD", "live", "release", "public", "main", "master"])
def test_near_miss_track_names_are_refused_not_guessed(name: str) -> None:
    """Accepting 'prod' would silently create a new empty closed-testing track."""
    with pytest.raises(ValidationError) as excinfo:
        resolve_track(name)
    assert "not a Play track name" in excinfo.value.message
    assert "track='production' explicitly" in excinfo.value.remedy


def test_known_and_custom_tracks_pass_through() -> None:
    for name in ("internal", "alpha", "beta", "production", "qa-team", "wear:production"):
        assert resolve_track(name) == name


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        ("production", True),
        ("Production", True),
        ("wear:production", True),
        ("automotive:production", True),
        ("beta", False),
        ("production-candidate", False),
    ],
)
def test_is_production_track(track: str, expected: bool) -> None:
    assert is_production_track(track) is expected


# --- The refusals ------------------------------------------------------------


def test_production_at_100_percent_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PRODUCTION_POLICY.decide("production", user_fraction=1.0, operation="play_create_release")
    assert "cannot release to 100% of production users in one step" in excinfo.value.message
    assert PRODUCTION_POLICY.expand_tool in excinfo.value.remedy
    assert PRODUCTION_POLICY.halt_tool in excinfo.value.remedy
    assert excinfo.value.details["policy"] == "staged_rollout_required"


def test_production_status_completed_is_refused() -> None:
    """``status='completed'`` is 100% spelled a different way."""
    with pytest.raises(ValidationError) as excinfo:
        PRODUCTION_POLICY.decide("production", status="completed", operation="play_create_release")
    assert "100% of production users" in excinfo.value.message


def test_production_at_50_percent_is_refused_as_over_the_ceiling() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PRODUCTION_POLICY.decide("production", user_fraction=0.5, operation="play_create_release")
    assert "above the" in excinfo.value.message
    assert excinfo.value.details["requested_user_fraction"] == 0.5
    assert excinfo.value.details["max_initial_user_fraction"] == PRODUCTION_POLICY.max_initial_fraction


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.5, 10, 50])
def test_fractions_outside_zero_to_one_are_refused(fraction: float) -> None:
    with pytest.raises(ValidationError) as excinfo:
        PRODUCTION_POLICY.decide("beta", user_fraction=fraction)
    assert "between 0 and 1" in excinfo.value.message


def test_unknown_release_status_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PRODUCTION_POLICY.decide("production", status="shipItNow")
    assert "Unknown release status" in excinfo.value.message


# --- The permitted paths -----------------------------------------------------


def test_production_without_a_fraction_defaults_to_ten_percent() -> None:
    decision = PRODUCTION_POLICY.decide("production", operation="play_create_release")
    assert decision.status == "inProgress"
    assert decision.user_fraction == 0.1
    assert decision.is_production is True
    assert decision.audience == "10% of eligible users"
    assert any("safe default" in note for note in decision.notes)


def test_production_exactly_at_the_ceiling_is_allowed() -> None:
    decision = PRODUCTION_POLICY.decide("production", user_fraction=0.2)
    assert decision.user_fraction == 0.2
    assert decision.status == "inProgress"


def test_production_draft_reaches_nobody() -> None:
    decision = PRODUCTION_POLICY.decide("production", status="draft")
    assert decision.status == "draft"
    assert decision.user_fraction is None
    assert decision.audience == "nobody (draft — saved but not served)"


def test_testing_tracks_are_not_staged() -> None:
    decision = PRODUCTION_POLICY.decide("internal")
    assert decision.is_production is False
    assert decision.status == "completed"
    assert any("testers, not to the public" in note for note in decision.notes)


def test_expansion_is_the_only_path_to_one_hundred_percent() -> None:
    decision = PRODUCTION_POLICY.decide_expansion("production", 1.0)
    assert decision.status == "completed"
    assert decision.user_fraction is None
    assert decision.audience == "100% of eligible users"

    partial = PRODUCTION_POLICY.decide_expansion("production", 0.5)
    assert partial.status == "inProgress"
    assert partial.user_fraction == 0.5


# --- The env override cannot disable the policy ------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_ceiling"),
    [
        ("1.0", 0.5),  # "let me ship to everyone" is clamped to half
        ("0.9", 0.5),
        ("0.5", 0.5),
        ("0.3", 0.3),
        ("0.05", 0.05),
        ("0", 0.01),  # cannot be turned into "block everything" either
        ("-3", 0.01),
        ("not a number", 0.2),  # falls back to the built-in default
        ("", 0.2),
    ],
)
def test_max_initial_rollout_env_override_is_clamped(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected_ceiling: float
) -> None:
    monkeypatch.setenv("STOREPILOT_MAX_INITIAL_ROLLOUT", raw)
    policy = _load_policy()
    assert policy.max_initial_fraction == pytest.approx(expected_ceiling)
    assert policy.default_fraction == pytest.approx(min(0.1, expected_ceiling))

    # And the clamped ceiling is actually enforced, not merely recorded.
    with pytest.raises(ValidationError):
        policy.decide("production", user_fraction=1.0)
    with pytest.raises(ValidationError):
        policy.decide("production", user_fraction=expected_ceiling + 0.01)


def test_env_override_absent_gives_the_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STOREPILOT_MAX_INITIAL_ROLLOUT", raising=False)
    policy = _load_policy()
    assert policy.max_initial_fraction == RolloutPolicy.max_initial_fraction == 0.2
    assert policy.default_fraction == 0.1


def test_production_halted_status_is_not_silently_turned_into_a_live_rollout() -> None:
    """Regression: production once ignored status='halted' and served a live 10% rollout."""
    decision = PRODUCTION_POLICY.decide("production", status="halted")
    assert decision.status == "halted", (
        f"asked for 'halted', policy returned {decision.status!r} at {decision.audience}"
    )


def test_non_production_tracks_do_honour_an_explicit_status() -> None:
    """The counterpart to the test above, on tracks that never reach the public."""
    assert PRODUCTION_POLICY.decide("beta", status="halted").status == "halted"
    assert PRODUCTION_POLICY.decide("beta", status="draft").status == "draft"
