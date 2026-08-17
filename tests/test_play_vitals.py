"""Android Vitals aggregation.

Google measures an app against two published thresholds — 1.09% user-perceived
crash rate, 0.47% ANR rate — and exceeding either costs store visibility. So the
number StorePilot prints has to be the number Play Console prints.

The trap is the average. Vitals rates are per-day and per-user; a plain mean of
daily rates lets one low-traffic day with a freak 40% rate dominate a month of
healthy traffic and invent a threshold breach the console never showed. Every
rate here is user-weighted, and where Google publishes its own rolling
user-weighted average, that is used verbatim.

Values are percentages throughout, matching the module's own convention
(``1.32`` means 1.32%).
"""

from __future__ import annotations

from datetime import date

import pytest

from storepilot.core.errors import StorePermissionError, ValidationError
from storepilot.core.models import Store
from storepilot.google_play import auth, reporting
from storepilot.google_play.reporting import (
    ANR_RATE_THRESHOLD_PERCENT,
    CRASH_RATE_THRESHOLD_PERCENT,
    DAILY_TIMEZONE,
    METRIC_SETS,
    MetricReading,
    _aggregate,
    list_anomalies,
    query_vitals,
    search_apps,
)
from tests.support.fake_google import FakeReportingClient, freshness_payload, timeline_row

CRASH = METRIC_SETS["crash"]
ANR = METRIC_SETS["anr"]

CRASH_RATE = CRASH.primary_metric
CRASH_28D = CRASH.rolling[28]
USERS = "distinctUsers"


@pytest.fixture
def reporting_client(monkeypatch: pytest.MonkeyPatch):
    """Install a fake Reporting API client and hand it back for configuration."""

    def install(client: FakeReportingClient) -> FakeReportingClient:
        monkeypatch.setattr(auth, "reporting_client", lambda: client)
        return client

    return install


# --- The weighting -----------------------------------------------------------


def test_a_freak_day_on_five_users_does_not_outvote_a_month_of_healthy_traffic() -> None:
    """40% crash rate on 5 users beside 0.5% on 1000 users is ~0.70%, not 20.25%."""
    rows = [
        timeline_row((2026, 7, 1), {CRASH_RATE: 40.0, USERS: 5}),
        timeline_row((2026, 7, 2), {CRASH_RATE: 0.5, USERS: 1000}),
    ]
    reading = _aggregate(rows, CRASH, 28)

    expected = (40.0 * 5 + 0.5 * 1000) / 1005
    assert reading.value == pytest.approx(expected, abs=1e-6)
    assert reading.value == pytest.approx(0.6965, abs=1e-3)
    assert reading.user_weighted is True

    plain_mean = (40.0 + 0.5) / 2
    assert plain_mean == pytest.approx(20.25)
    assert reading.value < CRASH_RATE_THRESHOLD_PERCENT < plain_mean
    assert reading.exceeds_threshold is False, "a plain mean would invent a policy breach here"
    assert "within threshold 1.09%" in reading.verdict()


def test_google_s_own_rolling_average_wins_when_present() -> None:
    """It is literally the figure Play Console judges, so our verdict cannot disagree."""
    rows = [
        timeline_row((2026, 7, 1), {CRASH_RATE: 40.0, USERS: 5}),
        timeline_row((2026, 7, 2), {CRASH_RATE: 0.5, USERS: 1000, CRASH_28D: 1.42}),
    ]
    reading = _aggregate(rows, CRASH, 28)

    assert reading.value == 1.42
    assert reading.metric_name == CRASH_28D
    assert reading.user_weighted is True
    assert reading.exceeds_threshold is True
    assert reading.distinct_users == 1000


def test_the_most_recent_rolling_datapoint_is_the_one_used() -> None:
    rows = [
        timeline_row((2026, 7, 2), {CRASH_28D: 0.90, USERS: 900}),
        timeline_row((2026, 7, 1), {CRASH_28D: 2.50, USERS: 100}),
    ]
    reading = _aggregate(rows, CRASH, 28)
    assert reading.value == 0.90
    assert reading.latest_covered_day == date(2026, 7, 2)


def test_a_window_with_no_rolling_average_falls_back_to_a_weighted_mean() -> None:
    """Google publishes rolling averages for 7 and 28 days only."""
    rows = [
        timeline_row((2026, 7, 1), {CRASH_RATE: 2.0, USERS: 100}),
        timeline_row((2026, 7, 2), {CRASH_RATE: 1.0, USERS: 300}),
    ]
    reading = _aggregate(rows, CRASH, 14)
    assert reading.value == pytest.approx((2.0 * 100 + 1.0 * 300) / 400)
    assert reading.metric_name == CRASH_RATE
    assert reading.user_weighted is True
    assert reading.days_covered == 2


def test_a_plain_mean_is_used_only_when_no_user_counts_came_back() -> None:
    rows = [
        timeline_row((2026, 7, 1), {CRASH_RATE: 2.0}),
        timeline_row((2026, 7, 2), {CRASH_RATE: 1.0}),
    ]
    reading = _aggregate(rows, CRASH, 14)
    assert reading.value == pytest.approx(1.5)
    assert reading.user_weighted is False, "and the caller is told the average is unweighted"


def test_no_datapoints_reads_as_no_data_never_as_zero() -> None:
    reading = _aggregate([], CRASH, 28)
    assert reading.value is None
    assert reading.exceeds_threshold is False
    assert reading.verdict() == "no data"
    assert reading.format_value() == "n/a"


def test_days_without_the_metric_are_skipped_not_counted_as_zero() -> None:
    rows = [
        timeline_row((2026, 7, 1), {CRASH_RATE: 2.0, USERS: 100}),
        timeline_row((2026, 7, 2), {USERS: 500}),  # metric suppressed that day
    ]
    reading = _aggregate(rows, CRASH, 14)
    assert reading.value == pytest.approx(2.0)
    assert reading.days_covered == 1


# --- Threshold verdicts ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "threshold", "exceeds"),
    [
        (1.08, CRASH_RATE_THRESHOLD_PERCENT, False),
        (1.09, CRASH_RATE_THRESHOLD_PERCENT, False),  # exactly at the line is not over it
        (1.10, CRASH_RATE_THRESHOLD_PERCENT, True),
        (0.46, ANR_RATE_THRESHOLD_PERCENT, False),
        (0.47, ANR_RATE_THRESHOLD_PERCENT, False),
        (0.48, ANR_RATE_THRESHOLD_PERCENT, True),
    ],
)
def test_threshold_boundaries(value: float, threshold: float, exceeds: bool) -> None:
    reading = MetricReading(
        key="crash", label="l", metric_name="m", value=value, threshold_percent=threshold
    )
    assert reading.exceeds_threshold is exceeds
    assert ("EXCEEDS" in reading.verdict()) is exceeds
    if not exceeds:
        assert "headroom" in reading.verdict()


def test_the_published_thresholds_are_the_google_ones() -> None:
    assert CRASH_RATE_THRESHOLD_PERCENT == 1.09
    assert ANR_RATE_THRESHOLD_PERCENT == 0.47
    assert CRASH.primary_metric == "userPerceivedCrashRate", "the user-perceived variant, not raw"
    assert ANR.primary_metric == "userPerceivedAnrRate"


def test_a_metric_without_a_published_threshold_says_so() -> None:
    reading = MetricReading(key="lmk", label="l", metric_name="m", value=3.0)
    assert reading.verdict() == "no published threshold"
    assert reading.exceeds_threshold is False


def test_an_unavailable_metric_is_never_reported_as_healthy() -> None:
    reading = MetricReading(
        key="crash",
        label="l",
        metric_name="m",
        value=None,
        threshold_percent=CRASH_RATE_THRESHOLD_PERCENT,
        error="permission denied",
    )
    assert reading.verdict() == "unavailable"


# --- The query ---------------------------------------------------------------


def test_query_vitals_reads_both_metric_sets_and_flags_breaches(reporting_client) -> None:
    client = reporting_client(
        FakeReportingClient(
            freshness={
                "crashrate": freshness_payload((2026, 7, 30)),
                "anrrate": freshness_payload((2026, 7, 30)),
            },
            rows={
                "crashrate": [
                    timeline_row((2026, 7, 29), {CRASH_RATE: 1.4, USERS: 900, CRASH_28D: 1.42})
                ],
                "anrrate": [
                    timeline_row(
                        (2026, 7, 29),
                        {ANR.primary_metric: 0.2, USERS: 900, ANR.rolling[28]: 0.21},
                    )
                ],
            },
        )
    )

    result = query_vitals("com.acme.todo", days=28, today=date(2026, 7, 31))

    assert result.readings["crash"].value == 1.42
    assert result.readings["anr"].value == 0.21
    assert result.snapshot.store is Store.GOOGLE_PLAY
    assert result.snapshot.exceeds_crash_threshold is True
    assert result.snapshot.exceeds_anr_threshold is False
    assert result.flags == ["crash rate above Google's threshold"]
    # latestEndTime is exclusive, so the last day with data is the day before it.
    assert result.snapshot.period_end == date(2026, 7, 29)
    assert client.log.count("crashrate.query") == 1


def test_the_request_body_matches_what_the_api_actually_accepts(reporting_client) -> None:
    client = reporting_client(
        FakeReportingClient(
            freshness={"crashrate": freshness_payload((2026, 7, 30))},
            rows={"crashrate": [timeline_row((2026, 7, 29), {CRASH_RATE: 1.0, USERS: 10})]},
        )
    )
    query_vitals("com.acme.todo", days=28, metrics=["crash"], today=date(2026, 7, 31))

    _name, kwargs = next(call for call in client.calls if call[0] == "crashrate.query")
    assert kwargs["name"] == "apps/com.acme.todo/crashRateMetricSet"
    timeline = kwargs["body"]["timelineSpec"]
    assert timeline["aggregationPeriod"] == "DAILY"
    assert timeline["startTime"] == {
        "year": 2026,
        "month": 7,
        "day": 2,
        "timeZone": {"id": DAILY_TIMEZONE},
    }
    assert timeline["endTime"]["day"] == 30, "the interval is half-open: [start, end)"
    assert timeline["endTime"]["timeZone"] == {"id": "America/Los_Angeles"}
    assert kwargs["body"]["userCohort"] == "OS_PUBLIC"
    assert CRASH_28D in kwargs["body"]["metrics"]
    assert USERS in kwargs["body"]["metrics"]


def test_one_failing_metric_set_degrades_only_that_metric(reporting_client) -> None:
    reporting_client(
        FakeReportingClient(
            freshness={
                "crashrate": freshness_payload((2026, 7, 30)),
                "anrrate": freshness_payload((2026, 7, 30)),
            },
            rows={"crashrate": [timeline_row((2026, 7, 29), {CRASH_RATE: 0.4, USERS: 900})]},
            errors={
                "anrrate.query": StorePermissionError(
                    "no access to the ANR metric set", remedy="grant it"
                )
            },
        )
    )

    result = query_vitals("com.acme.todo", days=28, today=date(2026, 7, 31))

    assert result.readings["crash"].value == pytest.approx(0.4)
    assert result.readings["anr"].value is None
    assert result.readings["anr"].error == "no access to the ANR metric set"
    assert result.readings["anr"].verdict() == "unavailable"
    assert result.snapshot.exceeds_anr_threshold is False


def test_every_metric_failing_re_raises_rather_than_printing_a_confident_no_data(
    reporting_client,
) -> None:
    """A setup-level failure must reach the user with its remedy attached."""
    reporting_client(
        FakeReportingClient(
            errors={
                "crashrate.get": StorePermissionError("no vitals access", remedy="grant it"),
                "crashrate.query": StorePermissionError("no vitals access", remedy="grant it"),
                "anrrate.get": StorePermissionError("no vitals access", remedy="grant it"),
                "anrrate.query": StorePermissionError("no vitals access", remedy="grant it"),
            }
        )
    )
    with pytest.raises(StorePermissionError, match="no vitals access"):
        query_vitals("com.acme.todo", days=28, today=date(2026, 7, 31))


def test_missing_freshness_metadata_is_a_caveat_not_a_failure(reporting_client) -> None:
    reporting_client(
        FakeReportingClient(
            rows={"crashrate": [timeline_row((2026, 7, 29), {CRASH_RATE: 0.4, USERS: 900})]},
        )
    )
    result = query_vitals("com.acme.todo", days=28, metrics=["crash"], today=date(2026, 7, 31))
    assert result.readings["crash"].value == pytest.approx(0.4)
    assert "assumed rather than confirmed" in (result.freshness.caveat or "")
    assert result.freshness.is_complete is False


def test_a_low_traffic_app_is_never_reported_as_crash_free(reporting_client) -> None:
    reporting_client(
        FakeReportingClient(freshness={"crashrate": freshness_payload((2026, 7, 30))})
    )
    result = query_vitals("com.acme.tiny", days=28, metrics=["crash"], today=date(2026, 7, 31))

    assert result.readings["crash"].value is None
    assert result.snapshot.crash_rate is None
    assert result.snapshot.exceeds_crash_threshold is False
    assert "does not mean the app is crash-free" in (result.freshness.caveat or "")


def test_freshness_memo_avoids_re_asking_for_every_app_on_every_call(
    reporting_client,
) -> None:
    client = reporting_client(
        FakeReportingClient(
            freshness={"crashrate": freshness_payload((2026, 7, 30))},
            rows={"crashrate": [timeline_row((2026, 7, 29), {CRASH_RATE: 0.4, USERS: 900})]},
        )
    )
    for _ in range(3):
        query_vitals("com.acme.todo", days=28, metrics=["crash"], today=date(2026, 7, 31))

    assert client.log.count("crashrate.get") == 1
    assert client.log.count("crashrate.query") == 3

    reporting.reset_freshness_cache()
    query_vitals("com.acme.todo", days=28, metrics=["crash"], today=date(2026, 7, 31))
    assert client.log.count("crashrate.get") == 2


@pytest.mark.parametrize("days", [0, -1, 366])
def test_out_of_range_windows_are_refused(days: int) -> None:
    with pytest.raises(ValidationError, match="out of range"):
        query_vitals("com.acme.todo", days=days)


def test_unknown_metrics_are_refused_with_the_valid_set() -> None:
    with pytest.raises(ValidationError) as excinfo:
        query_vitals("com.acme.todo", metrics=["crash", "vibes"])
    assert "vibes" in excinfo.value.message
    assert "slow_rendering" in excinfo.value.remedy


# --- Apps and anomalies ------------------------------------------------------


def test_search_apps_walks_pages_and_sorts_by_name(reporting_client) -> None:
    reporting_client(
        FakeReportingClient(
            apps=[
                {"packageName": "com.acme.zeta", "displayName": "Zeta"},
                {"packageName": "com.acme.alpha", "displayName": "Alpha"},
                {"displayName": "no package at all"},
            ]
        )
    )
    apps = search_apps()
    assert [a.app_id for a in apps] == ["com.acme.alpha", "com.acme.zeta"]
    assert all(a.store is Store.GOOGLE_PLAY for a in apps)


def test_anomalies_are_described_with_their_expected_range(reporting_client) -> None:
    reporting_client(
        FakeReportingClient(
            anomalies=[
                {
                    "name": "apps/com.acme.todo/anomalies/1",
                    "metricSet": "apps/com.acme.todo/crashRateMetricSet",
                    "metric": {
                        "metric": "crashRate",
                        "decimalValue": {"value": "0.093"},
                        "decimalValueConfidenceInterval": {
                            "lowerBound": {"value": "0.01"},
                            "upperBound": {"value": "0.03"},
                        },
                    },
                    "timelineSpec": {
                        "startTime": {"year": 2026, "month": 7, "day": 20},
                        "endTime": {"year": 2026, "month": 7, "day": 21},
                    },
                    "dimensions": [{"dimension": "versionCode", "valueLabel": "4501"}],
                }
            ]
        )
    )
    anomaly = list_anomalies("com.acme.todo")[0]
    described = anomaly.describe()
    assert "crashRate = 0.093" in described
    assert "expected 0.01-0.03" in described
    assert "versionCode=4501" in described
    assert anomaly.metric_set == "crashRateMetricSet"
