"""App Store Connect reporting: sales/finance TSV and the async analytics flow.

Two completely different mechanisms live here because they answer the same
question ("how is the app doing?") from opposite ends of Apple's API.

**Sales reports** (``GET /v1/salesReports``) are synchronous but brutally rate
limited — far stricter than the general ~3,600/hour budget, effectively a few
hundred fetches a day. Every response is cached, and past periods are cached
*forever* because Apple never rewrites them. A tool that loops over 90 days of
daily reports would exhaust the day's budget in one call, so the range cap is
enforced before the first request rather than discovered at request 200.

Two properties of the format cause silently wrong numbers if ignored:

* The body is **gzip TSV**, not JSON. httpx transparently decompresses when
  Apple sets ``Content-Encoding: gzip`` but not when it returns raw gzip bytes
  under ``application/a-gzip``, so both are handled by sniffing the magic bytes.
* **"Developer Proceeds" is per unit, not per row.** A row of 500 units at
  $0.70 proceeds is $350, not $0.70. Summing the column directly — the obvious
  reading — under-reports revenue by orders of magnitude on popular apps.

**Analytics reports** are asynchronous across four levels: request -> report ->
instance -> segment. An ONGOING request takes **24-48 hours** before any data
exists. Nothing here ever polls or blocks: each call advances the chain as far
as it can and returns the current state plus what to do next, so an LLM re-runs
the tool later instead of holding a tool call open for two days.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any

from storepilot.app_store.client import (
    DOCS_ASC_API,
    AscClient,
    attrs,
    resource_body,
)
from storepilot.config import settings
from storepilot.core.cache import DAILY, FOREVER, FileCache, monthly_policy
from storepilot.core.errors import (
    NotFoundError,
    StorePermissionError,
    ValidationError,
)
from storepilot.core.models import Freshness, Report, ReportRow, Store

SALES_SOURCE = "asc_sales"
ANALYTICS_SOURCE = "asc_analytics"

#: Apple publishes a day's sales the following day (usually by ~09:00 UTC).
SALES_LAG_DAYS = 1

#: Hard cap on how many report files one tool call may fetch. The sales endpoint
#: is rate limited severely enough that an unbounded backfill is a denial of
#: service against the user's own key.
MAX_SALES_FETCHES = 31

_GZIP_MAGIC = b"\x1f\x8b"


class Frequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


#: ``filter[version]`` is the single most error-prone parameter on this endpoint:
#: Apple returns a bare 400 when the version does not match the report type, and
#: the correct value differs per type *and* frequency with no discoverable rule.
#: These are ordered candidate lists — the first is tried, and a 400 falls
#: through to the next rather than surfacing Apple's unhelpful message.
#: ``None`` means "omit the parameter", which is correct for some combinations.
_VERSION_CANDIDATES: dict[tuple[str, str], tuple[str | None, ...]] = {
    ("SALES", "SUMMARY"): ("1_1", "1_0", None),
    ("SALES", "DETAILED"): ("1_1", "1_0", None),
    ("SUBSCRIPTION", "SUMMARY"): ("1_3", "1_2", "1_0", None),
    ("SUBSCRIPTION_EVENT", "SUMMARY"): ("1_3", "1_2", None),
    ("SUBSCRIBER", "DETAILED"): ("1_3", "1_2", None),
    ("SUBSCRIPTION_OFFER_CODE_REDEMPTION", "SUMMARY"): ("1_0", None),
    ("PRE_ORDER", "SUMMARY"): ("1_0", None),
    ("INSTALLS", "SUMMARY"): ("1_0", None),
}

_DEFAULT_VERSIONS: tuple[str | None, ...] = ("1_0", None)


def version_candidates(report_type: str, report_sub_type: str) -> tuple[str | None, ...]:
    return _VERSION_CANDIDATES.get(
        (report_type.upper(), report_sub_type.upper()), _DEFAULT_VERSIONS
    )


# --- Decoding ---------------------------------------------------------------


def maybe_gunzip(data: bytes) -> bytes:
    """Decompress only if the payload actually is gzip.

    Apple serves ``application/a-gzip``; whether the bytes reach us compressed
    depends on whether httpx recognised a ``Content-Encoding`` header, which
    varies. Sniffing the magic bytes makes both paths correct.
    """
    if data[:2] != _GZIP_MAGIC:
        return data
    try:
        return gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise ValidationError(
            "The report downloaded from App Store Connect is a truncated gzip file.",
            remedy="Re-run the tool — a partial download is the usual cause. If it repeats, "
            "clear the StorePilot cache so the bad blob is not served again.",
            details={"gzip_error": str(exc)},
        ) from exc


def iter_tsv(data: bytes) -> Iterator[dict[str, str]]:
    """Yield TSV rows as dicts keyed by the exact header Apple wrote.

    Headers are kept verbatim rather than normalized: Apple's sales report column
    names are stable and documented, and the finance reports carry a preamble
    that a normalizing reader would silently mangle.
    """
    text = maybe_gunzip(data).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t")
    for row in reader:
        if not row:
            continue
        if all((v or "").strip() == "" for v in row.values()):
            continue
        yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace(",", "").replace("$", "").strip()
    if not cleaned or cleaned in {"-", "N/A"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


_SALES_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y")


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in _SALES_DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


# --- Sales report parsing ---------------------------------------------------


def parse_sales_report(
    data: bytes,
    *,
    report_date: str,
    app_id: str | None = None,
    fallback_period: date | None = None,
) -> list[ReportRow]:
    """Parse a SALES/SUMMARY TSV into normalized rows.

    Emits three metrics per source row:

    ``units``
        The "Units" column as-is.
    ``proceeds``
        ``Units x Developer Proceeds`` — the actual money. Apple's column is a
        *per-unit* figure; summing it directly is the classic mistake and
        produces a total that looks plausible but is wrong by the unit count.
    ``proceeds_per_unit``
        The raw column, kept so a caller can audit the multiplication.

    Refunds arrive as negative unit counts and are left signed, so summing gives
    net rather than gross.
    """
    rows: list[ReportRow] = []
    for record in iter_tsv(data):
        apple_id = record.get("Apple Identifier") or record.get("Apple Id") or ""
        if app_id and apple_id and apple_id != app_id:
            continue
        row_app_id = apple_id or app_id or "account"

        period = (
            _to_date(record.get("Begin Date"))
            or fallback_period
            or _period_from_report_date(report_date)
        )
        country = record.get("Country Code") or None
        currency = record.get("Currency of Proceeds") or record.get("Customer Currency") or None

        units = _to_float(record.get("Units"))
        per_unit = _to_float(record.get("Developer Proceeds"))

        # (metric, value, carries_currency). "proceeds" is the multiplication
        # that turns Apple's per-unit column into actual money for the row.
        emitted: tuple[tuple[str, float | None, bool], ...] = (
            ("units", units, False),
            ("proceeds_per_unit", per_unit, True),
            (
                "proceeds",
                units * per_unit if units is not None and per_unit is not None else None,
                True,
            ),
        )
        for metric, value, money in emitted:
            if value is None:
                continue
            rows.append(
                ReportRow(
                    store=Store.APP_STORE,
                    app_id=row_app_id,
                    period=period,
                    metric=metric,
                    value=value,
                    dimension="country" if country else None,
                    dimension_value=country,
                    currency=currency if money else None,
                )
            )

    return rows


def _period_from_report_date(report_date: str) -> date:
    text = report_date.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return datetime.now(UTC).date()


# --- Report date validation -------------------------------------------------


def normalize_report_date(report_date: str, frequency: Frequency) -> str:
    """Validate the ``filter[reportDate]`` format for the given frequency.

    Apple's 400 for a mismatched format does not mention the format, so this is
    checked locally. Each frequency wants a different granularity, and WEEKLY in
    particular wants the **Sunday that ends** the week — a Monday is rejected.
    """
    text = report_date.strip()
    if frequency is Frequency.YEARLY:
        if not (len(text) == 4 and text.isdigit()):
            raise ValidationError(
                f"YEARLY sales reports need a year, got {report_date!r}.",
                remedy="Pass the year alone, e.g. '2025'.",
            )
        return text

    if frequency is Frequency.MONTHLY:
        try:
            parsed = datetime.strptime(text[:7], "%Y-%m")  # noqa: DTZ007
        except ValueError:
            raise ValidationError(
                f"MONTHLY sales reports need YYYY-MM, got {report_date!r}.",
                remedy="Pass the month, e.g. '2026-07'.",
            ) from None
        return parsed.strftime("%Y-%m")

    try:
        parsed_date = datetime.strptime(text, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        raise ValidationError(
            f"{frequency.value} sales reports need YYYY-MM-DD, got {report_date!r}.",
            remedy="Pass a full date, e.g. '2026-07-15'.",
        ) from None

    if frequency is Frequency.WEEKLY and parsed_date.weekday() != 6:
        sunday = parsed_date + timedelta(days=6 - parsed_date.weekday())
        raise ValidationError(
            f"WEEKLY sales reports are keyed by the Sunday that ends the week; "
            f"{text} is a {parsed_date.strftime('%A')}.",
            remedy=f"Use {sunday.isoformat()} (the Sunday of that week).",
        )
    return parsed_date.isoformat()


def sales_freshness(
    report_date: str,
    frequency: Frequency,
    *,
    today: date | None = None,
    row_count: int = 0,
    missing: bool = False,
) -> Freshness:
    """Explain whether this period can possibly have data yet.

    The failure this prevents: asking for today's sales, getting an empty report
    because Apple has not published it, and reporting "$0 revenue" as if sales
    had stopped.
    """
    today = today or datetime.now(UTC).date()
    period_start = _period_from_report_date(report_date)

    if frequency is Frequency.DAILY or frequency is Frequency.WEEKLY:
        available_from = period_start + timedelta(days=SALES_LAG_DAYS)
    elif frequency is Frequency.MONTHLY:
        month_end = _month_end(period_start)
        available_from = month_end + timedelta(days=5)
    else:
        available_from = date(period_start.year + 1, 1, 6)

    if today < available_from:
        return Freshness(
            as_of=today,
            requested_period=report_date,
            source=SALES_SOURCE,
            lag_days=(available_from - today).days,
            is_complete=False,
            caveat=(
                f"Apple has not published {frequency.value.lower()} sales for {report_date} yet "
                f"(expected around {available_from.isoformat()}). Any total shown is missing "
                f"data, not evidence of zero sales."
            ),
        )

    if missing or row_count == 0:
        return Freshness(
            as_of=today,
            requested_period=report_date,
            source=SALES_SOURCE,
            is_complete=False,
            caveat=(
                f"App Store Connect has no sales report for {report_date}. Apple omits the "
                f"report entirely for periods with no transactions, so this most likely means "
                f"zero sales — but it is also what a wrong vendor number produces. Confirm "
                f"STOREPILOT_ASC_VENDOR_NUMBER matches Payments and Financial Reports."
            ),
        )

    return Freshness(
        as_of=min(today, _period_end(period_start, frequency)),
        requested_period=report_date,
        source=SALES_SOURCE,
        lag_days=SALES_LAG_DAYS,
        is_complete=True,
    )


def _month_end(day: date) -> date:
    return date(day.year + (day.month == 12), (day.month % 12) + 1, 1) - timedelta(days=1)


def _period_end(start: date, frequency: Frequency) -> date:
    if frequency is Frequency.DAILY:
        return start
    if frequency is Frequency.WEEKLY:
        return start
    if frequency is Frequency.MONTHLY:
        return _month_end(start)
    return date(start.year, 12, 31)


# --- Sales fetching ---------------------------------------------------------


def _cache() -> FileCache:
    return FileCache(
        "app_store",
        root=settings.resolved_cache_dir,
        enabled=settings.cache_enabled,
    )


def vendor_number() -> str:
    number = (settings.asc_vendor_number or "").strip()
    if not number:
        raise ValidationError(
            "Sales reports need a vendor number and STOREPILOT_ASC_VENDOR_NUMBER is unset.",
            remedy=(
                "Find it in App Store Connect -> Payments and Financial Reports; it is the "
                "8-digit number shown next to your team name (it may be prefixed with '8'). "
                "Set STOREPILOT_ASC_VENDOR_NUMBER to it. It is not the same as the Team ID or "
                "the Issuer ID."
            ),
            doc_url=DOCS_ASC_API,
        )
    return number


def _cache_policy(report_date: str, frequency: Frequency, *, today: date | None = None) -> Any:
    """Past periods never change, so they are cached forever.

    This is what makes the severe rate limit survivable: a 30-day history costs
    30 requests once, and zero thereafter.
    """
    today = today or datetime.now(UTC).date()
    if frequency is Frequency.MONTHLY:
        return monthly_policy(report_date, today=today)
    period_start = _period_from_report_date(report_date)
    end = _period_end(period_start, frequency)
    return FOREVER if end + timedelta(days=SALES_LAG_DAYS + 1) < today else DAILY


def fetch_sales_report(
    client: AscClient,
    *,
    report_date: str,
    frequency: Frequency = Frequency.DAILY,
    report_type: str = "SALES",
    report_sub_type: str = "SUMMARY",
    version: str | None = None,
    vendor: str | None = None,
    today: date | None = None,
) -> tuple[bytes | None, str | None]:
    """Fetch one sales report, cached, walking the ``filter[version]`` candidates.

    Returns ``(payload, version_used)``. ``(None, None)`` means Apple has no
    report for that period — a 404 here is normal (Apple omits empty periods)
    and must not be treated as an error.
    """
    vendor = vendor or vendor_number()
    report_date = normalize_report_date(report_date, frequency)
    candidates = (version,) if version else version_candidates(report_type, report_sub_type)

    cache = _cache()
    policy = _cache_policy(report_date, frequency, today=today)
    base_key = f"sales/{vendor}/{report_type}/{report_sub_type}/{frequency.value}/{report_date}"

    cached = cache.get(base_key)
    if cached is not None:
        # An empty blob is the cached form of "Apple has no report for this
        # period" — worth caching, since re-asking costs the same scarce quota.
        return (cached or None), version or "cached"

    attempted: list[str] = []
    last_error: ValidationError | None = None

    for candidate in candidates:
        params: dict[str, Any] = {
            "filter[frequency]": frequency.value,
            "filter[reportDate]": report_date,
            "filter[reportSubType]": report_sub_type.upper(),
            "filter[reportType]": report_type.upper(),
            "filter[vendorNumber]": vendor,
        }
        if candidate is not None:
            params["filter[version]"] = candidate
        attempted.append(candidate or "<omitted>")

        try:
            payload = client.get_bytes(
                "/v1/salesReports",
                params=params,
                context=(
                    f"fetching {frequency.value} {report_type} report for {report_date}"
                    + (f" (version {candidate})" if candidate else "")
                ),
            )
        except NotFoundError:
            cache.set(base_key, b"", policy)
            return None, candidate
        except StorePermissionError as exc:
            raise StorePermissionError(
                f"This API key may not read sales reports ({exc.message}).",
                remedy=(
                    "Sales and finance reports need a key with the Admin, Finance, or Sales "
                    "role. Check the key's role in App Store Connect -> Users and Access -> "
                    "Integrations; App Manager and Developer keys can read apps and reviews "
                    "but are refused here."
                ),
                doc_url=exc.doc_url,
                details=exc.details,
            ) from exc
        except ValidationError as exc:
            # Apple answers a bad type/subtype/frequency/version combination with
            # a bare 400. Try the next candidate before giving up.
            last_error = exc
            continue

        cache.set(base_key, payload, policy)
        return payload, candidate

    raise ValidationError(
        f"App Store Connect rejected every known filter[version] for "
        f"{report_type}/{report_sub_type} at {frequency.value} frequency.",
        remedy=(
            "This combination of reportType, reportSubType, frequency and version is not one "
            "Apple accepts. Common working pairs: SALES+SUMMARY (version 1_1), "
            "SUBSCRIPTION+SUMMARY (version 1_3), SUBSCRIBER+DETAILED (version 1_3). "
            "Also confirm the vendor number is right — a wrong vendor number can surface as a "
            "400 rather than a 404."
        ),
        doc_url=DOCS_ASC_API,
        details={
            "versions_attempted": attempted,
            "report_type": report_type,
            "report_sub_type": report_sub_type,
            "frequency": frequency.value,
            "report_date": report_date,
            "apple_detail": (last_error.details.get("apple_detail") if last_error else None),
        },
    )


def get_sales(
    client: AscClient,
    *,
    report_date: str,
    frequency: Frequency = Frequency.DAILY,
    report_type: str = "SALES",
    report_sub_type: str = "SUMMARY",
    app_id: str | None = None,
    vendor: str | None = None,
    today: date | None = None,
) -> Report:
    """One period of sales, normalized and carrying its freshness caveat."""
    normalized = normalize_report_date(report_date, frequency)
    payload, used_version = fetch_sales_report(
        client,
        report_date=normalized,
        frequency=frequency,
        report_type=report_type,
        report_sub_type=report_sub_type,
        vendor=vendor,
        today=today,
    )
    rows = (
        parse_sales_report(payload, report_date=normalized, app_id=app_id)
        if payload
        else []
    )
    return Report(
        rows=rows,
        freshness=sales_freshness(
            normalized,
            frequency,
            today=today,
            row_count=len(rows),
            missing=payload is None,
        ),
        source_object=(
            f"salesReports:{report_type}/{report_sub_type}/{frequency.value}/{normalized}"
            + (f"?version={used_version}" if used_version else "")
        ),
    )


def daily_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValidationError(
            f"End date {end.isoformat()} is before start date {start.isoformat()}.",
            remedy="Pass the earlier date first.",
        )
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def check_range_size(days: int, *, cap: int = MAX_SALES_FETCHES) -> None:
    """Refuse a range that would burn the sales quota, before spending any of it."""
    if days > cap:
        raise ValidationError(
            f"Requested {days} daily sales reports in one call; the cap is {cap}.",
            remedy=(
                f"Apple rate limits /v1/salesReports far more aggressively than the rest of the "
                f"API — a few hundred fetches per day, shared across every tool using this key. "
                f"Ask for at most {cap} days at a time, or use frequency='MONTHLY' which "
                f"answers the same question in one request. Already-fetched days come from "
                f"cache and are free, so repeating a range you have pulled before is fine."
            ),
            details={"requested_days": days, "cap": cap},
        )


def get_sales_range(
    client: AscClient,
    *,
    start: date,
    end: date,
    app_id: str | None = None,
    vendor: str | None = None,
    today: date | None = None,
) -> Report:
    """Daily sales across a bounded date range, merged into one report."""
    days = daily_range(start, end)
    check_range_size(len(days))

    rows: list[ReportRow] = []
    caveats: list[str] = []
    missing: list[str] = []

    for day in days:
        report = get_sales(
            client,
            report_date=day.isoformat(),
            frequency=Frequency.DAILY,
            app_id=app_id,
            vendor=vendor,
            today=today,
        )
        rows.extend(report.rows)
        if not report.rows:
            missing.append(day.isoformat())
        warning = report.freshness.warning()
        if warning and warning not in caveats:
            caveats.append(warning)

    freshness = Freshness(
        as_of=end,
        requested_period=f"{start.isoformat()}..{end.isoformat()}",
        source=SALES_SOURCE,
        lag_days=SALES_LAG_DAYS,
        is_complete=not missing,
        caveat=(
            f"{len(missing)} of {len(days)} days have no report "
            f"({', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}). "
            + " ".join(caveats)
        ).strip()
        if missing or caveats
        else None,
    )
    return Report(rows=rows, freshness=freshness, source_object=f"salesReports:{len(days)} days")


# --- Analytics: the asynchronous four-level flow ----------------------------


class AnalyticsStage(str, Enum):
    """Where the request sits in Apple's pipeline. Each maps to one user action."""

    NOT_REQUESTED = "not_requested"
    PROVISIONING = "provisioning"
    REPORT_LISTED = "report_listed"
    INSTANCE_READY = "instance_ready"
    DATA_READY = "data_ready"
    STOPPED = "stopped"


ANALYTICS_CATEGORIES = (
    "APP_USAGE",
    "APP_STORE_ENGAGEMENT",
    "COMMERCE",
    "FRAMEWORK_USAGE",
    "PERFORMANCE",
)

ANALYTICS_GRANULARITIES = ("DAILY", "WEEKLY", "MONTHLY")

ACCESS_TYPES = ("ONE_TIME_SNAPSHOT", "ONGOING")


@dataclass
class AnalyticsProgress:
    """A resumable snapshot of the analytics pipeline for one app.

    Deliberately a *state report*, not a result: ONGOING requests take 24-48
    hours to produce their first data, so a tool that waited would either time
    out or hold a session open for two days. The caller re-runs the tool and the
    flow picks up exactly where it left off.
    """

    stage: AnalyticsStage
    app_id: str
    category: str
    granularity: str
    request_id: str | None = None
    access_type: str | None = None
    report_id: str | None = None
    report_name: str | None = None
    instance_id: str | None = None
    instance_date: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    report: Report | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def next_action(self) -> str:
        if self.stage is AnalyticsStage.NOT_REQUESTED:
            return (
                "No analytics request exists for this app. Re-run this tool with create=true "
                "to register one."
            )
        if self.stage is AnalyticsStage.PROVISIONING:
            return (
                "Apple has accepted the request but has not generated any report yet. This "
                "takes 24-48 hours for a new ONGOING request. Re-run this tool tomorrow — "
                "nothing needs to be resubmitted, and re-requesting would only reset the clock."
            )
        if self.stage is AnalyticsStage.REPORT_LISTED:
            return (
                f"The {self.category} report exists but has no {self.granularity} instances "
                f"yet. Apple generates instances a day behind; re-run this tool tomorrow."
            )
        if self.stage is AnalyticsStage.INSTANCE_READY:
            return (
                "An instance exists but Apple has not published its data segments yet. "
                "Re-run this tool shortly — segment generation lags the instance by minutes "
                "to hours."
            )
        if self.stage is AnalyticsStage.STOPPED:
            return (
                "Apple stopped this ONGOING request because it went unread for too long "
                "(stoppedDueToInactivity). Re-run with create=true to register a fresh one; "
                "the new one is subject to the same 24-48 hour wait."
            )
        return "Data is available and included below."

    def render(self) -> str:
        lines = [
            f"App Store analytics — app {self.app_id}, {self.category} @ {self.granularity}",
            f"Stage: {self.stage.value}",
        ]
        if self.request_id:
            lines.append(f"Request: {self.request_id} ({self.access_type or 'unknown access'})")
        if self.report_id:
            lines.append(f"Report: {self.report_name or self.report_id}")
        if self.instance_id:
            lines.append(f"Instance: {self.instance_id} ({self.instance_date or 'undated'})")
        if self.segments:
            total = sum(int(s.get("sizeInBytes") or 0) for s in self.segments)
            lines.append(f"Segments: {len(self.segments)} ({total:,} bytes)")
        lines.extend(f"Note: {note}" for note in self.notes)
        lines.append(f"Next: {self.next_action}")
        return "\n".join(lines)


def list_report_requests(client: AscClient, app_id: str) -> list[dict[str, Any]]:
    """Existing analytics report requests for an app.

    Checked before creating anything: a duplicate ONGOING request does not
    speed Apple up, and creating one repeatedly is how a caller ends up waiting
    forever without realising the first request already succeeded.
    """
    result = client.get_all(
        f"/v1/apps/{app_id}/analyticsReportRequests",
        params={"limit": 50},
        limit=50,
        context=f"listing analytics report requests for app {app_id}",
    )
    return list(result.data)


def create_report_request(
    client: AscClient,
    app_id: str,
    *,
    access_type: str = "ONGOING",
) -> dict[str, Any]:
    """Register an analytics report request.

    ONGOING by default: a ONE_TIME_SNAPSHOT covers only historical data up to
    the request, so a portfolio tool that wants tomorrow's numbers too would
    have to re-request daily.
    """
    if access_type.upper() not in ACCESS_TYPES:
        raise ValidationError(
            f"Unknown analytics access type {access_type!r}.",
            remedy=f"Use one of: {', '.join(ACCESS_TYPES)}.",
        )
    payload = client.post(
        "/v1/analyticsReportRequests",
        resource_body(
            "analyticsReportRequests",
            attributes={"accessType": access_type.upper()},
            relationships={"app": ("apps", app_id)},
        ),
        context=f"creating an analytics report request for app {app_id}",
    )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def advance_analytics(
    client: AscClient,
    app_id: str,
    *,
    category: str = "APP_USAGE",
    granularity: str = "DAILY",
    access_type: str = "ONGOING",
    create: bool = False,
    download: bool = True,
    max_segments: int = 3,
) -> AnalyticsProgress:
    """Walk the request -> report -> instance -> segment chain as far as it goes.

    Never blocks and never polls. Each level that is empty terminates the walk
    and returns the stage, so the caller knows whether to wait a day or a minute.
    """
    category = category.upper()
    granularity = granularity.upper()
    if category not in ANALYTICS_CATEGORIES:
        raise ValidationError(
            f"Unknown analytics category {category!r}.",
            remedy=f"Use one of: {', '.join(ANALYTICS_CATEGORIES)}.",
        )
    if granularity not in ANALYTICS_GRANULARITIES:
        raise ValidationError(
            f"Unknown analytics granularity {granularity!r}.",
            remedy=f"Use one of: {', '.join(ANALYTICS_GRANULARITIES)}.",
        )

    progress = AnalyticsProgress(
        stage=AnalyticsStage.NOT_REQUESTED,
        app_id=app_id,
        category=category,
        granularity=granularity,
    )

    # Level 1: the request.
    requests = list_report_requests(client, app_id)
    active = [r for r in requests if not attrs(r).get("stoppedDueToInactivity")]
    stopped = [r for r in requests if attrs(r).get("stoppedDueToInactivity")]

    if not active:
        if stopped and not create:
            progress.stage = AnalyticsStage.STOPPED
            progress.request_id = str(stopped[0].get("id"))
            return progress
        if not create:
            return progress
        created = create_report_request(client, app_id, access_type=access_type)
        progress.request_id = str(created.get("id") or "")
        progress.access_type = attrs(created).get("accessType") or access_type
        progress.stage = AnalyticsStage.PROVISIONING
        progress.notes.append(
            "Request registered. Apple begins generating reports within 24-48 hours; "
            "historical data appears with the first report."
        )
        return progress

    request = active[0]
    progress.request_id = str(request.get("id") or "")
    progress.access_type = attrs(request).get("accessType")
    progress.stage = AnalyticsStage.PROVISIONING

    # Level 2: reports within the request, filtered to the category.
    reports = client.get_all(
        f"/v1/analyticsReportRequests/{progress.request_id}/reports",
        params={"filter[category]": category},
        limit=50,
        context=f"listing {category} analytics reports",
    )
    if not reports.data:
        progress.notes.append(
            f"The request exists but Apple has published no {category} report for it yet."
        )
        return progress

    report_resource = reports.data[0]
    progress.report_id = str(report_resource.get("id") or "")
    progress.report_name = attrs(report_resource).get("name")
    progress.stage = AnalyticsStage.REPORT_LISTED

    # Level 3: instances at the requested granularity.
    instances = client.get_all(
        f"/v1/analyticsReports/{progress.report_id}/instances",
        params={"filter[granularity]": granularity},
        limit=10,
        context=f"listing {granularity} instances of report {progress.report_id}",
    )
    if not instances.data:
        return progress

    # Instances are returned oldest-first in practice; the newest is what a
    # "how is the app doing" question means, so pick by processingDate.
    instance = max(
        instances.data,
        key=lambda r: str(attrs(r).get("processingDate") or ""),
    )
    progress.instance_id = str(instance.get("id") or "")
    progress.instance_date = attrs(instance).get("processingDate")
    progress.stage = AnalyticsStage.INSTANCE_READY

    # Level 4: segments, each a pre-signed URL to a gzip TSV.
    segments = client.get_all(
        f"/v1/analyticsReportInstances/{progress.instance_id}/segments",
        limit=50,
        context=f"listing segments of instance {progress.instance_id}",
    )
    if not segments.data:
        return progress

    progress.segments = [
        {
            "id": s.get("id"),
            "url": attrs(s).get("url"),
            "checksum": attrs(s).get("checksum"),
            "sizeInBytes": attrs(s).get("sizeInBytes"),
        }
        for s in segments.data
    ]
    progress.stage = AnalyticsStage.DATA_READY

    if download:
        progress.report = download_segments(
            client,
            app_id,
            progress.segments[:max_segments],
            category=category,
            instance_date=progress.instance_date,
        )
        if len(progress.segments) > max_segments:
            progress.notes.append(
                f"Downloaded {max_segments} of {len(progress.segments)} segments; totals below "
                f"are partial. Raise max_segments to include the rest."
            )

    return progress


def download_segments(
    client: AscClient,
    app_id: str,
    segments: Sequence[Mapping[str, Any]],
    *,
    category: str,
    instance_date: str | None = None,
) -> Report:
    """Download and parse analytics segments into normalized rows.

    Segment URLs are pre-signed and time-limited, so the *parsed* result is what
    gets cached, keyed by the segment checksum Apple supplies — the checksum is
    stable across re-listings while the URL is not.
    """
    cache = _cache()
    rows: list[ReportRow] = []
    for segment in segments:
        url = segment.get("url")
        if not url:
            continue
        checksum = str(segment.get("checksum") or segment.get("id") or "")
        key = f"analytics/{app_id}/{category}/{checksum}"

        def fetch(target: str = str(url)) -> bytes:
            return client.download(target)

        payload = cache.get_or_fetch(key, fetch, FOREVER) if checksum else fetch()
        rows.extend(parse_analytics_segment(payload, app_id=app_id))

    as_of = None
    if instance_date:
        try:
            as_of = datetime.strptime(instance_date[:10], "%Y-%m-%d").date()  # noqa: DTZ007
        except ValueError:
            as_of = None

    return Report(
        rows=rows,
        freshness=Freshness(
            as_of=as_of,
            source=ANALYTICS_SOURCE,
            requested_period=instance_date,
            is_complete=bool(rows),
            caveat=(
                None
                if rows
                else "The segments downloaded contained no data rows for this instance."
            ),
        ),
        source_object=f"analytics:{category}",
    )


_ANALYTICS_DATE_COLUMNS = ("Date", "Processing Date", "Day")

#: Columns that describe the slice rather than measure it. Anything numeric that
#: is not one of these becomes a metric.
_ANALYTICS_DIMENSION_HINTS = (
    "app name",
    "app apple identifier",
    "territory",
    "platform version",
    "device",
    "source type",
    "page type",
    "event",
    "app version",
    "campaign",
)


def parse_analytics_segment(data: bytes, *, app_id: str) -> list[ReportRow]:
    """Parse an analytics segment TSV generically.

    The column set differs per report and Apple adds reports over time, so this
    resolves structurally rather than against a fixed schema: one date column,
    known dimension columns, and every remaining numeric column as a metric.
    A hard-coded schema would silently drop new metrics instead of surfacing them.
    """
    rows: list[ReportRow] = []
    for record in iter_tsv(data):
        date_key = next(
            (k for k in record if k in _ANALYTICS_DATE_COLUMNS),
            None,
        )
        period = _to_date(record.get(date_key or "")) or datetime.now(UTC).date()

        dimension: str | None = None
        dimension_value: str | None = None
        for key, value in record.items():
            if key.lower() in _ANALYTICS_DIMENSION_HINTS and value:
                dimension, dimension_value = key.lower().replace(" ", "_"), value
                break

        for key, value in record.items():
            if key == date_key or key.lower() in _ANALYTICS_DIMENSION_HINTS:
                continue
            number = _to_float(value)
            if number is None:
                continue
            rows.append(
                ReportRow(
                    store=Store.APP_STORE,
                    app_id=app_id,
                    period=period,
                    metric=key.strip().lower().replace(" ", "_"),
                    value=number,
                    dimension=dimension,
                    dimension_value=dimension_value,
                )
            )
    return rows
