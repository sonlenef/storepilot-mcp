"""Tests that need a real store account. EXCLUDED from the default run.

``addopts = "-m 'not live'"`` in pyproject.toml keeps these out of every normal
invocation, so a live test can never fire by accident — including in CI, where a
stray credential in the environment would otherwise make the suite talk to a real
Play Console or App Store Connect team.

Run them deliberately, against an account you are willing to have read:

    pytest -m live

Everything here is READ-ONLY. There is no live test that publishes anything: a
test that creates a release, replies to a review, or edits a listing would be a
test that costs the operator a real, user-visible change, and the write paths are
covered offline against fakes instead (see test_guards_tokens.py and
test_play_publisher.py).
"""

from __future__ import annotations

import pytest

from storepilot.config import settings

pytestmark = pytest.mark.live


@pytest.fixture
def play_configured() -> None:
    if not settings.google_play_enabled:
        pytest.skip("STOREPILOT_GOOGLE_CREDENTIALS is not set")


@pytest.fixture
def app_store_configured() -> None:
    if not settings.app_store_enabled:
        pytest.skip("STOREPILOT_ASC_KEY_PATH / KEY_ID / ISSUER_ID are not all set")


def test_setup_doctor_against_the_real_accounts() -> None:
    """The one call worth making for real: it is the setup contract end to end."""
    from storepilot.server import setup_doctor

    report = setup_doctor()
    assert "StorePilot setup check" in report
    assert "[fail]" not in report, report


def test_play_lists_the_real_apps(play_configured: None) -> None:
    from storepilot.google_play.reporting import search_apps

    apps = search_apps()
    assert apps, "the Reporting API returned no apps — check Play Console permissions"
    assert all(app.app_id for app in apps)


def test_play_vitals_return_a_percentage_in_a_plausible_range(play_configured: None) -> None:
    """Unit check against reality.

    Offline tests can only assert the module's own convention (percent, so 1.32
    means 1.32%). Confirming that the API really speaks percent rather than a
    0-1 fraction needs one real response — if it is a fraction, every threshold
    verdict in the product is wrong by 100x.
    """
    from storepilot.google_play.reporting import query_vitals, search_apps

    apps = search_apps()
    if not apps:
        pytest.skip("no apps visible to this service account")
    result = query_vitals(apps[0].app_id, days=28)
    crash = result.reading("crash")
    assert crash is not None
    if crash.value is None:
        pytest.skip(f"Android Vitals has no data for {apps[0].app_id}")
    assert 0.0 <= crash.value <= 100.0
    assert crash.value > 0.001, (
        "a crash rate this small suggests the API returns a 0-1 fraction while StorePilot "
        "compares it against a percentage threshold"
    )


def test_app_store_lists_the_real_apps(app_store_configured: None) -> None:
    from storepilot.app_store.client import shared_client
    from storepilot.app_store.resources import list_apps

    apps = list_apps(shared_client())
    assert apps is not None


def test_the_asc_token_is_accepted_by_apple(app_store_configured: None) -> None:
    """Signing is verified offline; that Apple *accepts* the token is not."""
    from storepilot.app_store.client import shared_client

    client = shared_client()
    payload = client.get_json("/v1/apps", params={"limit": 1})
    assert "data" in payload
    assert client.rate_limit.hour_remaining is not None, (
        "Apple stopped sending x-rate-limit; the pre-emptive throttle depends on it"
    )
