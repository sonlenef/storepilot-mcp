"""Google Play report CSV parsing.

Installs, ratings, crashes and earnings are NOT available from any Play REST API.
They exist only as CSV files in a private GCS bucket (``gs://pubsite_prod_rev_*``).
Two properties of those files drive the whole design of this module:

1. The CSVs are UTF-16 encoded with a BOM, not UTF-8. Decoding naively yields
   either mojibake or a UnicodeDecodeError.
2. Google adds, renames and reorders columns without notice (the earnings report
   columns changed in July 2026). Nothing here may depend on column position or
   column count — every lookup is by header name, whitespace/case tolerant, and a
   miss raises a ValidationError that lists the headers actually present.

This module deliberately performs no network I/O: it accepts bytes or a binary
file object so it is unit-testable offline. ``google_play/gcs_reports.py`` owns
fetching and calls in here.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import IO

from storepilot.core.errors import ValidationError, missing_column_error
from storepilot.core.models import Freshness, Report, ReportRow, Store

_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF8_BOM = b"\xef\xbb\xbf"

# Play stats land 3-7 days after the fact; a month's earnings are published
# around the 5th of the following month.
STATS_LAG_DAYS = 7
EARNINGS_PUBLISH_DAY = 5

SOURCE = "play_gcs_reports"


class ReportKind(str, Enum):
    INSTALLS = "installs"
    RATINGS = "ratings"
    CRASHES = "crashes"
    EARNINGS = "earnings"
    SALES = "sales"
    REVIEWS = "reviews"


#: Report kinds that are account-wide rather than per-app.
ACCOUNT_LEVEL_KINDS = frozenset({ReportKind.EARNINGS, ReportKind.SALES})


def normalize_month(month: str | date) -> str:
    """Normalize "2026-07", "202607", "2026/07" or a date into "202607"."""
    if isinstance(month, date):
        return f"{month.year:04d}{month.month:02d}"
    digits = re.sub(r"\D", "", str(month))
    if len(digits) != 6:
        raise ValidationError(
            f"Invalid month {month!r}.",
            remedy="Pass the month as 'YYYY-MM', for example '2026-07'.",
        )
    mon = int(digits[4:6])
    if not 1 <= mon <= 12:
        raise ValidationError(
            f"Invalid month {month!r}: month component must be 01-12.",
            remedy="Pass the month as 'YYYY-MM', for example '2026-07'.",
        )
    return digits


def month_bounds(month: str | date) -> tuple[date, date]:
    """First and last day of the month, inclusive."""
    ym = normalize_month(month)
    year, mon = int(ym[:4]), int(ym[4:6])
    first = date(year, mon, 1)
    last = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return first, last


# --- Object path builders ---------------------------------------------------


def report_object_path(
    kind: ReportKind | str,
    month: str | date,
    *,
    package_name: str | None = None,
    dimension: str = "overview",
) -> str:
    """Build the GCS object path for a report.

    Layout inside ``gs://pubsite_prod_rev_<id>/``::

        stats/installs/installs_<package>_<YYYYMM>_<dimension>.csv
        stats/ratings/ratings_<package>_<YYYYMM>_<dimension>.csv
        stats/crashes/crashes_<package>_<YYYYMM>_<dimension>.csv
        earnings/earnings_<YYYYMM>*.csv      (account-wide, suffix varies)
        sales/salesreport_<YYYYMM>.csv       (account-wide)
        reviews/reviews_<package>_<YYYYMM>.csv

    Earnings objects carry a trailing account/report id that is not derivable, so
    for that kind use :func:`report_object_prefix` and list the bucket instead.
    """
    kind = ReportKind(kind)
    ym = normalize_month(month)

    if kind is ReportKind.SALES:
        return f"sales/salesreport_{ym}.csv"
    if kind is ReportKind.EARNINGS:
        raise ValidationError(
            "Earnings objects have an undeterminable suffix.",
            remedy=(
                "Use report_object_prefix(ReportKind.EARNINGS, month) and list bucket "
                "objects under that prefix, then parse each match."
            ),
        )
    if not package_name:
        raise ValidationError(
            f"{kind.value} reports are per-app but no package_name was given.",
            remedy="Pass package_name, e.g. 'com.example.app'.",
        )
    if kind is ReportKind.REVIEWS:
        return f"reviews/reviews_{package_name}_{ym}.csv"
    return f"stats/{kind.value}/{kind.value}_{package_name}_{ym}_{dimension}.csv"


def report_object_prefix(
    kind: ReportKind | str,
    month: str | date | None = None,
    *,
    package_name: str | None = None,
) -> str:
    """Build a GCS list prefix, for kinds whose full object name is not derivable."""
    kind = ReportKind(kind)
    ym = normalize_month(month) if month is not None else ""

    if kind is ReportKind.EARNINGS:
        return f"earnings/earnings_{ym}" if ym else "earnings/"
    if kind is ReportKind.SALES:
        return f"sales/salesreport_{ym}" if ym else "sales/"
    if kind is ReportKind.REVIEWS:
        base = f"reviews/reviews_{package_name}_" if package_name else "reviews/"
        return f"{base}{ym}" if ym else base
    base = f"stats/{kind.value}/{kind.value}_"
    if package_name:
        base += f"{package_name}_"
        if ym:
            base += ym
    return base


# --- Decoding and row iteration ---------------------------------------------


def decode_report_bytes(data: bytes) -> str:
    """Decode a Play report to text.

    Play writes these files as UTF-16 with a BOM. UTF-8 and BOM-less UTF-16 are
    handled too so a future format change does not break every tool at once.
    """
    if data.startswith((_UTF16_LE_BOM, _UTF16_BE_BOM)):
        return data.decode("utf-16")
    if data.startswith(_UTF8_BOM):
        return data.decode("utf-8-sig")
    head = data[:512]
    if head.count(b"\x00") > len(head) // 4:
        # Dense NUL bytes mean 16-bit code units without a BOM.
        return data.decode("utf-16-be" if head[:1] == b"\x00" else "utf-16-le")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "Report file is neither valid UTF-16 nor UTF-8.",
            remedy=(
                "Re-download the object from the reports bucket; a truncated or "
                "partially written download is the usual cause."
            ),
            details={"decode_error": str(exc)},
        ) from exc


def normalize_header(name: str) -> str:
    """Fold a CSV header to a stable lookup key.

    "Daily Device Installs" -> "daily_device_installs"
    "Amount (Merchant Currency)" -> "amount_merchant_currency"
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return cleaned.strip("_")


def iter_report_rows(source: bytes | IO[bytes] | str) -> Iterator[dict[str, str]]:
    """Yield each CSV row as a dict keyed by normalized header name.

    Accepts raw bytes, a binary file object, or already-decoded text. Column
    order and count are irrelevant to consumers of this iterator by design.
    """
    if isinstance(source, str):
        text = source
    else:
        raw = source if isinstance(source, bytes) else source.read()
        text = decode_report_bytes(raw)

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return
    keys = [normalize_header(h) for h in header]
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        yield {k: (values[i].strip() if i < len(values) else "") for i, k in enumerate(keys)}


def read_headers(source: bytes | IO[bytes] | str) -> list[str]:
    """Normalized header names of a report, for diagnostics."""
    for row in iter_report_rows(source):
        return list(row)
    return []


# --- Column resolution ------------------------------------------------------


def find_column(
    row: dict[str, str],
    candidates: Sequence[str],
    *,
    source: str = "report",
    required: bool = True,
) -> str | None:
    """Resolve the first candidate header present in ``row``.

    Exact normalized match first, then a token-subset match so a Google rename
    from "Daily Device Installs" to "Daily Device Installs (new)" still resolves.

    Matching is on whole tokens, never substrings: substring matching silently
    confuses "Date" with "Update events" and "Install events" with "Uninstall
    events", which would attribute real numbers to the wrong metric. For the same
    reason single-token candidates ("Country", "Device") must match exactly —
    they are too ambiguous to fuzzy-match against a wider header.
    """
    normalized = [normalize_header(c) for c in candidates]
    for key in normalized:
        if key in row:
            return key
    for key in normalized:
        wanted = set(key.split("_"))
        if len(wanted) < 2:
            continue
        for actual in row:
            if wanted <= set(actual.split("_")):
                return actual
    if required:
        raise missing_column_error(list(candidates), sorted(row), source=source)
    return None


def get_value(row: dict[str, str], candidates: Sequence[str], *, source: str = "report") -> str:
    column = find_column(row, candidates, source=source)
    return row.get(column or "", "")


def parse_float(value: str | None) -> float | None:
    """Parse a numeric cell, tolerating thousands separators and blanks."""
    if value is None:
        return None
    cleaned = value.strip().replace(",", "").replace("%", "")
    if not cleaned or cleaned in {"-", "N/A", "NA"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y%m%d")


def parse_date(value: str | None) -> date | None:
    """Parse a date cell. Stats reports use ISO; earnings use "Jul 1, 2026"."""
    if not value:
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            # Report cells are calendar dates with no zone; attaching one would invent data.
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


# --- Freshness --------------------------------------------------------------


def report_freshness(
    kind: ReportKind | str,
    month: str | date,
    *,
    today: date | None = None,
    row_count: int = 0,
) -> Freshness:
    """Describe how complete a month's report can possibly be right now.

    Prevents the worst failure in this domain: reporting "0 installs" for a month
    whose CSV has not been published yet, as if the app had collapsed.
    """
    kind = ReportKind(kind)
    today = today or datetime.now(UTC).date()
    ym = normalize_month(month)
    first, last = month_bounds(ym)
    period = f"{ym[:4]}-{ym[4:]}"

    if kind in (ReportKind.EARNINGS, ReportKind.SALES):
        available_from = date(
            last.year + (last.month == 12),
            (last.month % 12) + 1,
            EARNINGS_PUBLISH_DAY,
        )
        if today < available_from:
            return Freshness(
                as_of=today,
                requested_period=period,
                source=SOURCE,
                lag_days=(available_from - today).days,
                is_complete=False,
                caveat=(
                    f"Earnings for {period} are not published yet — Google releases them "
                    f"around {available_from.isoformat()}. Any total shown is partial or "
                    f"zero because the report does not exist, not because revenue was zero."
                ),
            )
        return Freshness(as_of=today, requested_period=period, source=SOURCE, is_complete=True)

    complete_after = last + timedelta(days=STATS_LAG_DAYS)
    if today <= complete_after:
        covered_through = min(last, today - timedelta(days=3))
        incomplete = covered_through < last
        return Freshness(
            as_of=covered_through if covered_through >= first else None,
            requested_period=period,
            source=SOURCE,
            lag_days=STATS_LAG_DAYS,
            is_complete=not incomplete,
            caveat=(
                f"{kind.value} data for {period} is still filling in. Play stats land 3-7 days "
                f"late, so figures are complete only after {complete_after.isoformat()}."
            )
            if incomplete
            else None,
        )

    if row_count == 0:
        return Freshness(
            as_of=today,
            requested_period=period,
            source=SOURCE,
            lag_days=STATS_LAG_DAYS,
            is_complete=False,
            caveat=(
                f"No rows found for {period} even though the reporting window has closed. "
                f"The app likely had no data that month, or the report object is missing "
                f"from the bucket."
            ),
        )
    return Freshness(as_of=last, requested_period=period, source=SOURCE, is_complete=True)


# --- Normalization into ReportRow -------------------------------------------

_DATE_COLUMNS = ("Date", "Transaction Date", "Order Charged Date", "Day")
_PACKAGE_COLUMNS = ("Package Name", "Product id", "Product ID", "Package")

#: Header candidates per emitted metric. Order matters: first match wins.
INSTALLS_METRICS: dict[str, tuple[str, ...]] = {
    "daily_device_installs": ("Daily Device Installs",),
    "daily_device_uninstalls": ("Daily Device Uninstalls",),
    "daily_device_upgrades": ("Daily Device Upgrades",),
    "active_device_installs": ("Active Device Installs",),
    "daily_user_installs": ("Daily User Installs",),
    "daily_user_uninstalls": ("Daily User Uninstalls",),
    "total_user_installs": ("Total User Installs",),
    "install_events": ("Install events",),
    "uninstall_events": ("Uninstall events",),
    "update_events": ("Update events",),
}

RATINGS_METRICS: dict[str, tuple[str, ...]] = {
    "daily_average_rating": ("Daily Average Rating",),
    "total_average_rating": ("Total Average Rating",),
}

CRASHES_METRICS: dict[str, tuple[str, ...]] = {
    "daily_crashes": ("Daily Crashes",),
    "daily_anrs": ("Daily ANRs",),
}

#: Dimension column names keyed by the dimension slug used in the object path.
DIMENSION_COLUMNS: dict[str, tuple[str, ...]] = {
    "country": ("Country", "Country / Region", "Buyer Country"),
    "device": ("Device",),
    "os_version": ("Android OS Version", "OS Version"),
    "app_version": ("App Version Code", "App Version"),
    "language": ("Language",),
    "carrier": ("Carrier",),
    "tablets": ("Device",),
}


def _rows_for_metrics(
    source: bytes | IO[bytes] | str,
    *,
    package_name: str,
    metrics: dict[str, tuple[str, ...]],
    dimension: str,
    source_label: str,
) -> list[ReportRow]:
    out: list[ReportRow] = []
    resolved: dict[str, str | None] | None = None
    dimension_column: str | None = None

    for raw in iter_report_rows(source):
        if resolved is None:
            resolved = {
                metric: find_column(raw, cands, source=source_label, required=False)
                for metric, cands in metrics.items()
            }
            if not any(resolved.values()):
                raise missing_column_error(
                    [c[0] for c in metrics.values()],
                    sorted(raw),
                    source=source_label,
                )
            if dimension != "overview":
                dimension_column = find_column(
                    raw,
                    DIMENSION_COLUMNS.get(dimension, (dimension,)),
                    source=source_label,
                    required=False,
                )

        date_column = find_column(raw, _DATE_COLUMNS, source=source_label, required=False)
        period = parse_date(raw.get(date_column or "", ""))
        if period is None:
            continue
        dim_value = raw.get(dimension_column or "") or None

        for metric, column in resolved.items():
            if column is None:
                continue
            value = parse_float(raw.get(column))
            if value is None:
                continue
            out.append(
                ReportRow(
                    store=Store.GOOGLE_PLAY,
                    app_id=package_name,
                    period=period,
                    metric=metric,
                    value=value,
                    dimension=None if dimension == "overview" else dimension,
                    dimension_value=dim_value,
                )
            )
    return out


def parse_installs(
    source: bytes | IO[bytes] | str,
    *,
    package_name: str,
    month: str | date,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    """Parse ``stats/installs/installs_<package>_<YYYYMM>_<dimension>.csv``."""
    object_path = report_object_path(
        ReportKind.INSTALLS, month, package_name=package_name, dimension=dimension
    )
    rows = _rows_for_metrics(
        source,
        package_name=package_name,
        metrics=INSTALLS_METRICS,
        dimension=dimension,
        source_label=object_path,
    )
    return Report(
        rows=rows,
        freshness=report_freshness(ReportKind.INSTALLS, month, today=today, row_count=len(rows)),
        source_object=object_path,
    )


def parse_ratings(
    source: bytes | IO[bytes] | str,
    *,
    package_name: str,
    month: str | date,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    """Parse ``stats/ratings/ratings_<package>_<YYYYMM>_<dimension>.csv``."""
    object_path = report_object_path(
        ReportKind.RATINGS, month, package_name=package_name, dimension=dimension
    )
    rows = _rows_for_metrics(
        source,
        package_name=package_name,
        metrics=RATINGS_METRICS,
        dimension=dimension,
        source_label=object_path,
    )
    return Report(
        rows=rows,
        freshness=report_freshness(ReportKind.RATINGS, month, today=today, row_count=len(rows)),
        source_object=object_path,
    )


def parse_crashes(
    source: bytes | IO[bytes] | str,
    *,
    package_name: str,
    month: str | date,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    """Parse ``stats/crashes/crashes_<package>_<YYYYMM>_<dimension>.csv``."""
    object_path = report_object_path(
        ReportKind.CRASHES, month, package_name=package_name, dimension=dimension
    )
    rows = _rows_for_metrics(
        source,
        package_name=package_name,
        metrics=CRASHES_METRICS,
        dimension=dimension,
        source_label=object_path,
    )
    return Report(
        rows=rows,
        freshness=report_freshness(ReportKind.CRASHES, month, today=today, row_count=len(rows)),
        source_object=object_path,
    )


_EARNINGS_AMOUNT_COLUMNS = (
    "Amount (Merchant Currency)",
    "Merchant Amount",
    "Amount Merchant Currency",
)
_EARNINGS_CURRENCY_COLUMNS = ("Merchant Currency", "Currency")
_EARNINGS_PRODUCT_COLUMNS = ("Product id", "Product ID", "Package Name", "Sku Id")


def parse_earnings(
    source: bytes | IO[bytes] | str,
    *,
    month: str | date,
    package_name: str | None = None,
    today: date | None = None,
) -> Report:
    """Parse an ``earnings/earnings_<YYYYMM>*.csv`` object.

    Earnings are account-wide: one row per transaction across every app. Rows are
    emitted with ``app_id`` set to the product id when present, so callers can
    group by app; pass ``package_name`` to filter to a single app.

    Amounts are taken from the merchant-currency column (what actually lands in
    the payout) and the currency is carried on each row rather than assumed USD.
    """
    object_path = f"{report_object_prefix(ReportKind.EARNINGS, month)}*.csv"
    rows: list[ReportRow] = []
    amount_column: str | None = None
    currency_column: str | None = None
    product_column: str | None = None
    date_column: str | None = None
    resolved = False

    for raw in iter_report_rows(source):
        if not resolved:
            amount_column = find_column(raw, _EARNINGS_AMOUNT_COLUMNS, source=object_path)
            currency_column = find_column(
                raw, _EARNINGS_CURRENCY_COLUMNS, source=object_path, required=False
            )
            product_column = find_column(
                raw, _EARNINGS_PRODUCT_COLUMNS, source=object_path, required=False
            )
            date_column = find_column(raw, _DATE_COLUMNS, source=object_path, required=False)
            resolved = True

        value = parse_float(raw.get(amount_column or ""))
        if value is None:
            continue
        app_id = raw.get(product_column or "") or "account"
        if package_name and not app_id.startswith(package_name):
            continue
        period = parse_date(raw.get(date_column or "")) or month_bounds(month)[0]
        rows.append(
            ReportRow(
                store=Store.GOOGLE_PLAY,
                app_id=package_name or app_id,
                period=period,
                metric="earnings",
                value=value,
                currency=(raw.get(currency_column or "") or None),
            )
        )

    return Report(
        rows=rows,
        freshness=report_freshness(ReportKind.EARNINGS, month, today=today, row_count=len(rows)),
        source_object=object_path,
    )


def summarize(rows: Iterable[ReportRow]) -> dict[str, float]:
    """Sum every metric across rows — the shape most tools return to the LLM."""
    totals: dict[str, float] = {}
    for row in rows:
        totals[row.metric] = totals.get(row.metric, 0.0) + row.value
    return totals
