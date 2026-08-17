"""LLM-facing error taxonomy.

Every adapter raises these instead of leaking vendor exceptions. The contract is
that an error must always answer "what do I do next?" — in this domain the #1
failure mode is silent misconfiguration, so a bare message is not enough.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

DOCS_PLAY_GETTING_STARTED = "https://developers.google.com/android-publisher/getting_started"
DOCS_PLAY_API_ACCESS = "https://developers.google.com/android-publisher/api-ref/rest"
DOCS_PLAY_REPORTING = "https://developers.google.com/play/developer/reporting"
DOCS_PLAY_PERMISSIONS = "https://support.google.com/googleplay/android-developer/answer/9844686"
DOCS_PLAY_DOWNLOAD_REPORTS = "https://support.google.com/googleplay/android-developer/answer/6135870"
DOCS_ASC_KEYS = "https://developer.apple.com/documentation/appstoreconnectapi/creating-api-keys-for-app-store-connect-api"


def redact_path(path: str | Path | None) -> str:
    """Render a filesystem path without exposing the full user directory tree.

    Credential paths often sit under a home directory that identifies the user;
    only the last two segments carry diagnostic value.
    """
    if path is None:
        return "<not set>"
    p = Path(path)
    parts = p.parts
    if len(parts) <= 2:
        return str(p)
    return str(Path(".../") / parts[-2] / parts[-1])


class StorePilotError(Exception):
    """Base error. Carries a concrete remedy so the caller is never stranded."""

    kind: ClassVar[str] = "error"

    def __init__(
        self,
        message: str,
        *,
        remedy: str,
        doc_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy
        self.doc_url = doc_url
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "error": self.kind,
            "message": self.message,
            "remedy": self.remedy,
        }
        if self.doc_url:
            data["doc_url"] = self.doc_url
        if self.details:
            data["details"] = self.details
        return data

    def to_llm_string(self) -> str:
        return render_error(self)

    def __str__(self) -> str:
        return self.message


class CredentialsError(StorePilotError):
    """Credentials are missing, unreadable, malformed, or rejected by the store."""

    kind = "credentials_error"


class ApiNotEnabledError(StorePilotError):
    """The API responded, but it is not enabled in the caller's Cloud project.

    Distinct from a permission error: nothing granted inside Play Console will
    fix it, the API has to be switched on in Google Cloud.
    """

    kind = "api_not_enabled"


class StorePermissionError(StorePilotError):
    """Authenticated, but the account lacks the permission for this operation.

    Named to avoid shadowing the builtin ``PermissionError``.
    """

    kind = "permission_error"


class NotFoundError(StorePilotError):
    """The requested app, release, report, or object does not exist."""

    kind = "not_found"


class RateLimitError(StorePilotError):
    """Throttled upstream. ``retry_after`` is in seconds when the API supplied it."""

    kind = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        remedy: str,
        retry_after: float | None = None,
        doc_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remedy=remedy, doc_url=doc_url, details=details)
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.retry_after is not None:
            data["retry_after_seconds"] = self.retry_after
        return data


class UpstreamError(StorePilotError):
    """The store API failed for a reason outside our control (5xx, network, timeout)."""

    kind = "upstream_error"

    def __init__(
        self,
        message: str,
        *,
        remedy: str = "Retry in a few seconds. If it persists the store API is degraded — "
        "check https://status.cloud.google.com or https://developer.apple.com/system-status/",
        status: int | None = None,
        doc_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remedy=remedy, doc_url=doc_url, details=details)
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.status is not None:
            data["status"] = self.status
        return data


class ValidationError(StorePilotError):
    """Input, or upstream payload shape, failed validation.

    Also raised when a report CSV is missing an expected column — Google changes
    those without notice, so the available headers are attached to ``details``.
    """

    kind = "validation_error"


def render_error(exc: BaseException) -> str:
    """Render any exception into a block an LLM can act on.

    Always safe to call: non-StorePilot exceptions degrade to type + message so
    a tool can return this instead of crashing the server.
    """
    if isinstance(exc, StorePilotError):
        lines = [f"[{exc.kind}] {exc.message}", f"Fix: {exc.remedy}"]
        if exc.doc_url:
            lines.append(f"Docs: {exc.doc_url}")
        for key, value in exc.details.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)
    return f"[unexpected_error] {type(exc).__name__}: {exc}"


def missing_column_error(
    wanted: list[str] | tuple[str, ...],
    available: list[str],
    *,
    source: str,
) -> ValidationError:
    """Build the error for a report column that Google renamed or removed."""
    wanted_str = " / ".join(wanted)
    return ValidationError(
        f"{source}: no column matching {wanted_str!r}. Google changes report columns "
        f"without notice (earnings columns changed in July 2026).",
        remedy=(
            "Inspect the available headers listed below and map the metric to the new "
            "column name, or open an issue with this header list attached."
        ),
        doc_url=DOCS_PLAY_DOWNLOAD_REPORTS,
        details={"available_headers": available, "expected_one_of": list(wanted)},
    )


# --- Silent-empty-result hints ---------------------------------------------
#
# Google Play's reviews.list returns HTTP 200 with an EMPTY list when the service
# account lacks the "Reply to reviews" permission. There is no error to catch, so
# every adapter must translate "empty" into an actionable hint rather than
# reporting a confidently wrong "this app has no reviews".

PLAY_REVIEWS_EMPTY_HINT = (
    "Google Play returned zero reviews. This is ambiguous: the API returns an empty list "
    "(not an error) when the service account lacks the 'Reply to reviews' permission. "
    "Verify in Play Console -> Users and permissions -> <service account email> -> App "
    "permissions -> enable 'Reply to reviews'. Note also that the API only ever returns "
    "production-track reviews that carry a comment, from roughly the last 7 days — so an "
    "empty list can also be legitimate for a low-traffic app."
)


def play_reviews_empty_hint(package_name: str, *, service_account_email: str | None = None) -> str:
    """Actionable explanation for an empty ``reviews.list`` response."""
    hint = f"No reviews returned for {package_name}. {PLAY_REVIEWS_EMPTY_HINT}"
    if service_account_email:
        hint += f" Service account to grant: {service_account_email}"
    return hint


def empty_result_hint(
    resource: str,
    *,
    reason: str,
    remedy: str,
) -> str:
    """Generic 'the answer is empty and that might be a misconfiguration' note."""
    return f"No {resource} returned. Possible cause: {reason}. Check: {remedy}"
