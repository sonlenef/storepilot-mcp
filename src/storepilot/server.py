"""StorePilot MCP server entry point.

Phase 1 scope: Google Play read-only tools + setup_doctor.
Adapters register their tools only when their credentials are configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from storepilot.config import settings
from storepilot.core.errors import (
    DOCS_PLAY_DOWNLOAD_REPORTS,
    DOCS_PLAY_GETTING_STARTED,
    DOCS_PLAY_PERMISSIONS,
    ApiNotEnabledError,
    StorePilotError,
)

mcp = MCPServer("storepilot")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

Status = Literal["ok", "warn", "fail", "skip"]

_ICON: dict[str, str] = {"ok": "[ok]", "warn": "[warn]", "fail": "[fail]", "skip": "[skip]"}

#: Package used only to probe whether the Android Publisher API responds at all.
#: It intentionally does not exist, so a reachable API answers 404 rather than
#: touching any real app.
_PROBE_PACKAGE = "com.storepilot.doctor.probe.invalid"


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    remedy: str | None = None
    doc_url: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{_ICON[self.status]} {self.name}: {self.detail}"]
        if self.remedy:
            label = "Note" if self.status == "ok" else "Fix"
            lines.append(f"      {label}: {self.remedy}")
        if self.doc_url:
            lines.append(f"      Docs: {self.doc_url}")
        return "\n".join(lines)


def _from_error(name: str, exc: BaseException, *, status: Status = "fail") -> Check:
    if isinstance(exc, StorePilotError):
        return Check(name, status, exc.message, remedy=exc.remedy, doc_url=exc.doc_url)
    return Check(
        name,
        status,
        f"{type(exc).__name__}: {exc}",
        remedy="Unexpected failure — please report this with the message above.",
    )


def _check_play_credentials(checks: list[Check]) -> Any:
    """Step 1: key file exists, parses, and yields the service account email."""
    from storepilot.google_play import auth

    try:
        info = auth.load_service_account_info()
    except Exception as exc:  # noqa: BLE001 - every step reports rather than raises
        checks.append(_from_error("Credentials", exc))
        return None

    checks.append(
        Check(
            "Credentials",
            "ok",
            f"service account {info.client_email} loaded from {info.path} "
            f"(cloud project: {info.project_id or 'unknown'})",
            remedy=(
                f"Grant this email access in Play Console -> Users and permissions if you have "
                f"not already: {info.client_email}"
            ),
        )
    )
    return info


def _check_reporting_api(checks: list[Check], info: Any) -> list[dict[str, Any]]:
    """Step 3 (run early): apps.search proves reachability and yields the app list."""
    from storepilot.google_play import auth

    try:
        client = auth.reporting_client()
        response = client.apps().search(pageSize=100).execute()
    except Exception as exc:  # noqa: BLE001
        error = (
            exc
            if isinstance(exc, StorePilotError)
            else auth.classify_google_error(exc, context="calling Play Developer Reporting apps.search")
        )
        if isinstance(error, ApiNotEnabledError) and info is not None:
            error.remedy = (
                f"Enable the Play Developer Reporting API at {info.enable_reporting_api_url} — "
                f"it is a separate API from Android Publisher and needs enabling on its own."
            )
        checks.append(_from_error("Play Developer Reporting API", error))
        return []

    apps = response.get("apps", []) or []
    if not apps:
        checks.append(
            Check(
                "Play Developer Reporting API",
                "warn",
                "reachable, but it reports zero accessible apps",
                remedy=(
                    "The API works but this service account sees no apps. In Play Console -> "
                    "Users and permissions, grant it app access. Note the Reporting API only "
                    "lists apps that have a published release."
                ),
                doc_url=DOCS_PLAY_PERMISSIONS,
            )
        )
        return []

    names = [a.get("packageName", "?") for a in apps]
    preview = ", ".join(names[:5]) + (f" (+{len(names) - 5} more)" if len(names) > 5 else "")
    checks.append(
        Check(
            "Play Developer Reporting API",
            "ok",
            f"reachable — {len(names)} app(s) visible: {preview}",
            data={"packages": names},
        )
    )
    return apps


def _check_publisher_api(checks: list[Check], packages: list[str]) -> None:
    """Step 2: separate "API not enabled in Cloud" from "not invited to Play Console"."""
    from storepilot.google_play import auth

    probe = packages[0] if packages else _PROBE_PACKAGE
    is_real = bool(packages)

    try:
        client = auth.publisher_client()
        client.reviews().list(packageName=probe, maxResults=1).execute()
    except Exception as exc:  # noqa: BLE001
        error = (
            exc
            if isinstance(exc, StorePilotError)
            else auth.classify_google_error(
                exc, context="calling Android Publisher reviews.list", package_name=probe
            )
        )
        from storepilot.core.errors import NotFoundError

        if isinstance(error, NotFoundError) and not is_real:
            checks.append(
                Check(
                    "Android Publisher API",
                    "ok",
                    "reachable and credentials accepted (probe package correctly returned 404)",
                )
            )
            return
        checks.append(_from_error("Android Publisher API", error))
        return

    checks.append(Check("Android Publisher API", "ok", f"reachable, queried {probe}"))


def _check_reviews_permission(checks: list[Check], packages: list[str], info: Any) -> None:
    """Step 4: the silent trap — reviews.list returns 200 + empty list without permission."""
    from storepilot.core.errors import play_reviews_empty_hint
    from storepilot.google_play import auth

    if not packages:
        checks.append(
            Check(
                "Reviews permission",
                "skip",
                "no app available to test against",
                remedy="Resolve the app-list check above, then re-run setup_doctor.",
            )
        )
        return

    package = packages[0]
    try:
        client = auth.publisher_client()
        response = client.reviews().list(packageName=package, maxResults=5).execute()
    except Exception as exc:  # noqa: BLE001
        error = (
            exc
            if isinstance(exc, StorePilotError)
            else auth.classify_google_error(
                exc, context="calling reviews.list", package_name=package
            )
        )
        checks.append(_from_error("Reviews permission", error, status="warn"))
        return

    reviews = response.get("reviews", []) or []
    if reviews:
        checks.append(
            Check("Reviews permission", "ok", f"granted — {len(reviews)} review(s) read from {package}")
        )
        return

    email = info.client_email if info is not None else None
    checks.append(
        Check(
            "Reviews permission",
            "warn",
            f"reviews.list returned an EMPTY list for {package} — this is ambiguous",
            remedy=play_reviews_empty_hint(package, service_account_email=email),
            doc_url=DOCS_PLAY_PERMISSIONS,
        )
    )


def _check_reports_bucket(checks: list[Check]) -> None:
    """Step 5: the GCS reports bucket holding installs/ratings/earnings CSVs."""
    from storepilot.google_play import auth

    bucket_name = settings.google_reports_bucket
    if not bucket_name:
        checks.append(
            Check(
                "Reports bucket",
                "warn",
                "STOREPILOT_GOOGLE_REPORTS_BUCKET is not set — installs, ratings and earnings "
                "are unavailable (no Play REST API exposes them)",
                remedy=(
                    "Play Console -> Download reports -> Statistics (or Financial reports) -> "
                    "'Copy Cloud Storage URI'. It looks like gs://pubsite_prod_rev_0123456789 or "
                    "gs://pubsite_prod_<accountId>. "
                    "Set STOREPILOT_GOOGLE_REPORTS_BUCKET to the bucket id without the gs:// "
                    "prefix. The service account also needs the ACCOUNT-level Play Console permission "
                    "'View app information and download bulk reports' — app-level grants do not reach the bucket."
                ),
                doc_url=DOCS_PLAY_DOWNLOAD_REPORTS,
            )
        )
        return

    name = bucket_name.removeprefix("gs://").strip("/")
    try:
        client = auth.storage_client()
        blobs = list(client.list_blobs(name, max_results=5))
    except Exception as exc:  # noqa: BLE001
        error = (
            exc
            if isinstance(exc, StorePilotError)
            else auth.classify_google_error(exc, context=f"listing gs://{name}")
        )
        if not isinstance(error, StorePilotError) or error.kind in {"permission_error", "not_found"}:
            checks.append(
                Check(
                    "Reports bucket",
                    "fail",
                    f"cannot read gs://{name}: {error}",
                    remedy=(
                        f"In Play Console -> Users and permissions -> "
                        f"{auth.service_account_email()} -> Account permissions, tick 'View app "
                        f"information and download bulk reports (read-only)', and 'View financial "
                        f"data' for earnings. This is an ACCOUNT-level permission: granting apps "
                        f"individually under App permissions does not unlock the bucket, and "
                        f"neither does any Cloud Console IAM role — the bucket belongs to a "
                        f"Google-owned project. Also confirm the bucket id matches the one shown "
                        f"under Play Console -> Download reports."
                    ),
                    doc_url=DOCS_PLAY_DOWNLOAD_REPORTS,
                )
            )
            return
        checks.append(_from_error("Reports bucket", error))
        return

    if not blobs:
        checks.append(
            Check(
                "Reports bucket",
                "warn",
                f"gs://{name} is readable but empty",
                remedy=(
                    "Confirm this is the bucket shown by Play Console -> Download reports. Note "
                    "reports land 3-7 days late, and monthly earnings appear around the 5th of "
                    "the following month."
                ),
                doc_url=DOCS_PLAY_DOWNLOAD_REPORTS,
            )
        )
        return

    checks.append(
        Check("Reports bucket", "ok", f"gs://{name} readable ({blobs[0].name} and others found)")
    )


def _google_play_checks() -> list[Check]:
    checks: list[Check] = []
    if not settings.google_play_enabled:
        return [
            Check(
                "Google Play",
                "skip",
                "not configured",
                remedy=(
                    "Set STOREPILOT_GOOGLE_CREDENTIALS to the path of a service account JSON key. "
                    "Full setup: create a Google Cloud project, enable the Android Publisher and "
                    "Play Developer Reporting APIs, create a service account, download its JSON "
                    "key, then invite the service account email in Play Console -> Users and "
                    "permissions."
                ),
                doc_url=DOCS_PLAY_GETTING_STARTED,
            )
        ]

    info = _check_play_credentials(checks)

    reporting_checks: list[Check] = []
    apps = _check_reporting_api(reporting_checks, info) if info else []
    packages = [a.get("packageName") for a in apps if a.get("packageName")]

    if info is None:
        for name in (
            "Android Publisher API",
            "Play Developer Reporting API",
            "Reviews permission",
            "Reports bucket",
        ):
            checks.append(
                Check(
                    name,
                    "skip",
                    "cannot test until the credentials check above passes",
                    remedy="Fix the credentials check above first, then re-run setup_doctor.",
                )
            )
        return checks

    _check_publisher_api(checks, packages)
    checks.extend(reporting_checks)
    _check_reviews_permission(checks, packages, info)
    _check_reports_bucket(checks)
    return checks


def _app_store_checks() -> list[Check]:
    from storepilot.app_store.tools import check_setup

    return [Check(**entry) for entry in check_setup()]


@mcp.tool(annotations=READ_ONLY)
def setup_doctor() -> str:
    """Diagnose StorePilot's store credentials and report exactly what is missing.

    Runs every setup step independently and reports all of them, so a single
    failure never hides the rest. For Google Play it verifies: the service account
    key parses (and shows the email you must grant access to), the Android
    Publisher API is reachable, the Play Developer Reporting API is reachable and
    lists your apps, the "Reply to reviews" permission is present (the API returns
    an empty list rather than an error when it is missing), and the Cloud Storage
    reports bucket is configured and readable. Each failing step comes with the
    exact fix. Run this first whenever a Play tool returns empty or unexpected data.
    """
    sections: list[tuple[str, list[Check]]] = []
    try:
        sections.append(("Google Play", _google_play_checks()))
    except Exception as exc:  # noqa: BLE001
        sections.append(("Google Play", [_from_error("Google Play", exc)]))
    try:
        sections.append(("App Store Connect", _app_store_checks()))
    except Exception as exc:  # noqa: BLE001
        sections.append(("App Store Connect", [_from_error("App Store Connect", exc)]))

    lines: list[str] = ["StorePilot setup check", "=" * 60]
    counts: dict[str, int] = {}
    for title, checks in sections:
        lines.append("")
        lines.append(f"-- {title} " + "-" * max(0, 57 - len(title)))
        for check in checks:
            counts[check.status] = counts.get(check.status, 0) + 1
            lines.append(check.render())

    lines.append("")
    lines.append("=" * 60)
    summary = ", ".join(f"{counts[s]} {s}" for s in ("ok", "warn", "fail", "skip") if s in counts)
    lines.append(f"Summary: {summary or 'nothing checked'}")
    if counts.get("fail"):
        lines.append("Resolve the [fail] items above, then run setup_doctor again.")
    elif counts.get("warn"):
        lines.append("Usable, but the [warn] items limit which tools return data.")
    return "\n".join(lines)


def _register_adapters() -> None:
    if settings.google_play_enabled:
        from storepilot.google_play.tools_read import register as register_play_read

        register_play_read(mcp)

        from storepilot.google_play.tools_write import register as register_play_write

        register_play_write(mcp)
    if settings.app_store_enabled:
        from storepilot.app_store.tools import register as register_app_store

        register_app_store(mcp)
    if settings.google_play_enabled or settings.app_store_enabled:
        from storepilot.cross.tools import register as register_cross

        register_cross(mcp)


def main() -> None:
    _register_adapters()
    mcp.run()


if __name__ == "__main__":
    main()
