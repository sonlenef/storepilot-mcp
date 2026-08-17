"""Service account credentials -> Google API clients.

google-api-python-client is synchronous and is used as such; there is no async
transport for the Android Publisher discovery API, so wrapping it in fake async
would only add a layer that lies about concurrency.

Clients are built lazily and cached per scope set, because building a discovery
client is the expensive part and every tool call would otherwise repeat it.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storepilot.config import settings
from storepilot.core.errors import (
    DOCS_PLAY_API_ACCESS,
    DOCS_PLAY_GETTING_STARTED,
    DOCS_PLAY_PERMISSIONS,
    ApiNotEnabledError,
    CredentialsError,
    NotFoundError,
    RateLimitError,
    StorePermissionError,
    StorePilotError,
    UpstreamError,
    ValidationError,
    redact_path,
)

# Verified against the bundled discovery documents: Play Developer Reporting has
# its OWN scope, it is not covered by the androidpublisher scope.
ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
REPORTING_SCOPE = "https://www.googleapis.com/auth/playdeveloperreporting"
GCS_READ_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

ALL_SCOPES = (ANDROID_PUBLISHER_SCOPE, REPORTING_SCOPE, GCS_READ_SCOPE)

_lock = threading.Lock()
_cache: dict[str, Any] = {}


@dataclass(frozen=True)
class ServiceAccountInfo:
    """The parts of the key file that are safe to show a user.

    The client email is intentionally exposed: users must copy it into Play
    Console to grant access, and it is not a secret. The private key never is.
    """

    client_email: str
    project_id: str | None
    private_key_id_suffix: str
    path: str

    @property
    def enable_api_url(self) -> str:
        project = self.project_id or "_"
        return (
            "https://console.cloud.google.com/apis/library/androidpublisher.googleapis.com"
            f"?project={project}"
        )

    @property
    def enable_reporting_api_url(self) -> str:
        project = self.project_id or "_"
        return (
            "https://console.cloud.google.com/apis/library/playdeveloperreporting."
            f"googleapis.com?project={project}"
        )


def credentials_path() -> Path:
    """Resolve the configured key path, or raise with setup instructions."""
    raw = settings.google_credentials
    if raw is None:
        raise CredentialsError(
            "Google Play is not configured: STOREPILOT_GOOGLE_CREDENTIALS is unset.",
            remedy=(
                "Create a service account in Google Cloud, download its JSON key, and set "
                "STOREPILOT_GOOGLE_CREDENTIALS=/path/to/key.json (in your .env or the MCP "
                "server env block). Then run setup_doctor again."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
        )
    return Path(raw).expanduser()


def load_service_account_info(path: Path | None = None) -> ServiceAccountInfo:
    """Read and validate the service account JSON without building any client."""
    key_path = path or credentials_path()
    if not key_path.exists():
        raise CredentialsError(
            f"Service account key not found at {redact_path(key_path)}.",
            remedy=(
                "Check STOREPILOT_GOOGLE_CREDENTIALS points at an existing file. Download the "
                "key from Google Cloud Console -> IAM & Admin -> Service Accounts -> Keys -> "
                "Add key -> JSON."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
        )
    try:
        data = json.loads(key_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CredentialsError(
            f"Service account key at {redact_path(key_path)} is not valid JSON.",
            remedy=(
                "Re-download the JSON key from Google Cloud Console. A truncated download or "
                "a key pasted into an editor with smart quotes is the usual cause."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
            details={"json_error": str(exc)},
        ) from exc
    except OSError as exc:
        raise CredentialsError(
            f"Cannot read service account key at {redact_path(key_path)}.",
            remedy="Check file permissions on the key file (it must be readable by this process).",
            details={"os_error": str(exc)},
        ) from exc

    if data.get("type") != "service_account":
        raise CredentialsError(
            f"{redact_path(key_path)} is not a service account key "
            f"(type={data.get('type', 'missing')!r}).",
            remedy=(
                "OAuth client secrets and user credentials do not work here. Create a service "
                "account key: Google Cloud Console -> IAM & Admin -> Service Accounts -> Keys."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
        )
    missing = [f for f in ("client_email", "private_key") if not data.get(f)]
    if missing:
        raise CredentialsError(
            f"Service account key at {redact_path(key_path)} is missing: {', '.join(missing)}.",
            remedy="Re-download the JSON key; the file appears to have been edited or truncated.",
            doc_url=DOCS_PLAY_GETTING_STARTED,
        )

    key_id = str(data.get("private_key_id", ""))
    return ServiceAccountInfo(
        client_email=data["client_email"],
        project_id=data.get("project_id"),
        private_key_id_suffix=key_id[-6:] if key_id else "unknown",
        path=redact_path(key_path),
    )


def _credentials(scopes: tuple[str, ...]) -> Any:
    from google.auth.exceptions import GoogleAuthError
    from google.oauth2 import service_account

    key_path = credentials_path()
    load_service_account_info(key_path)
    try:
        return service_account.Credentials.from_service_account_file(
            str(key_path), scopes=list(scopes)
        )
    except (GoogleAuthError, ValueError) as exc:
        raise CredentialsError(
            f"Service account key at {redact_path(key_path)} was rejected by google-auth.",
            remedy=(
                "The private key in the file is malformed. Delete the key in Google Cloud "
                "Console and create a fresh JSON key."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
            details={"auth_error": str(exc)},
        ) from exc


def _cached(name: str, factory: Any) -> Any:
    with _lock:
        if name not in _cache:
            _cache[name] = factory()
        return _cache[name]


def _build(service: str, version: str, scopes: tuple[str, ...]) -> Any:
    from googleapiclient.discovery import build

    creds = _credentials(scopes)
    try:
        # static_discovery uses the discovery documents bundled with the client
        # library, so building a client never makes a network call.
        return build(
            service,
            version,
            credentials=creds,
            cache_discovery=False,
            static_discovery=True,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed error below
        raise classify_google_error(exc, context=f"building the {service} {version} client")


def publisher_client() -> Any:
    """Android Publisher v3 client (releases, listings, reviews, edits)."""
    return _cached("androidpublisher", lambda: _build("androidpublisher", "v3", (ANDROID_PUBLISHER_SCOPE,)))


def reporting_client() -> Any:
    """Play Developer Reporting v1beta1 client (Android Vitals, app search)."""
    return _cached(
        "playdeveloperreporting",
        lambda: _build("playdeveloperreporting", "v1beta1", (REPORTING_SCOPE,)),
    )


def storage_client() -> Any:
    """Read-only Cloud Storage client for the pubsite_prod_rev_* reports bucket."""

    def factory() -> Any:
        from google.cloud import storage

        creds = _credentials((GCS_READ_SCOPE,))
        return storage.Client(project=getattr(creds, "project_id", None), credentials=creds)

    return _cached("storage", factory)


def service_account_email() -> str:
    """The email users must grant access to, or "unknown" if unreadable."""
    try:
        return load_service_account_info().client_email
    except StorePilotError:
        return "unknown"


def reset_clients() -> None:
    """Drop cached credentials and clients. Call after config changes or in tests."""
    with _lock:
        _cache.clear()


# --- Vendor exception -> StorePilotError -------------------------------------

_SERVICE_DISABLED_MARKERS = (
    "accessnotconfigured",
    "service_disabled",
    "has not been used in project",
    "is disabled",
)


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _error_body(exc: Exception) -> str:
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    return str(exc)


def _retry_after(exc: Exception) -> float | None:
    resp = getattr(exc, "resp", None)
    raw = None
    if resp is not None:
        try:
            raw = resp.get("retry-after")
        except (AttributeError, TypeError):
            raw = getattr(resp, "retry_after", None)
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def classify_google_error(
    exc: Exception,
    *,
    context: str,
    package_name: str | None = None,
) -> StorePilotError:
    """Translate a Google client exception into an actionable StorePilot error.

    The critical distinction this makes is "API not enabled in the Cloud project"
    (403 SERVICE_DISABLED, fixed in Google Cloud) versus "service account not
    invited to Play Console" (401, fixed in Play Console). Users conflate these
    constantly and the raw Google message does not separate them.
    """
    if isinstance(exc, StorePilotError):
        return exc

    from google.auth.exceptions import RefreshError

    email = service_account_email()
    status = _http_status(exc)
    body = _error_body(exc)
    lowered = body.lower()
    target = f" for {package_name}" if package_name else ""

    if isinstance(exc, RefreshError):
        return CredentialsError(
            f"Could not obtain an access token while {context}.",
            remedy=(
                "The service account key was rejected. Confirm the key has not been deleted or "
                "disabled in Google Cloud Console -> IAM & Admin -> Service Accounts -> Keys, "
                "and that the machine clock is accurate (JWT signing is time sensitive)."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
            details={"service_account": email},
        )

    if status == 403 and any(m in lowered for m in _SERVICE_DISABLED_MARKERS):
        try:
            info = load_service_account_info()
            enable_url = info.enable_api_url
            project = info.project_id or "unknown"
        except StorePilotError:
            enable_url = "https://console.cloud.google.com/apis/library"
            project = "unknown"
        return ApiNotEnabledError(
            f"The API is not enabled in Google Cloud project {project!r} (while {context}).",
            remedy=(
                f"Enable it at {enable_url} — this is a Google Cloud setting, granting "
                f"permissions in Play Console will not fix it. Wait ~1 minute after enabling."
            ),
            doc_url=DOCS_PLAY_GETTING_STARTED,
            details={"service_account": email, "cloud_project": project},
        )

    if status == 401:
        return CredentialsError(
            f"Google rejected the credentials while {context}{target}.",
            remedy=(
                f"The service account is authenticated but Play Console does not know it. In "
                f"Play Console -> Users and permissions -> Invite new users, invite {email} and "
                f"grant it access to the app. Also confirm the Play Console account is linked to "
                f"the same Google Cloud project as this key."
            ),
            doc_url=DOCS_PLAY_PERMISSIONS,
            details={"service_account": email},
        )

    if status == 429 or (status == 403 and ("ratelimit" in lowered or "quota" in lowered)):
        return RateLimitError(
            f"Rate limited by Google while {context}.",
            remedy=(
                "Wait and retry. Android Publisher allows roughly 200,000 requests/day but a much "
                "lower burst rate; batch per-app calls rather than looping tightly."
            ),
            retry_after=_retry_after(exc),
        )

    if status == 403:
        return StorePermissionError(
            f"The service account lacks permission while {context}{target}.",
            remedy=(
                f"In Play Console -> Users and permissions, select {email} and grant the app "
                f"permissions this operation needs (View app information, View financial data, "
                f"or Reply to reviews). Permission changes can take a few minutes to apply."
            ),
            doc_url=DOCS_PLAY_PERMISSIONS,
            details={"service_account": email},
        )

    if status == 404:
        return NotFoundError(
            f"Google returned 404 while {context}{target}.",
            remedy=(
                "Check the package name is exact and that the app has had at least one build "
                "uploaded via the Play Console UI — the API cannot see apps that have never "
                "been published."
            ),
            details={"service_account": email},
        )

    if status in (400, 409, 412):
        # Without this a Play 400 fell through to UpstreamError, whose remedy is
        # "retry once" — advice that can never work for a malformed request and
        # that contradicts the App Store adapter, where 400/409 is a
        # ValidationError naming the offending field. Same status, same class of
        # problem, so the same answer.
        return ValidationError(
            f"Google rejected the request while {context}{target}: {body[:300]}",
            remedy=(
                "This is a malformed or not-currently-allowed request, not a transient "
                "failure — retrying sends the identical request. Common causes: a version "
                "code that was already used or is lower than the live one, a track name that "
                "does not exist on this app, a release with no version codes, or a listing "
                "field over its length limit. The message above names the field where Google "
                "identified one."
            ),
            doc_url=DOCS_PLAY_API_ACCESS,
            details={"service_account": email, "status": status},
        )

    if status is not None and status >= 500:
        return UpstreamError(
            f"Google returned {status} while {context}.",
            status=status,
        )

    return UpstreamError(
        f"Unexpected failure while {context}: {type(exc).__name__}: {exc}",
        remedy=(
            "Retry once. If it persists, run setup_doctor to confirm credentials, API enablement "
            "and Play Console permissions are all still in place."
        ),
        status=status,
    )
