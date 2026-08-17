"""Google Play read-only MCP tools.

Every tool returns prose-and-table text rather than a JSON dump: the string goes
straight to an LLM and then to a human, and a raw payload forces the model to
re-derive the interesting part (is this crash rate bad?) on every call.

Three house rules, each earned from a specific failure mode:

1. Nothing raises. A tool catches ``StorePilotError`` at its boundary and returns
   ``render_error``, because an MCP client shows a traceback to the user as-is
   and a traceback contains no remedy.
2. Freshness caveats are printed before the numbers. Reporting "0 installs" for a
   month whose CSV has not been published yet is the most damaging thing this
   server can do, and it looks exactly like a real collapse in revenue.
3. Money is always printed with its currency. Play pays out in merchant currency
   and a portfolio can span several; a bare "1,240.55" is a wrong answer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp.server import MCPServer

from storepilot.core import csv_reports
from storepilot.core.errors import (
    StorePilotError,
    play_reviews_empty_hint,
    render_error,
)
from storepilot.core.guards import UNTRUSTED_CONTENT_NOTE, untrusted
from storepilot.core.models import App, Freshness, PortfolioEntry, Report, Store
from storepilot.google_play import auth, gcs_reports, reporting
from storepilot.server import READ_ONLY

#: Installs metrics worth putting in a summary, in the order they read best.
_INSTALL_HEADLINES: tuple[tuple[str, str], ...] = (
    ("daily_device_installs", "Device installs"),
    ("install_events", "Install events"),
    ("daily_device_uninstalls", "Device uninstalls"),
    ("uninstall_events", "Uninstall events"),
    ("daily_device_upgrades", "Device upgrades"),
    ("update_events", "Update events"),
    ("daily_user_installs", "User installs"),
    ("daily_user_uninstalls", "User uninstalls"),
)


# --- Formatting helpers ------------------------------------------------------


def _number(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _money(amount: float, currency: str) -> str:
    """Amount with its currency code. Never assume USD — Play pays in merchant currency."""
    return f"{amount:,.2f} {currency}"


def _freshness_lines(*sources: Freshness | None) -> list[str]:
    """Deduplicated staleness warnings, to print above the numbers they qualify."""
    seen: dict[str, None] = {}
    for freshness in sources:
        if freshness is None:
            continue
        warning = freshness.warning()
        if warning:
            seen.setdefault(warning, None)
    return [f"! {w}" for w in seen]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Fixed-width table. Alignment is what makes a portfolio scan readable."""
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out)


def _guard(fn: Callable[[], str]) -> str:
    """Run a tool body, converting any failure into an actionable string.

    Broad by design: an unexpected exception escaping into an MCP client is worse
    than an ugly-but-honest error line, and ``render_error`` degrades safely on
    anything that is not a StorePilotError.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - tools must never surface a traceback
        return render_error(exc)


def _default_month(today: date | None = None) -> str:
    """Last complete month — the most recent one that can have a full report."""
    today = today or datetime.now(UTC).date()
    first = today.replace(day=1)
    previous = first - timedelta(days=1)
    return f"{previous.year:04d}-{previous.month:02d}"


def _pretty_month(month: str) -> str:
    ym = csv_reports.normalize_month(month)
    return f"{ym[:4]}-{ym[4:]}"


# --- Report summarizing ------------------------------------------------------


def _installs_summary(report: Report) -> list[str]:
    totals = csv_reports.summarize(report.rows)
    lines: list[str] = []
    for metric, label in _INSTALL_HEADLINES:
        if metric in totals:
            lines.append(f"  {label}: {_number(totals[metric])}")
    if "active_device_installs" in totals:
        # Active devices is a daily snapshot, not a flow: summing it is meaningless,
        # so report the latest day instead of the total.
        active = [r for r in report.rows if r.metric == "active_device_installs"]
        if active:
            latest = max(active, key=lambda r: r.period)
            lines.append(
                f"  Active device installs: {_number(latest.value)} "
                f"(snapshot on {latest.period.isoformat()}, not a monthly sum)"
            )
    return lines


def _ratings_summary(report: Report) -> list[str]:
    daily = [r for r in report.rows if r.metric == "daily_average_rating"]
    total = [r for r in report.rows if r.metric == "total_average_rating"]
    lines: list[str] = []
    if total:
        latest = max(total, key=lambda r: r.period)
        lines.append(
            f"  Lifetime average rating: {latest.value:.2f} "
            f"(as of {latest.period.isoformat()})"
        )
    if daily:
        mean = sum(r.value for r in daily) / len(daily)
        lines.append(f"  Average of daily ratings this month: {mean:.2f} ({len(daily)} days rated)")
    return lines


def _latest_rating(report: Report) -> float | None:
    rows = [r for r in report.rows if r.metric == "total_average_rating"]
    if not rows:
        rows = [r for r in report.rows if r.metric == "daily_average_rating"]
    if not rows:
        return None
    return max(rows, key=lambda r: r.period).value


def _install_total(report: Report) -> float | None:
    totals = csv_reports.summarize(report.rows)
    for metric in ("daily_device_installs", "install_events", "daily_user_installs"):
        if metric in totals:
            return totals[metric]
    return None


# --- Registration ------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    """Attach every Google Play read-only tool to the server."""

    @mcp.tool(annotations=READ_ONLY)
    def play_list_apps() -> str:
        """List every Google Play app this StorePilot install can access.

        Start here. Other Play tools take a `package_name` and this is how to
        learn it without the user having to remember exact package strings.

        Source: Play Developer Reporting `apps.search`. It lists apps the service
        account has been granted access to in Play Console AND that have at least
        one published release — an app that only exists as a draft will not
        appear, which is a Google limitation, not a permission problem.
        """

        def run() -> str:
            apps = reporting.search_apps()
            if not apps:
                email = auth.service_account_email()
                return (
                    "No apps are visible to StorePilot.\n"
                    f"The service account {email} is authenticated but has not been granted "
                    "access to any app, or the apps have no published release yet.\n"
                    "Fix: Play Console -> Users and permissions -> invite "
                    f"{email} -> grant it 'View app information' on each app. "
                    "Run setup_doctor for a full diagnosis."
                )
            rows = [[app.name, app.app_id] for app in apps]
            return f"{len(apps)} Google Play app(s):\n\n" + _table(["App", "Package name"], rows)

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def play_get_vitals(package_name: str, days: int = 28) -> str:
        """Android Vitals for one app: crash and ANR rates vs Google's thresholds.

        Answers "is this app in trouble with Google?". Both figures are the
        USER-PERCEIVED, user-weighted rates — the exact numbers Play Console
        judges an app on — expressed as a percentage of distinct daily users.

        Google's bad-behaviour thresholds: user-perceived crash rate 1.09%,
        user-perceived ANR rate 0.47%. Exceeding either can cost the app store
        visibility and put a warning on its listing, so the verdict per metric is
        stated explicitly.

        Args:
            package_name: e.g. "com.example.app". Get it from `play_list_apps`.
            days: trailing window. 28 (default) and 7 map onto Google's own
                rolling user-weighted averages and are the most trustworthy;
                other values are averaged locally, weighted by daily user counts.

        Data trails real time by roughly 2-3 days, and Google suppresses vitals
        entirely for apps below a minimum daily user count — for a low-traffic
        app "no data" does not mean "no crashes".
        """

        def run() -> str:
            result = reporting.query_vitals(package_name, days=days)
            lines = [f"Android Vitals for {package_name} — trailing {days} days"]
            lines.extend(_freshness_lines(result.freshness))
            lines.append("")

            for key in reporting.DEFAULT_METRICS:
                reading = result.reading(key)
                if reading is None:
                    continue
                if reading.error:
                    lines.append(f"{reading.label}: unavailable — {reading.error}")
                    continue
                lines.append(f"{reading.label}: {reading.format_value()} -> {reading.verdict()}")
                detail = [f"metric: {reading.metric_name}"]
                if reading.user_weighted:
                    detail.append("user-weighted")
                if reading.distinct_users:
                    detail.append(f"~{_number(reading.distinct_users)} distinct users")
                if reading.latest_covered_day:
                    detail.append(f"through {reading.latest_covered_day.isoformat()}")
                lines.append(f"  ({', '.join(detail)})")

            flags = result.flags
            lines.append("")
            if flags:
                lines.append(
                    "VERDICT: this app EXCEEDS Google's bad-behaviour threshold — "
                    + "; ".join(flags)
                    + ". Apps over these thresholds risk reduced Play Store visibility. "
                    "Use play_get_anomalies to see whether Google flagged a specific spike."
                )
            elif any(r.value is not None for r in result.readings.values()):
                lines.append("VERDICT: within Google's thresholds on every metric checked.")
            else:
                lines.append(
                    "VERDICT: no vitals data returned for this window — see the caveat above."
                )
            return "\n".join(lines)

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def play_get_anomalies(package_name: str) -> str:
        """Vitals anomalies Google's own detection flagged for an app.

        These are deviations Google considered significant — a crash-rate spike on
        a specific Android version or device model, for example — with the value
        observed and the range that was expected. Higher signal than a threshold
        check alone, because it catches a regression that is bad relative to the
        app's own baseline while still under the absolute threshold.

        An empty result is genuinely good news: it means nothing unusual was
        detected recently.

        Args:
            package_name: e.g. "com.example.app". Get it from `play_list_apps`.
        """

        def run() -> str:
            anomalies = reporting.list_anomalies(package_name)
            if not anomalies:
                return (
                    f"No vitals anomalies detected for {package_name}. Google did not flag any "
                    "unusual deviation in crash, ANR or other vitals metrics recently."
                )
            lines = [f"{len(anomalies)} anomaly/anomalies detected for {package_name}:", ""]
            for anomaly in anomalies:
                metric_set = anomaly.metric_set or "vitals"
                lines.append(f"- [{metric_set}] {anomaly.describe()}")
            lines.append("")
            lines.append(
                "Each line is a metric whose value fell outside its expected range. "
                "Dimensions in brackets show where it was concentrated (device, Android "
                "version, app version), which is usually the fastest route to the cause."
            )
            return "\n".join(lines)

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def play_get_stats(package_name: str, month: str = "") -> str:
        """Installs and ratings for one app for one calendar month.

        This data exists ONLY as CSV files in the Play reports Cloud Storage
        bucket — no Play REST API serves it — so it needs
        STOREPILOT_GOOGLE_REPORTS_BUCKET configured and the service account
        granted 'Storage Object Viewer' on that bucket. Run setup_doctor if this
        returns a permission error.

        Args:
            package_name: e.g. "com.example.app". Get it from `play_list_apps`.
            month: the month as "YYYY-MM", e.g. "2026-07". Defaults to the last
                complete month. Play stats land 3-7 days late, so the current
                month is always partial and is labelled as such.

        Installs and ratings are fetched independently: if one report is missing
        the other is still reported, with a note explaining the gap.
        """

        def run() -> str:
            target = _pretty_month(month) if month else _default_month()
            lines = [f"Play stats for {package_name} — {target}"]
            body: list[str] = []
            freshness: list[Freshness | None] = []

            try:
                installs = gcs_reports.get_installs(package_name, target)
                freshness.append(installs.freshness)
                body.append("Installs:")
                summary = _installs_summary(installs)
                body.extend(summary or ["  (report contained no install rows)"])
            except StorePilotError as exc:
                body.append("Installs: unavailable")
                body.append("  " + render_error(exc).replace("\n", "\n  "))

            body.append("")

            try:
                ratings = gcs_reports.get_ratings(package_name, target)
                freshness.append(ratings.freshness)
                body.append("Ratings:")
                summary = _ratings_summary(ratings)
                body.extend(summary or ["  (report contained no rating rows)"])
            except StorePilotError as exc:
                body.append("Ratings: unavailable")
                body.append("  " + render_error(exc).replace("\n", "\n  "))

            lines.extend(_freshness_lines(*freshness))
            lines.append("")
            lines.extend(body)
            return "\n".join(lines)

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def play_get_earnings(month: str = "", package_name: str = "") -> str:
        """Google Play earnings for a calendar month, account-wide or for one app.

        Answers "how much did this make?" — which no Play REST API can, because
        earnings exist only as CSVs in the reports Cloud Storage bucket. Requires
        STOREPILOT_GOOGLE_REPORTS_BUCKET and 'Storage Object Viewer' on it.

        Args:
            month: the month as "YYYY-MM", e.g. "2026-07". Defaults to the last
                complete month. Google publishes a month's earnings around the
                5th of the following month; before then the report does not exist
                and a zero total means "not published", not "no revenue" — this
                tool says so explicitly rather than reporting 0.
            package_name: optional, e.g. "com.example.app", to narrow the report
                to a single app. Leave empty for the whole account.

        Amounts are in MERCHANT currency (what actually reaches the payout) and
        are reported per currency. Totals across different currencies are never
        added together.
        """

        def run() -> str:
            target = _pretty_month(month) if month else _default_month()
            filter_app = package_name.strip() or None
            report = gcs_reports.get_earnings(target, package_name=filter_app)

            scope = filter_app or "whole account"
            lines = [f"Play earnings — {target} ({scope})"]
            lines.extend(_freshness_lines(report.freshness))
            lines.append("")

            totals = gcs_reports.earnings_by_currency(report)
            if not totals:
                lines.append(
                    "No earnings rows found. If the caveat above does not explain it, the "
                    "account may have had no transactions this month, or the service account "
                    "lacks 'View financial data' — run setup_doctor."
                )
                return "\n".join(lines)

            lines.append("Total:")
            for currency, amount in sorted(totals.items(), key=lambda kv: -abs(kv[1])):
                lines.append(f"  {_money(amount, currency)}")

            if not filter_app:
                per_app = gcs_reports.earnings_by_app(report)
                if len(per_app) > 1:
                    lines.append("")
                    lines.append("By product/app:")
                    rows = [
                        [app_id, ", ".join(_money(v, c) for c, v in sorted(by_currency.items()))]
                        for app_id, by_currency in sorted(
                            per_app.items(),
                            key=lambda kv: -sum(abs(v) for v in kv[1].values()),
                        )
                    ]
                    lines.append(_table(["Product / app", "Earnings"], rows))

            lines.append("")
            lines.append(
                f"Rows: {len(report.rows)} transaction line(s) from {report.source_object}. "
                "Amounts are merchant-currency (post-Google-fee payout basis), not list price."
            )
            return "\n".join(lines)

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def play_list_reviews(
        package_name: str,
        min_rating: int = 1,
        max_rating: int = 5,
        limit: int = 50,
    ) -> str:
        """Recent user reviews for an app, from the Android Publisher API.

        IMPORTANT LIMITATIONS, all imposed by Google and not fixable here:
        - Only PRODUCTION-track reviews are returned. Testing-track feedback is
          invisible to this API.
        - Only reviews that CARRY A COMMENT are returned. A bare star rating with
          no text does not appear, so this is not a way to count ratings — use
          `play_get_stats` for the rating average.
        - Only roughly the last 7 days of reviews are retained by the endpoint.
        - When the service account lacks the 'Reply to reviews' permission the API
          returns HTTP 200 with an EMPTY list rather than an error. An empty
          result is therefore ambiguous, and this tool says so instead of
          claiming the app has no reviews.

        Args:
            package_name: e.g. "com.example.app". Get it from `play_list_apps`.
            min_rating: keep reviews with at least this many stars (1-5).
            max_rating: keep reviews with at most this many stars (1-5).
                Set both to 1 to triage the angriest users first.
            limit: maximum reviews to return (1-100).
        """

        def run() -> str:
            low = max(1, min(5, min_rating))
            high = max(1, min(5, max_rating))
            if low > high:
                low, high = high, low
            capped = max(1, min(100, limit))

            client = auth.publisher_client()
            try:
                payload = (
                    client.reviews()
                    .list(packageName=package_name, maxResults=capped)
                    .execute()
                ) or {}
            except Exception as exc:
                raise auth.classify_google_error(
                    exc, context="calling reviews.list", package_name=package_name
                ) from exc

            raw = payload.get("reviews") or []
            if not raw:
                return play_reviews_empty_hint(
                    package_name, service_account_email=auth.service_account_email()
                )

            kept: list[str] = []
            filtered_out = 0
            for entry in raw:
                comments = entry.get("comments") or []
                user_comment: dict[str, Any] = {}
                developer_replied = False
                for comment in comments:
                    if "userComment" in comment:
                        user_comment = comment["userComment"] or {}
                    if "developerComment" in comment:
                        developer_replied = True
                rating = user_comment.get("starRating")
                if rating is None or not (low <= int(rating) <= high):
                    filtered_out += 1
                    continue

                when = ""
                seconds = (user_comment.get("lastModified") or {}).get("seconds")
                if seconds:
                    try:
                        when = datetime.fromtimestamp(int(seconds), tz=UTC).date().isoformat()
                    except (TypeError, ValueError, OSError):
                        when = ""
                # Everything below is written by whoever installed the app: the
                # review body, and the reviewer's Google account display name.
                # `.replace("\n", " ")` was not enough — str.splitlines also
                # breaks on \r, \x85, U+2028 and friends, and authorName was not
                # sanitized at all. A display name containing a newline could
                # emit lines that read as StorePilot's own output ("[done] ...",
                # "call again with confirm=True") straight into the model's
                # context. untrusted() flattens all of it to one line, so
                # injected text can never start a line.
                text = untrusted(user_comment.get("text"), limit=1000)
                meta = [f"{'*' * int(rating)} ({rating}/5)"]
                if when:
                    meta.append(when)
                author = untrusted(entry.get("authorName"), limit=60)
                if author:
                    meta.append(author)
                version = untrusted(user_comment.get("appVersionName"), limit=40)
                device = untrusted(
                    (user_comment.get("deviceMetadata") or {}).get("productName"), limit=40
                )
                if version:
                    meta.append(f"v{version}")
                if device:
                    meta.append(device)
                if developer_replied:
                    meta.append("replied")
                kept.append(f"- [{' | '.join(meta)}]\n  {text or '(no text)'}")

            header = (
                f"{len(kept)} review(s) for {package_name} "
                f"(rating {low}-{high}, fetched {len(raw)})"
            )
            if not kept:
                return (
                    f"{header}\nAll {filtered_out} fetched review(s) fell outside the "
                    f"{low}-{high} star filter. Widen min_rating/max_rating to see them."
                )
            note = (
                "Production-track, comment-bearing reviews from roughly the last 7 days only "
                "— a Google Play API limitation, not a filter applied here."
            )
            return "\n".join([header, note, UNTRUSTED_CONTENT_NOTE, "", *kept])

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def play_portfolio_health(month: str = "", days: int = 28) -> str:
        """One-call health scan of EVERY app in the Play account.

        The flagship overview: for each app it gathers Android Vitals (crash and
        ANR vs Google's thresholds), the latest average rating, and the month's
        installs, then flags the apps that need attention. Use this to answer
        "how is my portfolio doing?" without naming a single package.

        Args:
            month: stats month as "YYYY-MM" for the installs/rating columns.
                Defaults to the last complete month.
            days: vitals window in days; 28 (default) matches Play Console.

        Each app is fetched independently and a failure on one app becomes an
        error note in its row rather than failing the whole report — a missing
        permission on a single app must not hide the other twenty. Requests are
        throttled to stay under the Reporting API's 10 queries/second quota, so a
        large portfolio takes a few seconds.

        Rating and install columns need the reports bucket
        (STOREPILOT_GOOGLE_REPORTS_BUCKET); without it the vitals columns still
        work and the rest reads "n/a".
        """

        def run() -> str:
            target = _pretty_month(month) if month else _default_month()
            apps = reporting.search_apps()
            if not apps:
                email = auth.service_account_email()
                return (
                    "No apps are visible to StorePilot, so there is no portfolio to scan.\n"
                    f"Fix: Play Console -> Users and permissions -> invite {email} and grant "
                    "it app access. Run setup_doctor for a full diagnosis."
                )

            entries: list[PortfolioEntry] = []
            notes: list[str] = []
            caveats: list[Freshness | None] = []
            bucket_error: str | None = None

            for app in apps:
                entry, app_notes, freshness, bucket_error = _scan_app(
                    app, target, days, bucket_error
                )
                entries.append(entry)
                notes.extend(app_notes)
                caveats.extend(freshness)

            return _render_portfolio(entries, target, days, notes, caveats, bucket_error)

        return _guard(run)


# --- Portfolio internals -----------------------------------------------------


def _scan_app(
    app: App,
    month: str,
    days: int,
    bucket_error: str | None,
) -> tuple[PortfolioEntry, list[str], list[Freshness | None], str | None]:
    """Gather one app's row. Degrades per data source rather than raising."""
    entry = PortfolioEntry(store=Store.GOOGLE_PLAY, app_id=app.app_id, name=app.name)
    notes: list[str] = []
    freshness: list[Freshness | None] = []

    try:
        vitals = reporting.query_vitals(app.app_id, days=days)
        entry.crash_rate = vitals.snapshot.crash_rate
        entry.anr_rate = vitals.snapshot.anr_rate
        entry.freshness = vitals.freshness
        freshness.append(vitals.freshness)
        if vitals.snapshot.exceeds_crash_threshold:
            entry.health_flags.append("CRASH")
        if vitals.snapshot.exceeds_anr_threshold:
            entry.health_flags.append("ANR")
        for reading in vitals.readings.values():
            if reading.error:
                notes.append(f"{app.app_id}: vitals {reading.key} — {reading.error}")
    except StorePilotError as exc:
        entry.health_flags.append("?vitals")
        notes.append(f"{app.app_id}: vitals unavailable — {exc.message}")

    # One bucket-level failure (unset bucket, missing IAM role) applies to every
    # app, so record it once and stop retrying it per app.
    if bucket_error is None:
        try:
            ratings = gcs_reports.get_ratings(app.app_id, month)
            entry.average_rating = _latest_rating(ratings)
            freshness.append(ratings.freshness)
        except StorePilotError as exc:
            if exc.kind in {"validation_error", "permission_error", "credentials_error"}:
                bucket_error = render_error(exc)
            else:
                notes.append(f"{app.app_id}: ratings unavailable — {exc.message}")

    if bucket_error is None:
        try:
            installs = gcs_reports.get_installs(app.app_id, month)
            entry.installs_last_30d = _install_total(installs)
            freshness.append(installs.freshness)
        except StorePilotError as exc:
            if exc.kind in {"validation_error", "permission_error", "credentials_error"}:
                bucket_error = render_error(exc)
            else:
                notes.append(f"{app.app_id}: installs unavailable — {exc.message}")

    return entry, notes, freshness, bucket_error


def _render_portfolio(
    entries: list[PortfolioEntry],
    month: str,
    days: int,
    notes: list[str],
    caveats: list[Freshness | None],
    bucket_error: str | None,
) -> str:
    crash_limit = reporting.CRASH_RATE_THRESHOLD_PERCENT
    anr_limit = reporting.ANR_RATE_THRESHOLD_PERCENT

    def rate(value: float | None, limit: float) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}%" + ("  !" if value > limit else "")

    def status(entry: PortfolioEntry) -> str:
        if entry.health_flags:
            return ",".join(entry.health_flags)
        if entry.crash_rate is None and entry.anr_rate is None:
            # "ok" would claim this app was checked and passed. It was not
            # measured at all, which is a different thing and must not read as
            # a clean bill of health.
            return "no vitals"
        return "ok"

    rows = [
        [
            entry.name[:34],
            entry.app_id,
            rate(entry.crash_rate, crash_limit),
            rate(entry.anr_rate, anr_limit),
            f"{entry.average_rating:.2f}" if entry.average_rating is not None else "n/a",
            _number(entry.installs_last_30d),
            status(entry),
        ]
        for entry in sorted(
            entries,
            key=lambda e: (-len([f for f in e.health_flags if f in {"CRASH", "ANR"}]), e.name.lower()),
        )
    ]

    flagged = [e for e in entries if {"CRASH", "ANR"} & set(e.health_flags)]
    lines = [
        f"Google Play portfolio health — {len(entries)} app(s)",
        f"Vitals: trailing {days} days. Installs/rating: {month}.",
        (
            f"Thresholds: user-perceived crash {crash_limit}%, ANR {anr_limit}% "
            f"('!' marks a breach)."
        ),
    ]
    lines.extend(_freshness_lines(*caveats))
    lines.append("")
    lines.append(
        _table(
            ["App", "Package", "Crash", "ANR", "Rating", f"Installs {month}", "Flags"],
            rows,
        )
    )
    lines.append("")

    if flagged:
        lines.append(f"RED FLAGS — {len(flagged)} app(s) exceed a Google threshold:")
        for entry in flagged:
            which = " and ".join(f for f in entry.health_flags if f in {"CRASH", "ANR"})
            lines.append(
                f"  - {entry.name} ({entry.app_id}): {which} above threshold. "
                f"Run play_get_anomalies('{entry.app_id}') to localize the spike."
            )
    else:
        lines.append("No app exceeds Google's crash or ANR threshold.")

    unmeasured = [
        e for e in entries if not e.health_flags and e.crash_rate is None and e.anr_rate is None
    ]
    if unmeasured:
        lines.append(
            f"{len(unmeasured)} app(s) returned no vitals data and were NOT checked against "
            f"the thresholds ({', '.join(e.app_id for e in unmeasured)}). Google suppresses "
            f"vitals below a minimum daily user count, so this is normal for low-traffic apps."
        )

    if bucket_error:
        lines.append("")
        lines.append("Rating and install columns are unavailable for every app:")
        lines.append("  " + bucket_error.replace("\n", "\n  "))

    if notes:
        lines.append("")
        lines.append("Per-app issues:")
        lines.extend(f"  - {note}" for note in notes[:40])
        if len(notes) > 40:
            lines.append(f"  ... and {len(notes) - 40} more")

    return "\n".join(lines)
