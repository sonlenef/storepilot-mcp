"""The cross-store portfolio table.

These tests assert *invariants*, not layout. The column order and the wording
will change; what must never change is:

* two currencies are never added together;
* an app whose stability was not measured is never rendered as healthy;
* one failing app, or one whole store being down, shrinks the table rather than
  emptying it;
* every blank-looking cell carries a reason, because a reader fills a blank in
  with "zero".
"""

from __future__ import annotations

import re

import pytest

from storepilot.core.errors import RateLimitError, StorePermissionError, ValidationError
from storepilot.core.models import Release, ReleaseStatus, Store
from storepilot.cross import tools
from storepilot.cross.apps import AppPair, Registry, StoreApp
from storepilot.cross.tools import _play_earnings_index, portfolio_overview
from tests.support.gateways import (
    MONTH,
    SUPPRESSED_CAVEAT,
    FakeApple,
    FakePlay,
    FakeVitals,
    earnings_report,
    installs_report,
    ratings_report,
    sales_report,
)

PLAY_APPS = [
    StoreApp(Store.GOOGLE_PLAY, "com.acme.todo", "Acme Todo", "com.acme.todo"),
    StoreApp(Store.GOOGLE_PLAY, "com.acme.photo", "Acme Photo Editor", "com.acme.photo"),
    StoreApp(Store.GOOGLE_PLAY, "com.acme.widgets", "Acme Widgets", "com.acme.widgets"),
]
APPLE_APPS = [
    StoreApp(Store.APP_STORE, "1234567890", "Acme Todo", "com.acme.todo"),
    StoreApp(Store.APP_STORE, "1555000111", "Acme Photo Editor", "com.acme.photo.ios"),
    StoreApp(Store.APP_STORE, "1999888777", "Acme Notes", "com.acme.notes"),
]

REGISTRY = Registry(
    path=tools.registry_module.Path("/tmp/does-not-exist/apps.toml"),
    exists=True,
    pairs=[
        AppPair(
            key="acme-todo",
            name="Acme Todo",
            play_package="com.acme.todo",
            apple_id="1234567890",
            bundle_id="com.acme.todo",
        ),
        AppPair(
            key="acme-photo",
            name="Acme Photo Editor",
            play_package="com.acme.photo",
            apple_id="1555000111",
            bundle_id="com.acme.photo.ios",
        ),
    ],
)

VITALS = {
    "com.acme.todo": FakeVitals("com.acme.todo", 0.42, 0.11),
    "com.acme.photo": FakeVitals("com.acme.photo", 1.87, 0.62),  # breaches both thresholds
    "com.acme.widgets": FakeVitals(
        "com.acme.widgets", None, None, caveat=SUPPRESSED_CAVEAT
    ),  # too small to measure
}

RELEASES_PLAY = {
    "com.acme.todo": Release(
        store=Store.GOOGLE_PLAY,
        app_id="com.acme.todo",
        track="production",
        version_name="4.2.0",
        status=ReleaseStatus.COMPLETED,
    ),
    "com.acme.photo": Release(
        store=Store.GOOGLE_PLAY,
        app_id="com.acme.photo",
        track="production",
        version_name="2.9.1",
        status=ReleaseStatus.IN_PROGRESS,
        user_fraction=0.1,
    ),
    "com.acme.widgets": Release(
        store=Store.GOOGLE_PLAY,
        app_id="com.acme.widgets",
        track="production",
        version_name="1.0.4",
        status=ReleaseStatus.COMPLETED,
    ),
}
RELEASES_APPLE = {
    "1234567890": Release(
        store=Store.APP_STORE,
        app_id="1234567890",
        track="appstore",
        version_name="4.2.0",
        status=ReleaseStatus.COMPLETED,
    ),
    "1555000111": Release(
        store=Store.APP_STORE,
        app_id="1555000111",
        track="appstore",
        version_name="2.8.0",
        status=ReleaseStatus.COMPLETED,
    ),
    "1999888777": Release(
        store=Store.APP_STORE,
        app_id="1999888777",
        track="appstore",
        version_name="1.1.0",
        status=ReleaseStatus.COMPLETED,
    ),
}

INSTALLS = {p: installs_report(p, n) for p, n in
            (("com.acme.todo", 12405), ("com.acme.photo", 88231), ("com.acme.widgets", 412))}
RATINGS = {p: ratings_report(p, r) for p, r in
           (("com.acme.todo", 4.31), ("com.acme.photo", 3.88), ("com.acme.widgets", 4.90))}

ALL_APP_NAMES = ("Acme Todo", "Acme Photo Editor", "Acme Widgets", "Acme Notes")


def healthy_play(**overrides) -> FakePlay:
    kwargs = {
        "apps": PLAY_APPS,
        "vitals": VITALS,
        "installs": INSTALLS,
        "ratings": RATINGS,
        "earnings": earnings_report(
            [
                ("com.acme.todo", 1240.55, "USD"),
                ("com.acme.photo", 8912.10, "USD"),
                ("com.acme.widgets", 31.20, "USD"),
            ]
        ),
        "releases": RELEASES_PLAY,
    }
    kwargs.update(overrides)
    return FakePlay(**kwargs)


def healthy_apple(**overrides) -> FakeApple:
    kwargs = {
        "apps": APPLE_APPS,
        "sales": sales_report(
            [
                ("1234567890", 9102, 812.40, "USD"),
                ("1555000111", 40310, 5120.00, "USD"),
                ("1999888777", 220, 44.10, "USD"),
            ]
        ),
        "releases": RELEASES_APPLE,
    }
    kwargs.update(overrides)
    return FakeApple(**kwargs)


def render(play: FakePlay, apple: FakeApple, *, registry: Registry = REGISTRY) -> str:
    return portfolio_overview(MONTH, 28, play=play, apple=apple, registry=registry)


def app_row(output: str, app_id: str) -> str:
    for line in output.splitlines():
        if re.search(rf"\s{re.escape(app_id)}\s", line):
            return line
    raise AssertionError(f"no table row for {app_id} in:\n{output}")


# --- Baseline ----------------------------------------------------------------


def test_every_app_from_both_stores_gets_a_row() -> None:
    output = render(healthy_play(), healthy_apple())
    for name in ALL_APP_NAMES:
        assert name in output
    for app_id in ("com.acme.todo", "com.acme.photo", "com.acme.widgets"):
        assert app_row(output, app_id)
    for apple_id in ("1234567890", "1555000111", "1999888777"):
        assert app_row(output, apple_id)


def test_a_breach_is_marked_and_explained() -> None:
    output = render(healthy_play(), healthy_apple())
    photo = app_row(output, "com.acme.photo")
    assert "1.87%!" in photo and "0.62%!" in photo
    assert "CRASH+ANR" in photo
    assert "NEEDS ATTENTION" in output
    assert "play_get_anomalies('com.acme.photo')" in output


def test_a_staged_rollout_is_surfaced() -> None:
    output = render(healthy_play(), healthy_apple())
    assert "10%" in app_row(output, "com.acme.photo")
    assert "Rollouts in flight" in output


def test_version_drift_between_stores_is_called_out() -> None:
    output = render(healthy_play(), healthy_apple())
    assert "Version drift between stores" in output
    assert "Play 2.9.1 vs App Store 2.8.0" in output


# --- Invariant: currencies are never summed ----------------------------------


def test_multiple_currencies_are_never_added_together() -> None:
    play = healthy_play(
        earnings=earnings_report(
            [
                ("com.acme.todo", 1240.55, "USD"),
                ("com.acme.photo", 210_450_000, "VND"),
                ("standalone_sku_42", 88.00, "EUR"),
            ]
        )
    )
    apple = healthy_apple(
        sales=sales_report(
            [
                ("1234567890", 9102, 812.40, "USD"),
                ("1555000111", 40310, 4_180_000, "JPY"),
            ]
        )
    )
    output = render(play, apple)

    revenue = output.split("Revenue — ")[1].split("\n\n")[0]
    for currency in ("USD", "VND", "JPY", "EUR"):
        assert currency in revenue, f"{currency} vanished from the revenue section"

    # Each revenue line names exactly one currency.
    for line in revenue.splitlines()[1:]:
        named = [c for c in ("USD", "VND", "JPY", "EUR") if c in line]
        assert len(named) <= 1, f"a single line mixes currencies: {line!r}"

    # And no cross-currency total is printed anywhere.
    grand_total = 1240.55 + 210_450_000 + 88.00 + 812.40 + 4_180_000
    assert f"{grand_total:,.2f}" not in output
    assert "never added together" in output


def test_revenue_cells_always_carry_their_currency() -> None:
    output = render(healthy_play(), healthy_apple())
    todo = app_row(output, "com.acme.todo")
    assert "1,240.55 USD" in todo, "a bare number across two stores is a wrong answer"


def test_in_app_product_revenue_is_attributed_to_its_package() -> None:
    report = earnings_report(
        [
            ("com.acme.todo", 100.0, "USD"),
            ("com.acme.todo.premium", 50.0, "USD"),  # in-app product of a known app
            ("standalone_sku_42", 25.0, "USD"),  # bare SKU, owner unknowable
        ]
    )
    per_app, unattributed = _play_earnings_index(report, {"com.acme.todo"})

    assert per_app["com.acme.todo"] == {"USD": 150.0}
    assert unattributed == {"USD": 25.0}, "leftovers are reported, never dropped or spread around"


def test_unattributable_revenue_is_shown_rather_than_silently_dropped() -> None:
    play = healthy_play(
        earnings=earnings_report(
            [("com.acme.todo", 100.0, "USD"), ("standalone_sku_42", 25.0, "USD")]
        )
    )
    output = render(play, healthy_apple())
    assert "could not be attributed to a known app" in output
    assert "25.00" in output


# --- Invariant: nothing unmeasured is called healthy -------------------------


def test_app_store_rows_never_claim_stability_apple_does_not_report() -> None:
    output = render(healthy_play(), healthy_apple())
    for apple_id in ("1234567890", "1555000111", "1999888777"):
        row = app_row(output, apple_id)
        assert "no-vitals" in row
        assert not re.search(r"\bok\b", row), f"App Store row claims health: {row!r}"


def test_an_app_with_suppressed_vitals_is_unmeasured_not_ok() -> None:
    output = render(healthy_play(), healthy_apple())
    widgets = app_row(output, "com.acme.widgets")
    assert "unmeasured" in widgets
    assert not re.search(r"\bok\b", widgets)
    assert "not a clean bill of health" in output
    assert "Acme Widgets (com.acme.widgets)" in output


def test_a_healthy_measured_app_is_allowed_to_say_ok() -> None:
    """The counterweight: the invariant must not be satisfied by never saying ok."""
    output = render(healthy_play(), healthy_apple())
    assert re.search(r"\bok\b", app_row(output, "com.acme.todo"))


def test_when_nothing_was_measured_no_app_is_called_healthy() -> None:
    output = render(FakePlay(configured=False), FakeApple(configured=False), registry=REGISTRY)
    assert "No app exceeds a Google crash or ANR threshold" not in output
    assert "not a clean bill of health" in output


# --- Invariant: failures shrink the table, never empty it --------------------


def test_one_apps_failing_vitals_leaves_every_other_app_intact() -> None:
    play = healthy_play(
        fail={
            ("vitals", "com.acme.photo"): StorePermissionError(
                "The service account cannot read Android Vitals for com.acme.photo.",
                remedy="Play Console -> Users and permissions -> grant 'View app information'.",
            )
        }
    )
    output = render(play, healthy_apple())

    for name in ALL_APP_NAMES:
        assert name in output
    photo = app_row(output, "com.acme.photo")
    assert "no-perm" in photo
    assert "1.87%" not in photo
    assert not re.search(r"\bok\b", photo)
    assert "Per-app issues" in output
    assert "vitals unavailable" in output
    # The healthy apps are untouched.
    assert "4.31" in app_row(output, "com.acme.todo")


def test_one_store_being_unconfigured_still_lists_its_apps_with_reasons() -> None:
    output = render(healthy_play(), FakeApple(configured=False))

    assert "Acme Todo" in output and "Acme Photo Editor" in output
    apple_row = app_row(output, "1234567890")
    assert apple_row.count("off") >= 5, "every App Store cell carries the reason it is empty"
    assert "off: store not configured on this machine" in output
    assert "App Store columns are unavailable for every app" in output
    # Play data is unaffected.
    assert "12,405" in app_row(output, "com.acme.todo")


def test_a_store_that_fails_mid_listing_reports_the_error_not_an_empty_portfolio() -> None:
    play = healthy_play(
        fail={("list_apps", ""): RateLimitError("quota exhausted", remedy="wait an hour")}
    )
    output = render(play, healthy_apple())

    assert "quota exhausted" in output
    # Registry apps still appear, from the App Store side.
    assert "Acme Todo" in output and "Acme Notes" in output


def test_a_missing_reports_bucket_only_blanks_the_cells_it_feeds() -> None:
    output = render(healthy_play(bucket=False), healthy_apple())
    todo = app_row(output, "com.acme.todo")
    assert "no-bucket" in todo
    assert "0.42%" in todo, "vitals come from a different API and must survive"
    assert "STOREPILOT_GOOGLE_REPORTS_BUCKET" in output


def test_a_missing_vendor_number_only_blanks_apple_revenue() -> None:
    output = render(healthy_play(), healthy_apple(vendor=False))
    assert "no-vendor" in app_row(output, "1234567890")
    assert "4.2.0" in app_row(output, "1234567890"), "the live version is still readable"


def test_a_failing_release_read_does_not_lose_the_row() -> None:
    play = healthy_play(
        fail={("release", "com.acme.todo"): RateLimitError("429", remedy="wait")}
    )
    output = render(play, healthy_apple())
    assert "quota" in app_row(output, "com.acme.todo")
    assert "Acme Todo" in output


def test_neither_store_configured_still_renders_a_usable_answer() -> None:
    output = render(
        FakePlay(configured=False),
        FakeApple(configured=False),
        registry=Registry(path=REGISTRY.path),
    )
    assert "no apps visible on either store" in output
    assert "Google Play columns are unavailable" in output
    assert "App Store columns are unavailable" in output


# --- Freshness and warnings --------------------------------------------------


def test_staleness_is_printed_above_the_numbers_it_qualifies() -> None:
    play = healthy_play(
        earnings=earnings_report(
            [("com.acme.todo", 0.0, "USD")],
            caveat="Earnings for 2026-07 are not published yet.",
        )
    )
    output = render(play, healthy_apple())
    header, table = output.split("App    ", 1)
    assert "not published yet" in header, "the caveat must precede the table, not follow it"
    assert table


def test_paired_and_single_store_apps_are_counted_separately() -> None:
    output = render(healthy_play(), healthy_apple())
    assert "4 app(s), 2 paired across both stores" in output


def test_apps_that_look_pairable_but_are_not_written_down_prompt_the_user() -> None:
    """A pairing no API states must be written down before revenue is joined."""
    output = render(healthy_play(), healthy_apple(), registry=Registry(path=REGISTRY.path))
    assert "not paired in" in output
    assert "suggest_app_pairs" in output
    # Until then each store's copy keeps its own row rather than being guessed together.
    assert "6 app(s), 0 paired across both stores" in output


def test_a_registry_entry_the_credentials_cannot_see_is_reported_not_dropped() -> None:
    registry = Registry(
        path=REGISTRY.path,
        exists=True,
        pairs=[
            *REGISTRY.pairs,
            AppPair(key="ghost", name="Ghost App", play_package="com.acme.ghost"),
        ],
    )
    output = render(healthy_play(), healthy_apple(), registry=registry)
    assert "Ghost App" in output
    assert "which this Play account cannot see" in output


# --- Input validation --------------------------------------------------------


@pytest.mark.parametrize("days", [0, 400])
def test_an_out_of_range_vitals_window_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        portfolio_overview(MONTH, days, play=healthy_play(), apple=healthy_apple(), registry=REGISTRY)


@pytest.mark.parametrize("month", ["july", "2026", ""])
def test_month_parsing(month: str) -> None:
    if month == "":
        # Empty means "the last complete month" — never the current, partial one.
        output = render(healthy_play(), healthy_apple())
        assert "Installs, revenue and rating:" in output
        return
    with pytest.raises(ValidationError):
        portfolio_overview(
            month, 28, play=healthy_play(), apple=healthy_apple(), registry=REGISTRY
        )
