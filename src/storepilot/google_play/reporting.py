"""Play Developer Reporting API v1beta1 — Android Vitals, app discovery, anomalies.

Everything here was shaped against the discovery document bundled with
google-api-python-client (``playdeveloperreporting.v1beta1.json``) rather than
from memory, because the request JSON is unusually easy to get subtly wrong:

* The client resources are ``vitals().crashrate()`` / ``vitals().anrrate()``,
  lowercase and without the ``MetricSet`` suffix, even though the *resource name*
  in the request is ``apps/{package}/crashRateMetricSet``.
* ``timelineSpec.startTime``/``endTime`` are ``google.type.DateTime`` objects
  (year/month/day fields), not RFC-3339 strings, and for DAILY aggregation the
  only timezone the API accepts is ``America/Los_Angeles``.
* The interval is half-open: ``[startTime, endTime)``. ``latestEndTime`` from the
  freshness metadata is therefore the *exclusive* end of available data, so the
  last day actually covered is ``latestEndTime - 1 day``.

Quota is 10 queries/second per project, which the portfolio fan-out would blow
through immediately, so every request goes through a process-wide throttle.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from storepilot.core.errors import StorePilotError, ValidationError
from storepilot.core.models import App, Freshness, Store, VitalsSnapshot
from storepilot.google_play import auth

SOURCE = "play_reporting_api"

#: DAILY aggregation is only offered in this timezone — the API rejects any other.
DAILY_TIMEZONE = "America/Los_Angeles"

# --- Google's "bad behaviour" thresholds -------------------------------------
#
# These are the two numbers Play Console measures an app against on the Android
# vitals page. Exceeding either makes the app eligible for reduced store
# visibility and a warning on the store listing, so they are the entire point of
# the vitals tool: a crash rate is meaningless to a user without the line it must
# stay under.
#
# Both are expressed as a PERCENTAGE of distinct daily users, user-weighted over
# a 28-day window, and both are measured on the *user-perceived* variant of the
# metric (a crash while the app was in active use / an input-dispatch ANR) —
# not the raw rate.
#
# Source: https://support.google.com/googleplay/android-developer/answer/9844486
CRASH_RATE_THRESHOLD_PERCENT = 1.09
ANR_RATE_THRESHOLD_PERCENT = 0.47

#: Vitals data lags real time; beyond this many days behind we call it stale.
_FRESHNESS_STALE_AFTER_DAYS = 3

#: Documented quota is 10 QPS; leave headroom for other clients on the project.
_MAX_QPS = 8.0


@dataclass(frozen=True)
class MetricSetSpec:
    """One Android Vitals metric set and how to read a headline number from it.

    ``rolling`` maps a window length in days to Google's own pre-computed
    user-weighted rolling average for that window. Preferring it over anything we
    average ourselves matters: the 28-day user-weighted user-perceived rate is
    literally the number Play Console compares against the threshold, so using it
    means our verdict cannot disagree with the console's.
    """

    key: str
    accessor: str  # attribute on client.vitals(), e.g. "crashrate"
    resource_suffix: str  # e.g. "crashRateMetricSet"
    label: str
    primary_metric: str  # user-perceived variant where one exists
    rolling: dict[int, str] = field(default_factory=dict)
    threshold_percent: float | None = None
    unit: str = "%"

    def resource_name(self, package_name: str) -> str:
        return f"apps/{package_name}/{self.resource_suffix}"


#: Every metric set the API exposes. Crash and ANR carry thresholds and are what
#: the tools query by default; the rest are wired up so adding them to a tool is
#: a one-line change rather than a new integration.
METRIC_SETS: dict[str, MetricSetSpec] = {
    "crash": MetricSetSpec(
        key="crash",
        accessor="crashrate",
        resource_suffix="crashRateMetricSet",
        label="User-perceived crash rate",
        primary_metric="userPerceivedCrashRate",
        rolling={
            7: "userPerceivedCrashRate7dUserWeighted",
            28: "userPerceivedCrashRate28dUserWeighted",
        },
        threshold_percent=CRASH_RATE_THRESHOLD_PERCENT,
    ),
    "anr": MetricSetSpec(
        key="anr",
        accessor="anrrate",
        resource_suffix="anrRateMetricSet",
        label="User-perceived ANR rate",
        primary_metric="userPerceivedAnrRate",
        rolling={
            7: "userPerceivedAnrRate7dUserWeighted",
            28: "userPerceivedAnrRate28dUserWeighted",
        },
        threshold_percent=ANR_RATE_THRESHOLD_PERCENT,
    ),
    "excessive_wakeup": MetricSetSpec(
        key="excessive_wakeup",
        accessor="excessivewakeuprate",
        resource_suffix="excessiveWakeupRateMetricSet",
        label="Excessive wakeup rate",
        primary_metric="excessiveWakeupRate",
        rolling={7: "excessiveWakeupRate7dUserWeighted", 28: "excessiveWakeupRate28dUserWeighted"},
    ),
    "lmk": MetricSetSpec(
        key="lmk",
        accessor="lmkrate",
        resource_suffix="lmkRateMetricSet",
        label="Low-memory kill rate",
        primary_metric="userPerceivedLmkRate",
        rolling={7: "userPerceivedLmkRate7dUserWeighted", 28: "userPerceivedLmkRate28dUserWeighted"},
    ),
    "slow_rendering": MetricSetSpec(
        key="slow_rendering",
        accessor="slowrenderingrate",
        resource_suffix="slowRenderingRateMetricSet",
        label="Slow rendering rate (20fps)",
        primary_metric="slowRenderingRate20Fps",
        rolling={
            7: "slowRenderingRate20Fps7dUserWeighted",
            28: "slowRenderingRate20Fps28dUserWeighted",
        },
    ),
    "slow_start": MetricSetSpec(
        key="slow_start",
        accessor="slowstartrate",
        resource_suffix="slowStartRateMetricSet",
        label="Slow cold start rate",
        primary_metric="slowColdStartRate",
        rolling={7: "slowColdStartRate7dUserWeighted", 28: "slowColdStartRate28dUserWeighted"},
    ),
    "stuck_wakelock": MetricSetSpec(
        key="stuck_wakelock",
        accessor="stuckbackgroundwakelockrate",
        resource_suffix="stuckBackgroundWakelockRateMetricSet",
        label="Stuck background wakelock rate",
        primary_metric="stuckBgWakelockRate",
        rolling={7: "stuckBgWakelockRate7dUserWeighted", 28: "stuckBgWakelockRate28dUserWeighted"},
    ),
}

#: The two metric sets with published thresholds — the default for every tool.
DEFAULT_METRICS: tuple[str, ...] = ("crash", "anr")

#: Normalization metric present in every rate metric set; used as the weight when
#: we have to average daily values ourselves.
_DISTINCT_USERS = "distinctUsers"


# --- Throttle ----------------------------------------------------------------


class _Throttle:
    """Process-wide minimum spacing between Reporting API requests.

    A plain sleep-based spacer rather than a token bucket on purpose: bursting up
    to the quota and then getting 429s would be strictly worse than pacing, since
    a portfolio scan is latency-tolerant but must not fail halfway through.
    """

    def __init__(self, qps: float) -> None:
        self._min_interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_for = self._next_at - now
            if wait_for > 0:
                time.sleep(wait_for)
                now = time.monotonic()
            self._next_at = now + self._min_interval


_throttle = _Throttle(_MAX_QPS)


def _execute(request: Any, *, context: str, package_name: str | None = None) -> dict[str, Any]:
    """Run one throttled Reporting API request, translating vendor errors."""
    _throttle.wait()
    try:
        return request.execute() or {}
    except Exception as exc:
        raise auth.classify_google_error(exc, context=context, package_name=package_name) from exc


# --- google.type.DateTime helpers -------------------------------------------


def _daily_datetime(day: date) -> dict[str, Any]:
    """Build a DAILY-aligned ``google.type.DateTime``.

    For DAILY aggregation the hours/minutes/seconds/nanos fields must be unset
    (they default to 0) and the timezone must be America/Los_Angeles.
    """
    return {
        "year": day.year,
        "month": day.month,
        "day": day.day,
        "timeZone": {"id": DAILY_TIMEZONE},
    }


def _date_from_datetime(value: dict[str, Any] | None) -> date | None:
    if not value:
        return None
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _decimal(value: dict[str, Any] | None) -> float | None:
    """Read a ``google.type.Decimal``, whose numeric value arrives as a string."""
    if not value:
        return None
    raw = value.get("value")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _metric_value(metric: dict[str, Any] | None) -> float | None:
    """Read the ``decimalValue`` out of a ``MetricValue`` wrapper."""
    if not metric:
        return None
    return _decimal(metric.get("decimalValue"))


def _row_metrics(row: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for metric in row.get("metrics") or []:
        name = metric.get("metric")
        parsed = _metric_value(metric)
        if name and parsed is not None:
            out[name] = parsed
    return out


def _dimension_label(dim: dict[str, Any]) -> str:
    value = dim.get("valueLabel") or dim.get("stringValue") or dim.get("int64Value") or "?"
    return f"{dim.get('dimension', '?')}={value}"


# --- Apps --------------------------------------------------------------------


def search_apps(*, page_size: int = 100, max_pages: int = 10) -> list[App]:
    """Every app this service account can see, so users never type a package name.

    Note this lists apps visible to the *Reporting* API, which means apps with at
    least one published release; a brand-new app with only a draft will not show.
    """
    client = auth.reporting_client()
    apps: list[App] = []
    page_token: str | None = None

    for _ in range(max_pages):
        request = client.apps().search(pageSize=page_size, pageToken=page_token)
        payload = _execute(request, context="calling Play Developer Reporting apps.search")
        for entry in payload.get("apps") or []:
            package = entry.get("packageName")
            if not package:
                continue
            apps.append(
                App(
                    store=Store.GOOGLE_PLAY,
                    app_id=package,
                    name=entry.get("displayName") or package,
                )
            )
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    apps.sort(key=lambda a: a.name.lower())
    return apps


# --- Freshness ---------------------------------------------------------------

_freshness_memo: dict[tuple[str, str], tuple[float, date | None]] = {}
_freshness_lock = threading.Lock()
_FRESHNESS_MEMO_TTL = 3600.0


def _latest_end_date(spec: MetricSetSpec, package_name: str) -> date | None:
    """Exclusive end of available DAILY data, from the metric set's own metadata.

    Assuming a fixed lag instead would silently query a window that is partly in
    the future, which the API answers with fewer rows rather than an error.
    Memoized for an hour: freshness advances at most once a day, and the
    portfolio fan-out would otherwise pay for this on every app on every call.
    """
    memo_key = (package_name, spec.key)
    now = time.monotonic()
    with _freshness_lock:
        cached = _freshness_memo.get(memo_key)
        if cached and now - cached[0] < _FRESHNESS_MEMO_TTL:
            return cached[1]

    resource = getattr(client_vitals(), spec.accessor)()
    payload = _execute(
        resource.get(name=spec.resource_name(package_name)),
        context=f"reading {spec.resource_suffix} freshness",
        package_name=package_name,
    )
    latest: date | None = None
    for entry in (payload.get("freshnessInfo") or {}).get("freshnesses") or []:
        if entry.get("aggregationPeriod") == "DAILY":
            latest = _date_from_datetime(entry.get("latestEndTime"))
            break

    with _freshness_lock:
        _freshness_memo[memo_key] = (now, latest)
    return latest


def client_vitals() -> Any:
    """``client.vitals()`` — split out so metric-set access stays one line."""
    return auth.reporting_client().vitals()


def reset_freshness_cache() -> None:
    """Drop memoized freshness metadata (tests, or after a long-lived session)."""
    with _freshness_lock:
        _freshness_memo.clear()


# --- Vitals queries ----------------------------------------------------------


@dataclass
class MetricReading:
    """One headline vitals number plus everything needed to justify it."""

    key: str
    label: str
    metric_name: str  # the API metric the value actually came from
    value: float | None  # percent, e.g. 1.32 means 1.32%
    unit: str = "%"
    threshold_percent: float | None = None
    distinct_users: float | None = None
    days_covered: int = 0
    latest_covered_day: date | None = None
    user_weighted: bool = False
    daily: list[tuple[date, float]] = field(default_factory=list)
    error: str | None = None

    @property
    def exceeds_threshold(self) -> bool:
        return (
            self.threshold_percent is not None
            and self.value is not None
            and self.value > self.threshold_percent
        )

    def verdict(self) -> str:
        if self.error:
            return "unavailable"
        if self.value is None:
            return "no data"
        if self.threshold_percent is None:
            return "no published threshold"
        if self.exceeds_threshold:
            return f"EXCEEDS threshold {self.threshold_percent}{self.unit}"
        headroom = self.threshold_percent - self.value
        return f"within threshold {self.threshold_percent}{self.unit} (headroom {headroom:.2f}pp)"

    def format_value(self) -> str:
        return "n/a" if self.value is None else f"{self.value:.3f}{self.unit}"


@dataclass
class VitalsResult:
    """A vitals query for one app: the shared snapshot plus per-metric detail."""

    package_name: str
    days: int
    readings: dict[str, MetricReading]
    snapshot: VitalsSnapshot
    freshness: Freshness

    def reading(self, key: str) -> MetricReading | None:
        return self.readings.get(key)

    @property
    def flags(self) -> list[str]:
        out: list[str] = []
        for reading in self.readings.values():
            if reading.exceeds_threshold:
                out.append(f"{reading.key} rate above Google's threshold")
        return out


def _aggregate(rows: list[dict[str, Any]], spec: MetricSetSpec, days: int) -> MetricReading:
    """Collapse a daily timeline into the one number to compare with a threshold.

    Preference order, best first:

    1. Google's own ``*{days}dUserWeighted`` rolling average, read from the most
       recent datapoint — this is the exact figure Play Console judges.
    2. A user-weighted mean we compute over the window: ``sum(rate_i * users_i) /
       sum(users_i)``. Weighting is not optional; a plain mean of daily rates
       lets a low-traffic day with a freak 40% rate dominate a month of healthy
       traffic and invents a threshold breach that Play Console does not show.
    3. A plain mean, only when ``distinctUsers`` came back empty.
    """
    rolling_metric = spec.rolling.get(days)
    daily: list[tuple[date, float]] = []
    weighted_sum = 0.0
    weight_total = 0.0
    plain: list[float] = []
    rolling_latest: tuple[date, float] | None = None
    users_latest: float | None = None

    for row in rows:
        day = _date_from_datetime(row.get("startTime"))
        metrics = _row_metrics(row)
        users = metrics.get(_DISTINCT_USERS)

        is_newer_rolling = (
            rolling_metric is not None
            and rolling_metric in metrics
            and day is not None
            and (rolling_latest is None or day >= rolling_latest[0])
        )
        if is_newer_rolling:
            rolling_latest = (day, metrics[rolling_metric])  # type: ignore[arg-type,index]
            users_latest = users

        rate = metrics.get(spec.primary_metric)
        if rate is None or day is None:
            continue
        daily.append((day, rate))
        plain.append(rate)
        if users:
            weighted_sum += rate * users
            weight_total += users

    daily.sort()
    latest_day = daily[-1][0] if daily else (rolling_latest[0] if rolling_latest else None)

    if rolling_latest is not None:
        return MetricReading(
            key=spec.key,
            label=spec.label,
            metric_name=rolling_metric or spec.primary_metric,
            value=rolling_latest[1],
            unit=spec.unit,
            threshold_percent=spec.threshold_percent,
            distinct_users=users_latest,
            days_covered=days,
            latest_covered_day=latest_day,
            user_weighted=True,
            daily=daily,
        )

    if weight_total > 0:
        value = weighted_sum / weight_total
        weighted = True
    elif plain:
        value = sum(plain) / len(plain)
        weighted = False
    else:
        value = None
        weighted = False

    return MetricReading(
        key=spec.key,
        label=spec.label,
        metric_name=spec.primary_metric,
        value=value,
        unit=spec.unit,
        threshold_percent=spec.threshold_percent,
        distinct_users=weight_total or None,
        days_covered=len(daily),
        latest_covered_day=latest_day,
        user_weighted=weighted,
        daily=daily,
    )


def _query_metric_set(
    spec: MetricSetSpec,
    package_name: str,
    *,
    days: int,
    end_exclusive: date,
) -> MetricReading:
    metrics = [spec.primary_metric, _DISTINCT_USERS]
    rolling_metric = spec.rolling.get(days)
    if rolling_metric:
        metrics.insert(0, rolling_metric)

    body = {
        "timelineSpec": {
            "aggregationPeriod": "DAILY",
            "startTime": _daily_datetime(end_exclusive - timedelta(days=days)),
            "endTime": _daily_datetime(end_exclusive),
        },
        "metrics": metrics,
        # OS_PUBLIC is the API default, but stating it keeps the numbers matched
        # to what Play Console shows even if that default ever changes.
        "userCohort": "OS_PUBLIC",
        "pageSize": 1000,
    }

    resource = getattr(client_vitals(), spec.accessor)()
    payload = _execute(
        resource.query(name=spec.resource_name(package_name), body=body),
        context=f"querying {spec.resource_suffix}",
        package_name=package_name,
    )
    return _aggregate(payload.get("rows") or [], spec, days)


def query_vitals(
    package_name: str,
    *,
    days: int = 28,
    metrics: Iterable[str] = DEFAULT_METRICS,
    today: date | None = None,
) -> VitalsResult:
    """Android Vitals for one app over a trailing window, judged against thresholds.

    ``days`` is the window length; 7 and 28 map onto Google's own rolling
    user-weighted averages and are strongly preferred. Values are percentages of
    distinct daily users.
    """
    if days < 1 or days > 365:
        raise ValidationError(
            f"days={days} is out of range.",
            remedy="Pass a window between 1 and 365 days; 28 (the default) or 7 match "
            "Google's own rolling averages and are what Play Console reports.",
        )

    keys = list(metrics)
    unknown = [k for k in keys if k not in METRIC_SETS]
    if unknown:
        raise ValidationError(
            f"Unknown vitals metric(s): {', '.join(unknown)}.",
            remedy=f"Choose from: {', '.join(sorted(METRIC_SETS))}.",
        )

    today = today or datetime.now(UTC).date()
    readings: dict[str, MetricReading] = {}
    latest_ends: list[date] = []
    failures: list[StorePilotError] = []
    freshness_unavailable = False

    for key in keys:
        spec = METRIC_SETS[key]
        try:
            end_exclusive = _latest_end_date(spec, package_name)
        except StorePilotError:
            # Freshness metadata is a nicety; a permissions/quota failure here
            # will resurface on the query below, where it is actionable.
            end_exclusive = None
            freshness_unavailable = True
        if end_exclusive is None:
            # Vitals typically trail by ~2 days. Only a fallback — the caveat on
            # the returned Freshness says so explicitly.
            end_exclusive = today - timedelta(days=1)
            freshness_unavailable = True
        else:
            latest_ends.append(end_exclusive)

        try:
            readings[key] = _query_metric_set(
                spec, package_name, days=days, end_exclusive=end_exclusive
            )
        except StorePilotError as exc:
            failures.append(exc)
            readings[key] = MetricReading(
                key=spec.key,
                label=spec.label,
                metric_name=spec.primary_metric,
                value=None,
                unit=spec.unit,
                threshold_percent=spec.threshold_percent,
                error=exc.message,
            )

    # Degrading per metric is right when one metric set is unavailable, but when
    # every one failed the cause is setup-level (no credentials, API not enabled,
    # no access to this app). Swallowing that would print an authoritative-looking
    # "no data" report whose real message — and its remedy — never reaches the
    # user, so re-raise instead.
    if failures and len(failures) == len(keys):
        raise failures[0]

    crash = readings.get("crash")
    anr = readings.get("anr")
    # latestEndTime is exclusive, so the last day with data is the day before it.
    period_end = min(latest_ends) - timedelta(days=1) if latest_ends else today
    covered = [r.latest_covered_day for r in readings.values() if r.latest_covered_day]
    if covered:
        period_end = min(period_end, max(covered))

    snapshot = VitalsSnapshot(
        store=Store.GOOGLE_PLAY,
        app_id=package_name,
        period_end=period_end,
        crash_rate=crash.value if crash else None,
        anr_rate=anr.value if anr else None,
        exceeds_crash_threshold=bool(crash and crash.exceeds_threshold),
        exceeds_anr_threshold=bool(anr and anr.exceeds_threshold),
    )

    return VitalsResult(
        package_name=package_name,
        days=days,
        readings=readings,
        snapshot=snapshot,
        freshness=_vitals_freshness(
            period_end,
            days=days,
            today=today,
            metadata_missing=freshness_unavailable,
            any_data=any(r.value is not None for r in readings.values()),
        ),
    )


def _vitals_freshness(
    period_end: date,
    *,
    days: int,
    today: date,
    metadata_missing: bool,
    any_data: bool,
) -> Freshness:
    lag = (today - period_end).days
    caveats: list[str] = []
    if metadata_missing:
        caveats.append(
            "Could not read the metric set's freshness metadata, so the window end was "
            "assumed rather than confirmed."
        )
    if lag > _FRESHNESS_STALE_AFTER_DAYS:
        caveats.append(
            f"Latest vitals data is {lag} days old — Android Vitals normally trails by "
            f"2-3 days, so a larger gap usually means the app had too few users to "
            f"report on recent days."
        )
    if not any_data:
        caveats.append(
            "No datapoints were returned for this window. Android Vitals suppresses "
            "metrics for apps below a minimum daily user count, so this is expected for "
            "a low-traffic app and does not mean the app is crash-free."
        )
    # The queried interval is half-open, [end_exclusive - days, end_exclusive),
    # and period_end is end_exclusive - 1 day. So the INCLUSIVE window actually
    # covered is period_end - (days - 1) .. period_end: subtracting a full `days`
    # here reported a 29-day window for a 28-day query, one day earlier than
    # anything that was measured.
    window_start = period_end - timedelta(days=days - 1)
    return Freshness(
        as_of=period_end,
        requested_period=f"{window_start.isoformat()}..{period_end.isoformat()}",
        source=SOURCE,
        lag_days=lag,
        is_complete=not caveats,
        caveat=" ".join(caveats) or None,
    )


# --- Anomalies ---------------------------------------------------------------


@dataclass
class Anomaly:
    """One Google-detected deviation in a vitals metric."""

    name: str
    metric_set: str
    metric: str
    value: float | None
    lower_bound: float | None
    upper_bound: float | None
    start: date | None
    end: date | None
    dimensions: list[str] = field(default_factory=list)

    def describe(self) -> str:
        window = ""
        if self.start:
            window = f" on {self.start.isoformat()}"
            if self.end and self.end != self.start:
                window = f" over {self.start.isoformat()}..{self.end.isoformat()}"
        value = "unknown value" if self.value is None else f"{self.value:.4g}"
        expected = ""
        if self.lower_bound is not None and self.upper_bound is not None:
            expected = f" (expected {self.lower_bound:.4g}-{self.upper_bound:.4g})"
        where = f" [{', '.join(self.dimensions)}]" if self.dimensions else ""
        return f"{self.metric} = {value}{expected}{window}{where}"


def list_anomalies(package_name: str, *, page_size: int = 50) -> list[Anomaly]:
    """Anomalies Google's own detection flagged for this app.

    Cheap (one request) and high signal: these are deviations Google considered
    notable enough to surface in Play Console, so they catch regressions a
    threshold comparison alone would miss.
    """
    client = auth.reporting_client()
    payload = _execute(
        client.anomalies().list(parent=f"apps/{package_name}", pageSize=page_size),
        context="listing vitals anomalies",
        package_name=package_name,
    )

    out: list[Anomaly] = []
    for entry in payload.get("anomalies") or []:
        metric = entry.get("metric") or {}
        interval = entry.get("timelineSpec") or {}
        confidence = metric.get("decimalValueConfidenceInterval") or {}
        out.append(
            Anomaly(
                name=entry.get("name", ""),
                metric_set=(entry.get("metricSet") or "").rsplit("/", 1)[-1],
                metric=metric.get("metric") or "unknown",
                value=_metric_value(metric),
                lower_bound=_decimal(confidence.get("lowerBound")),
                upper_bound=_decimal(confidence.get("upperBound")),
                start=_date_from_datetime(interval.get("startTime")),
                end=_date_from_datetime(interval.get("endTime")),
                dimensions=[_dimension_label(d) for d in entry.get("dimensions") or []],
            )
        )
    return out
