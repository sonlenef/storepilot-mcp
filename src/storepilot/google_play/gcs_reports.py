"""Read Play's report CSVs out of ``gs://pubsite_prod_rev_*``.

Installs, ratings, crashes and earnings have **no REST API at all**. Google
publishes them only as CSV objects in a private Cloud Storage bucket attached to
the developer account, which is why "how much did this app make last month?" is
unanswerable in tooling that only speaks Android Publisher.

Division of labour: this module does network I/O and caching and nothing else.
All parsing lives in :mod:`storepilot.core.csv_reports`, which is pure and
offline-testable, so a Google column rename is fixed in one place.

Caching is not an optimization here so much as a correctness-preserving nicety:
a past month's report object is immutable, so ``monthly_policy`` pins it forever
and only the in-progress month is re-fetched daily.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from storepilot.config import settings
from storepilot.core import csv_reports
from storepilot.core.cache import DAILY, FileCache, monthly_policy
from storepilot.core.csv_reports import ReportKind
from storepilot.core.errors import (
    DOCS_PLAY_DOWNLOAD_REPORTS,
    NotFoundError,
    StorePermissionError,
    StorePilotError,
    ValidationError,
)
from storepilot.core.models import Report
from storepilot.google_play import auth

CACHE_NAMESPACE = "play_gcs"

#: Cap on how many earnings objects we merge for one month. Google normally
#: writes one per month, occasionally a couple; anything beyond this is a sign of
#: a wrong prefix rather than real data, and downloading them all would be slow.
MAX_EARNINGS_OBJECTS = 12


def bucket_name() -> str:
    """The configured reports bucket id, or a setup error that says where to find it."""
    raw = settings.google_reports_bucket
    if not raw or not raw.strip():
        raise ValidationError(
            "No Play reports bucket is configured, so installs, ratings and earnings "
            "cannot be read (no Play REST API exposes them).",
            remedy=(
                "In Play Console go to Download reports -> Statistics (or Financial "
                "reports) and click 'Copy Cloud Storage URI'. It looks like "
                "gs://pubsite_prod_rev_0123456789 or gs://pubsite_prod_<accountId>. Set "
                "STOREPILOT_GOOGLE_REPORTS_BUCKET to it (the gs:// prefix is optional). "
                f"Then, in Play Console -> Users and permissions -> {auth.service_account_email()} "
                "-> Account permissions, tick 'View app information and download bulk reports'. "
                "App-level grants do not reach the bucket."
            ),
            doc_url=DOCS_PLAY_DOWNLOAD_REPORTS,
        )
    # Accept a full URI, a bucket id, or a URI with a trailing path component.
    return raw.strip().removeprefix("gs://").strip("/").split("/", 1)[0]


def _cache() -> FileCache:
    return FileCache(
        CACHE_NAMESPACE,
        root=settings.resolved_cache_dir,
        enabled=settings.cache_enabled,
    )


def _classify_storage_error(
    exc: Exception,
    *,
    context: str,
    bucket: str,
    object_path: str | None = None,
) -> StorePilotError:
    """Translate a Cloud Storage failure, with bucket-specific remedies.

    A 403 here means something different from a 403 on the Publisher API. App-level
    permissions do not reach the bucket at all: it is unlocked by the ACCOUNT-level
    "download bulk reports" permission, and it lives in a Google-owned project, so
    sending the user to Cloud Console IAM would waste their time on a bucket they
    cannot administer.
    """
    error = auth.classify_google_error(exc, context=context)
    email = auth.service_account_email()

    if isinstance(error, StorePermissionError):
        error.remedy = (
            f"Bucket access is an ACCOUNT-level Play Console permission, not an app-level one, "
            f"and not something you grant in Cloud Console — the bucket lives in a Google-owned "
            f"project you cannot administer. In Play Console go to Users and permissions -> "
            f"{email} -> Account permissions and tick 'View app information and download bulk "
            f"reports (read-only)'; add 'View financial data' too for earnings. Granting apps "
            f"one by one under App permissions does NOT unlock the bucket. Note this permission "
            f"covers every app in the account — bulk reports have no per-app scope."
        )
        error.doc_url = DOCS_PLAY_DOWNLOAD_REPORTS
    elif isinstance(error, NotFoundError):
        target = f"gs://{bucket}/{object_path}" if object_path else f"gs://{bucket}"
        error.message = f"{target} does not exist."
        error.remedy = (
            "If the bucket id is wrong, re-copy it from Play Console -> Download reports -> "
            "'Copy Cloud Storage URI'. If the bucket is right, this report simply has not "
            "been generated: Play only writes a monthly object for apps that had activity, "
            "stats land 3-7 days late, and monthly earnings appear around the 5th of the "
            "following month."
        )
        error.doc_url = DOCS_PLAY_DOWNLOAD_REPORTS
    return error


# --- Object access -----------------------------------------------------------


def list_objects(prefix: str, *, max_results: int = 100) -> list[str]:
    """Object names under a prefix. Needed wherever the full name is not derivable."""
    bucket = bucket_name()
    client = auth.storage_client()
    try:
        blobs = client.list_blobs(bucket, prefix=prefix, max_results=max_results)
        return sorted(blob.name for blob in blobs)
    except Exception as exc:
        raise _classify_storage_error(
            exc, context=f"listing gs://{bucket}/{prefix}", bucket=bucket
        ) from exc


def list_objects_cached(prefix: str, *, month: str | date | None = None) -> list[str]:
    """``list_objects`` behind the blob cache, keyed by prefix.

    A listing for a closed month is as immutable as the objects it names, so this
    keeps the earnings path down to a single network round trip per month.
    """
    bucket = bucket_name()
    policy = monthly_policy(month) if month is not None else DAILY
    cache = _cache()
    raw = cache.get_or_fetch(
        f"listing/{bucket}/{prefix}",
        lambda: json.dumps(list_objects(prefix)).encode("utf-8"),
        policy,
    )
    try:
        names = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return list_objects(prefix)
    return [str(n) for n in names] if isinstance(names, list) else []


def fetch_object(object_path: str, *, month: str | date | None = None) -> bytes:
    """Download one report object, via the cache."""
    bucket = bucket_name()
    policy = monthly_policy(month) if month is not None else DAILY

    def download() -> bytes:
        client = auth.storage_client()
        try:
            blob = client.bucket(bucket).blob(object_path)
            data = blob.download_as_bytes()
        except Exception as exc:
            raise _classify_storage_error(
                exc,
                context=f"downloading gs://{bucket}/{object_path}",
                bucket=bucket,
                object_path=object_path,
            ) from exc
        return data

    return _cache().get_or_fetch(f"{bucket}/{object_path}", download, policy)


# --- Per-app stats reports ---------------------------------------------------


def _get_stats_report(
    kind: ReportKind,
    parser: Any,
    package_name: str,
    month: str | date,
    *,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    object_path = csv_reports.report_object_path(
        kind, month, package_name=package_name, dimension=dimension
    )
    data = fetch_object(object_path, month=month)
    return parser(
        data, package_name=package_name, month=month, dimension=dimension, today=today
    )


def get_installs(
    package_name: str,
    month: str | date,
    *,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    """Installs/uninstalls/active devices for one app for one month."""
    return _get_stats_report(
        ReportKind.INSTALLS,
        csv_reports.parse_installs,
        package_name,
        month,
        dimension=dimension,
        today=today,
    )


def get_ratings(
    package_name: str,
    month: str | date,
    *,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    """Daily and cumulative average rating for one app for one month."""
    return _get_stats_report(
        ReportKind.RATINGS,
        csv_reports.parse_ratings,
        package_name,
        month,
        dimension=dimension,
        today=today,
    )


def get_crashes(
    package_name: str,
    month: str | date,
    *,
    dimension: str = "overview",
    today: date | None = None,
) -> Report:
    """Daily crash and ANR counts from the reports bucket.

    Distinct from :mod:`storepilot.google_play.reporting`: these are raw event
    counts, whereas Android Vitals reports user-normalized rates. The rates are
    what Google judges an app on; these counts are useful for spotting a spike.
    """
    return _get_stats_report(
        ReportKind.CRASHES,
        csv_reports.parse_crashes,
        package_name,
        month,
        dimension=dimension,
        today=today,
    )


# --- Account-level earnings --------------------------------------------------


def get_earnings(
    month: str | date,
    *,
    package_name: str | None = None,
    today: date | None = None,
) -> Report:
    """Merged earnings for a month, optionally narrowed to one app.

    Earnings objects carry a trailing account/report id that cannot be derived
    from the month alone, so this lists the bucket by prefix and merges every
    object that matches — guessing a filename here would report zero revenue for
    an account that earned plenty.

    Amounts stay in their merchant currency; the caller must not assume USD.
    """
    prefix = csv_reports.report_object_prefix(ReportKind.EARNINGS, month)
    names = [n for n in list_objects_cached(prefix, month=month) if n.lower().endswith(".csv")]

    if not names:
        # An absent object is normal for a month that has not been published yet;
        # report_freshness turns that into the right caveat rather than "$0".
        return Report(
            rows=[],
            freshness=csv_reports.report_freshness(
                ReportKind.EARNINGS, month, today=today, row_count=0
            ),
            source_object=f"{prefix}*.csv (no objects found)",
        )

    rows = []
    used: list[str] = []
    for name in names[:MAX_EARNINGS_OBJECTS]:
        data = fetch_object(name, month=month)
        report = csv_reports.parse_earnings(
            data, month=month, package_name=package_name, today=today
        )
        rows.extend(report.rows)
        used.append(name)

    freshness = csv_reports.report_freshness(
        ReportKind.EARNINGS, month, today=today, row_count=len(rows)
    )
    if len(names) > MAX_EARNINGS_OBJECTS:
        # Silently summing the first N objects would under-report revenue by
        # whatever the skipped ones hold, and the total would look perfectly
        # ordinary. Say it out loud instead.
        skipped = len(names) - MAX_EARNINGS_OBJECTS
        truncation_caveat = (
            f"{len(names)} earnings objects matched this month's prefix and only the first "
            f"{MAX_EARNINGS_OBJECTS} were read — the totals below EXCLUDE {skipped} file(s) "
            f"and are therefore too low. Google normally writes one object per month, so "
            f"this usually means the prefix is matching more than it should."
        )
        freshness = freshness.model_copy(
            update={
                "is_complete": False,
                "caveat": " ".join(filter(None, [freshness.caveat, truncation_caveat])),
            }
        )

    return Report(rows=rows, freshness=freshness, source_object=", ".join(used))


def earnings_by_currency(report: Report) -> dict[str, float]:
    """Total earnings per currency code — never collapse currencies into one number."""
    totals: dict[str, float] = {}
    for row in report.rows:
        if row.metric != "earnings":
            continue
        code = row.currency or "unknown"
        totals[code] = totals.get(code, 0.0) + row.value
    return totals


def earnings_by_app(report: Report) -> dict[str, dict[str, float]]:
    """Per-app, per-currency earnings totals: ``{app_id: {currency: amount}}``."""
    out: dict[str, dict[str, float]] = {}
    for row in report.rows:
        if row.metric != "earnings":
            continue
        code = row.currency or "unknown"
        per_app = out.setdefault(row.app_id, {})
        per_app[code] = per_app.get(code, 0.0) + row.value
    return out
