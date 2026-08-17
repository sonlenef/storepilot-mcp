"""HTTP client for the App Store Connect API (JSON:API over httpx).

**Synchronous, deliberately.** The Google Play adapter is synchronous because
google-api-python-client has no async transport, and the cross-store tools call
both adapters from a single function body. Making this half of the product async
would force every shared tool to either run an event loop by hand or maintain two
code paths for the same query. The workload here is also latency-bound on a small
number of sequential, rate-limited calls — not a case where concurrency pays.
``httpx.Client`` is used rather than ``requests`` so the async variant remains a
drop-in later if the whole server moves.

What this module centralizes, so no endpoint module re-implements it:

* **Auth injection** — a fresh JWT per request from the shared token manager,
  with a single automatic retry after invalidating the token on a 401 (covers the
  narrow case of a token that expired between minting and arrival).
* **Pagination** — JSON:API returns ``links.next``; callers get every page up to
  an explicit cap and are *told* when they hit it rather than silently truncated.
* **``included`` resolution** — sideloaded resources are indexed by
  ``(type, id)`` once per response instead of being re-scanned per relationship.
* **Rate limiting** — Apple allows ~3600 requests/hour per key and reports the
  remaining budget in the ``x-rate-limit`` header. There is *also* an
  undocumented per-minute ceiling that starts refusing somewhere around 300-350
  requests in a clock minute. Both are enforced pre-emptively here: a portfolio
  scan across many apps will otherwise walk straight into a 429 halfway through
  and leave the caller with partial data.
* **Error classification** — Apple's ``errors[]`` payload becomes a StorePilot
  error carrying the fix, never a raw status code.
"""

from __future__ import annotations

import email.utils
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from storepilot.app_store import auth
from storepilot.app_store.auth import ASC_BASE_URL, AscCredentials, TokenManager
from storepilot.core.errors import (
    DOCS_ASC_KEYS,
    NotFoundError,
    RateLimitError,
    StorePermissionError,
    StorePilotError,
    UpstreamError,
    ValidationError,
)

DOCS_ASC_API = "https://developer.apple.com/documentation/appstoreconnectapi"
DOCS_ASC_ROLES = "https://developer.apple.com/help/app-store-connect/reference/role-permissions"

#: Documented hourly budget per API key.
HOURLY_LIMIT = 3600

#: Undocumented per-minute ceiling. Teams report 429s from roughly 300-350
#: requests inside one clock minute; 240 keeps a wide margin while still
#: allowing a fast portfolio sweep.
MINUTE_SOFT_CAP = 240

#: When fewer than this many hourly requests remain, start spacing calls out so
#: a long-running session degrades gracefully instead of hitting a wall.
HOUR_LOW_WATER = 300

#: Below this, the budget is nearly gone: space calls by a full second so an
#: interactive session keeps working rather than dying on a 429.
HOUR_CRITICAL = 60

#: Default page size. Apple's maximum is 200 for most collections; a large page
#: costs the same one request as a small one, so use it.
DEFAULT_PAGE_SIZE = 200

#: Refuse to walk more than this many pages in a single call unless told
#: otherwise — protects the hourly budget from an unbounded sweep.
DEFAULT_MAX_PAGES = 20

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3


# --- Rate limiting ----------------------------------------------------------


@dataclass
class RateLimitSnapshot:
    """Parsed ``x-rate-limit`` header, e.g. ``user-hour-lim:3600;user-hour-rem:3121;``."""

    hour_limit: int | None = None
    hour_remaining: int | None = None
    observed_at: float = 0.0
    raw: str | None = None

    @property
    def is_low(self) -> bool:
        return self.hour_remaining is not None and self.hour_remaining < HOUR_LOW_WATER

    @property
    def is_critical(self) -> bool:
        return self.hour_remaining is not None and self.hour_remaining < HOUR_CRITICAL

    def describe(self) -> str:
        if self.hour_remaining is None:
            return "rate limit: unknown (Apple sent no x-rate-limit header yet)"
        limit = self.hour_limit or HOURLY_LIMIT
        pct = 100.0 * self.hour_remaining / limit if limit else 0.0
        note = ""
        if self.is_critical:
            note = " — CRITICAL, requests are being spaced out by a second each"
        elif self.is_low:
            note = " — low, requests are being throttled"
        return f"rate limit: {self.hour_remaining}/{limit} requests left this hour ({pct:.0f}%){note}"


def parse_rate_limit(header: str | None, *, observed_at: float = 0.0) -> RateLimitSnapshot:
    """Parse Apple's semicolon-delimited rate-limit header.

    Tolerant by design: the header is undocumented enough that Apple could add
    or rename fields, and an unparseable header must never break a request that
    otherwise succeeded.
    """
    snapshot = RateLimitSnapshot(observed_at=observed_at, raw=header)
    if not header:
        return snapshot
    for part in header.split(";"):
        if ":" not in part:
            continue
        name, _, value = part.partition(":")
        try:
            number = int(value.strip())
        except ValueError:
            continue
        key = name.strip().lower()
        if key.endswith("hour-lim"):
            snapshot.hour_limit = number
        elif key.endswith("hour-rem"):
            snapshot.hour_remaining = number
    return snapshot


class Throttle:
    """Pre-emptive pacing against both of Apple's ceilings.

    The per-minute window is a sliding deque of request timestamps rather than a
    fixed bucket: Apple's undocumented limit behaves like a sliding window, and a
    fixed bucket would let a burst straddle the boundary and still trip it.
    """

    def __init__(
        self,
        *,
        minute_cap: int = MINUTE_SOFT_CAP,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.minute_cap = minute_cap
        self._sleep = sleep
        self._monotonic = monotonic
        self._recent: deque[float] = deque()
        self._snapshot = RateLimitSnapshot()
        self._lock = threading.Lock()
        #: Total seconds spent self-throttling. Surfaced in diagnostics so a slow
        #: tool call can be explained rather than looking like a hang.
        self.slept_seconds = 0.0

    @property
    def snapshot(self) -> RateLimitSnapshot:
        return self._snapshot

    def observe(self, snapshot: RateLimitSnapshot) -> None:
        if snapshot.hour_remaining is not None or snapshot.raw:
            self._snapshot = snapshot

    def hourly_delay(self) -> float:
        """Spacing to apply based on how much of the hourly budget is left."""
        if self._snapshot.is_critical:
            return 1.0
        if self._snapshot.is_low:
            return 0.25
        return 0.0

    def acquire(self) -> float:
        """Block until it is safe to send another request. Returns seconds slept."""
        with self._lock:
            slept = 0.0
            now = self._monotonic()
            self._evict(now)

            if len(self._recent) >= self.minute_cap:
                # Wait for the oldest request to age out of the 60s window.
                wait = max(0.0, 60.0 - (now - self._recent[0])) + 0.05
                self._sleep(wait)
                slept += wait
                now = self._monotonic()
                self._evict(now)

            delay = self.hourly_delay()
            if delay > 0:
                self._sleep(delay)
                slept += delay
                now = self._monotonic()

            self._recent.append(now)
            self.slept_seconds += slept
            return slept

    def _evict(self, now: float) -> None:
        while self._recent and now - self._recent[0] >= 60.0:
            self._recent.popleft()

    def recent_minute_count(self) -> int:
        with self._lock:
            self._evict(self._monotonic())
            return len(self._recent)


# --- JSON:API result shapes -------------------------------------------------

ResourceKey = tuple[str, str]


@dataclass
class Page:
    """One JSON:API response, with ``included`` already indexed."""

    data: list[dict[str, Any]] = field(default_factory=list)
    included: dict[ResourceKey, dict[str, Any]] = field(default_factory=dict)
    next_url: str | None = None
    total: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PagedResult:
    """Every page a call walked, flattened, plus whether it stopped early."""

    data: list[dict[str, Any]] = field(default_factory=list)
    included: dict[ResourceKey, dict[str, Any]] = field(default_factory=dict)
    pages: int = 0
    #: True when the page cap or the caller's ``limit`` cut the walk short —
    #: the caller must say so rather than presenting a partial list as complete.
    truncated: bool = False
    #: Apple's ``meta.paging.total`` when present.
    total: int | None = None

    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.data)

    def related(self, resource: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
        """Resolve a relationship on ``resource`` against the sideloaded ``included`` set."""
        return resolve_related(resource, name, self.included)

    def related_one(self, resource: Mapping[str, Any], name: str) -> dict[str, Any] | None:
        items = self.related(resource, name)
        return items[0] if items else None

    def truncation_note(self, *, what: str) -> str | None:
        if not self.truncated:
            return None
        total = f" of about {self.total}" if self.total else ""
        return (
            f"Showing the first {len(self.data)}{total} {what}; more exist. "
            f"Narrow the filters or raise the limit to see the rest."
        )


def index_included(payload: Mapping[str, Any]) -> dict[ResourceKey, dict[str, Any]]:
    """Index a response's ``included`` array by ``(type, id)`` for O(1) lookup."""
    index: dict[ResourceKey, dict[str, Any]] = {}
    for item in payload.get("included") or []:
        if isinstance(item, dict) and item.get("type") and item.get("id"):
            index[(item["type"], item["id"])] = item
    return index


def resolve_related(
    resource: Mapping[str, Any],
    name: str,
    included: Mapping[ResourceKey, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the sideloaded resources a relationship points at.

    Handles both relationship shapes — a single ``data`` object and a ``data``
    array — and silently skips pointers whose target was not sideloaded (which
    happens whenever the caller forgot the matching ``include=`` parameter).
    """
    relationships = resource.get("relationships") or {}
    rel = relationships.get(name) or {}
    data = rel.get("data")
    if data is None:
        return []
    pointers = data if isinstance(data, list) else [data]
    out: list[dict[str, Any]] = []
    for pointer in pointers:
        if not isinstance(pointer, dict):
            continue
        found = included.get((pointer.get("type", ""), pointer.get("id", "")))
        if found is not None:
            out.append(found)
    return out


def attrs(resource: Mapping[str, Any] | None) -> dict[str, Any]:
    """Attributes of a JSON:API resource, or an empty dict when absent."""
    if not resource:
        return {}
    value = resource.get("attributes")
    return value if isinstance(value, dict) else {}


# --- Error classification ---------------------------------------------------


def _apple_errors(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            return [e for e in errors if isinstance(e, dict)]
    return []


def _error_detail(errors: Sequence[Mapping[str, Any]]) -> str:
    """Flatten Apple's error array into one readable line.

    Apple splits the useful information across ``title`` (generic) and ``detail``
    (specific), and puts the offending field in ``source.pointer`` or
    ``source.parameter``. All three are needed to act on a 400.
    """
    parts: list[str] = []
    for err in errors:
        chunk = str(err.get("detail") or err.get("title") or err.get("code") or "").strip()
        source = err.get("source") or {}
        if isinstance(source, dict):
            where = source.get("pointer") or source.get("parameter")
            if where:
                chunk = f"{chunk} (at {where})" if chunk else f"at {where}"
        code = err.get("code")
        if code and str(code) not in chunk:
            chunk = f"{chunk} [{code}]" if chunk else f"[{code}]"
        if chunk:
            parts.append(chunk)
    return "; ".join(parts)


def _error_codes(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(e["code"]) for e in errors if e.get("code")]


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse ``Retry-After`` in either form: delta-seconds or an HTTP date."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    current = now if now is not None else time.time()
    return max(0.0, when.timestamp() - current)


def classify_asc_error(
    status: int,
    payload: Any,
    *,
    context: str,
    retry_after: float | None = None,
    rate_limit: RateLimitSnapshot | None = None,
) -> StorePilotError:
    """Map an App Store Connect HTTP failure onto the shared error taxonomy.

    The distinctions that matter to a user, and that a bare status code loses:
    a 401 is *always* a key/clock problem and never worth retrying; a 403 means
    the key's App Store Connect **role** is too weak (fixed by an Account Holder,
    not by us); a 409 is Apple's catch-all for "the resource is not in a state
    that allows this", which is a validation problem, not a conflict to retry.
    """
    errors = _apple_errors(payload)
    detail = _error_detail(errors)
    codes = _error_codes(errors)
    details: dict[str, Any] = {}
    if detail:
        details["apple_detail"] = detail
    if codes:
        details["apple_codes"] = codes
    if rate_limit and rate_limit.hour_remaining is not None:
        details["hourly_requests_remaining"] = rate_limit.hour_remaining

    if status == 401:
        return auth.rejected_token_error(detail, context=context)

    if status == 403:
        return StorePermissionError(
            f"App Store Connect refused the request while {context}"
            + (f": {detail}" if detail else "."),
            remedy=(
                "The key authenticated but its role is not allowed to do this. In App Store "
                "Connect -> Users and Access -> Integrations, check the key's role: reading "
                "apps, builds and reviews needs at least App Manager or Developer; sales and "
                "finance reports need Admin, Finance, or Sales; replying to reviews and "
                "submitting for review need App Manager or Admin. Only an Account Holder or "
                "Admin can change a key's role, and a new role applies to newly minted tokens "
                "within a few minutes."
            ),
            doc_url=DOCS_ASC_ROLES,
            details=details or None,
        )

    if status == 404:
        return NotFoundError(
            f"App Store Connect returned 404 while {context}"
            + (f": {detail}" if detail else "."),
            remedy=(
                "Check the identifier. App Store Connect uses the numeric Apple ID (e.g. "
                "1234567890) for /v1/apps/{id}, not the bundle id — pass the bundle id through "
                "filter[bundleId] instead. Also confirm the app is visible to this key's team; "
                "apps in another team are indistinguishable from apps that do not exist."
            ),
            doc_url=DOCS_ASC_API,
            details=details or None,
        )

    if status == 429:
        return RateLimitError(
            f"Rate limited by App Store Connect while {context}"
            + (f": {detail}" if detail else "."),
            remedy=(
                "Apple allows about 3,600 requests/hour per API key, plus an undocumented "
                "per-minute ceiling around 300. StorePilot already paces itself against both, "
                "so hitting this means another tool is sharing the same key. Wait for the "
                "window to reset, or issue a separate API key for this server. Sales reports "
                "are limited far more strictly still — read them from cache rather than "
                "re-fetching."
            ),
            retry_after=retry_after,
            details=details or None,
        )

    if status in (400, 409, 422):
        return ValidationError(
            f"App Store Connect rejected the request while {context}"
            + (f": {detail}" if detail else f" (HTTP {status})."),
            remedy=(
                "Apple returns 409 both for genuinely invalid input and for 'the resource is "
                "not in a state that allows this' — for example editing a version that is "
                "already Waiting for Review, or submitting one with no build attached. The "
                "detail above names the offending field; fix it and retry."
            ),
            doc_url=DOCS_ASC_API,
            details=details or None,
        )

    if status >= 500:
        return UpstreamError(
            f"App Store Connect returned {status} while {context}"
            + (f": {detail}" if detail else "."),
            status=status,
            remedy=(
                "This is Apple's side. Retry in a few seconds; if it persists check "
                "https://developer.apple.com/system-status/ — App Store Connect API outages "
                "are listed there and no configuration change will help until it clears."
            ),
            details=details or None,
        )

    return UpstreamError(
        f"Unexpected HTTP {status} from App Store Connect while {context}"
        + (f": {detail}" if detail else "."),
        status=status,
        details=details or None,
    )


# --- Outbound URL policy -----------------------------------------------------

#: Hosts this client will send an App Store Connect Bearer token to. Apple's own
#: pagination links only ever point at the API host; anything else means the
#: response was not what we think it was.
_APPLE_API_HOSTS = ("api.appstoreconnect.apple.com", "api.enterprise.developer.apple.com")


def require_remote_url(url: str, *, what: str, require_apple: bool = False) -> None:
    """Reject a URL that is not a plain https fetch of a remote host.

    Two URLs in this client are taken from a response body rather than composed
    locally: ``links.next`` (which is sent WITH the Bearer JWT) and the analytics
    segment URL (which is not). Both come from Apple today, so neither is a live
    SSRF — but "the data told us where to go" is exactly the shape that becomes
    one the moment a caller passes a URL through, and the check costs nothing.

    Blocks non-https schemes outright, which also rules out ``file://`` reading a
    local file into a report, and blocks the loopback and link-local addresses
    that make an SSRF interesting (169.254.169.254 is a cloud metadata service).
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValidationError(
            f"Refusing to fetch {what} over {parsed.scheme or 'no'} scheme.",
            remedy=(
                "StorePilot only fetches https URLs. A non-https URL here means the response "
                "that supplied it was not from Apple; do not retry, and report it."
            ),
            details={"url": url.split("?", 1)[0]},
        )
    host = (parsed.hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"} or host.startswith("169.254."):
        raise ValidationError(
            f"Refusing to fetch {what} from {host or 'an empty host'}.",
            remedy=(
                "Loopback and link-local addresses are never valid store endpoints. "
                "169.254.169.254 in particular is a cloud metadata service."
            ),
            details={"url": url.split("?", 1)[0]},
        )
    if require_apple and host not in _APPLE_API_HOSTS:
        raise ValidationError(
            f"Refusing to send App Store Connect credentials to {host} while following {what}.",
            remedy=(
                "Pagination links must stay on Apple's API host. A link pointing elsewhere "
                "means the response was tampered with; do not retry."
            ),
            details={"host": host, "expected": list(_APPLE_API_HOSTS)},
        )


# --- Client -----------------------------------------------------------------


class AscClient:
    """Synchronous App Store Connect API client.

    Construct with ``transport=`` to inject an ``httpx.MockTransport`` in tests;
    every other seam (sleep, clock, retry count) is injectable for the same
    reason — nothing here should require a real Apple account to exercise.
    """

    def __init__(
        self,
        *,
        credentials: AscCredentials | None = None,
        token_manager: TokenManager | None = None,
        base_url: str = ASC_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        throttle: Throttle | None = None,
    ) -> None:
        if token_manager is not None:
            self._tokens = token_manager
        elif credentials is not None:
            self._tokens = TokenManager(credentials)
        else:
            # Deferred: raises CredentialsError at first use, not at import time,
            # so a server with only Google Play configured still starts.
            self._tokens = None  # type: ignore[assignment]
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._sleep = sleep
        self._monotonic = monotonic
        self.throttle = throttle or Throttle(sleep=sleep, monotonic=monotonic)
        self._client = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": "StorePilot/0.1 (+https://github.com/storepilot)"},
            follow_redirects=True,
        )

    # -- lifecycle --

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def tokens(self) -> TokenManager:
        if self._tokens is None:
            self._tokens = auth.token_manager()
        return self._tokens

    @property
    def rate_limit(self) -> RateLimitSnapshot:
        return self.throttle.snapshot

    # -- core request --

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        accept: str = "application/json",
        context: str | None = None,
        absolute: bool = False,
    ) -> httpx.Response:
        """Send one request with auth, throttling, retries and error classification.

        Returns the raw response so callers can choose JSON or bytes. Any
        non-2xx status has already been converted into a raised StorePilotError.
        """
        label = context or f"{method} {path}"
        if absolute:
            # `path` is Apple's own links.next, not something a caller composed —
            # but this request carries the Bearer JWT, so the destination is
            # checked before the credential is attached rather than trusted
            # because of where it came from.
            require_remote_url(path, what="a pagination link", require_apple=True)
        url = path if absolute else self._join(path)
        attempt = 0

        while True:
            self.throttle.acquire()
            headers = {"Accept": accept, **self.tokens.auth_header()}
            if json_body is not None:
                headers["Content-Type"] = "application/json"

            try:
                response = self._client.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body is not None else None,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                raise UpstreamError(
                    f"App Store Connect timed out while {label} "
                    f"(after {self.max_retries + 1} attempts).",
                    remedy=(
                        "Check network connectivity and any corporate proxy. If only sales or "
                        "analytics report calls time out, they are genuinely slow — those "
                        "endpoints build a file server-side."
                    ),
                    details={"timeout": str(exc)},
                ) from exc
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"Could not reach App Store Connect while {label}: {type(exc).__name__}.",
                    remedy=(
                        "Confirm this machine can reach https://api.appstoreconnect.apple.com "
                        "(a proxy or firewall that blocks it produces exactly this)."
                    ),
                    details={"transport_error": str(exc)},
                ) from exc

            self.throttle.observe(
                parse_rate_limit(
                    response.headers.get("x-rate-limit"), observed_at=self._monotonic()
                )
            )

            if response.is_success:
                return response

            retry_after = parse_retry_after(response.headers.get("retry-after"))

            # A 401 can mean the token expired in flight. Re-mint once; a second
            # 401 is a real credential problem and must not be retried further.
            if response.status_code == 401 and attempt == 0:
                attempt += 1
                self.tokens.invalidate()
                continue

            if response.status_code == 429 and attempt < self.max_retries:
                attempt += 1
                self._sleep(retry_after if retry_after is not None else self._backoff_delay(attempt))
                continue

            if response.status_code >= 500 and attempt < self.max_retries:
                attempt += 1
                self._backoff(attempt)
                continue

            raise classify_asc_error(
                response.status_code,
                self._safe_json(response),
                context=label,
                retry_after=retry_after,
                rate_limit=self.rate_limit,
            )

    def _join(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return path if path.startswith("/") else f"/{path}"

    def _backoff_delay(self, attempt: int) -> float:
        # Exponential with jitter: without jitter, several tools retrying after
        # the same 429 would resynchronise and trip the limit again together.
        return min(8.0, 0.5 * (2 ** (attempt - 1))) * (0.75 + random.random() * 0.5)

    def _backoff(self, attempt: int) -> None:
        self._sleep(self._backoff_delay(attempt))

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"errors": [{"detail": response.text[:500]}]} if response.text else {}

    # -- typed helpers --

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        response = self.request("GET", path, params=params, context=context)
        payload = self._safe_json(response)
        return payload if isinstance(payload, dict) else {}

    def get_page(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        context: str | None = None,
        absolute: bool = False,
    ) -> Page:
        response = self.request("GET", path, params=params, context=context, absolute=absolute)
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            raise UpstreamError(
                f"App Store Connect returned a non-object body for {path}.",
                remedy="Retry; if it persists the endpoint shape changed and needs a fix here.",
            )
        data = payload.get("data")
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        paging = ((payload.get("meta") or {}).get("paging") or {}) if payload.get("meta") else {}
        return Page(
            data=[i for i in items if isinstance(i, dict)],
            included=index_included(payload),
            next_url=((payload.get("links") or {}).get("next")),
            total=paging.get("total") if isinstance(paging, dict) else None,
            raw=payload,
        )

    def get_all(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        context: str | None = None,
    ) -> PagedResult:
        """Walk ``links.next`` until the collection ends or a cap is reached.

        Two caps, both intentional: ``limit`` is the caller's "I only need N",
        and ``max_pages`` is the safety net that stops one tool call from
        spending the whole hourly budget on an app with 50,000 reviews. Hitting
        either sets ``truncated`` so the tool can say so out loud.
        """
        query = dict(params or {})
        if limit is not None:
            page_size = min(page_size, max(1, limit))
        query.setdefault("limit", page_size)

        result = PagedResult()
        url: str | None = path
        absolute = False

        while url is not None and result.pages < max_pages:
            page = self.get_page(
                url,
                params=None if absolute else query,
                context=context,
                absolute=absolute,
            )
            result.pages += 1
            result.data.extend(page.data)
            result.included.update(page.included)
            if page.total is not None:
                result.total = page.total

            if limit is not None and len(result.data) >= limit:
                result.data = result.data[:limit]
                result.truncated = page.next_url is not None or (
                    result.total is not None and result.total > len(result.data)
                )
                return result

            url = page.next_url
            # links.next is a fully-formed URL carrying the cursor and every
            # original filter; re-sending our params would duplicate them.
            absolute = url is not None

        if url is not None:
            result.truncated = True
        return result

    def get_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/a-gzip",
        context: str | None = None,
    ) -> bytes:
        """Fetch a binary body — the sales/finance report endpoints return gzip TSV."""
        response = self.request("GET", path, params=params, accept=accept, context=context)
        return response.content

    def download(self, url: str, *, context: str | None = None) -> bytes:
        """Fetch a pre-signed analytics segment URL.

        Deliberately unauthenticated: Apple hands out signed CDN URLs for report
        segments, and attaching a Bearer token to them is either ignored or
        rejected. The throttle still applies — the download counts against
        nothing, but pacing keeps a many-segment report from saturating the link.
        """
        require_remote_url(url, what="an analytics segment URL")
        label = context or f"downloading report segment from {url.split('?', 1)[0]}"
        self.throttle.acquire()
        try:
            response = self._client.get(url, headers={"Accept": "*/*"})
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Could not {label}: {type(exc).__name__}.",
                remedy="Retry — segment URLs are time-limited and may need to be re-listed.",
                details={"transport_error": str(exc)},
            ) from exc
        if not response.is_success:
            raise classify_asc_error(
                response.status_code,
                self._safe_json(response),
                context=label,
                rate_limit=self.rate_limit,
            )
        return response.content

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        context: str | None = None,
    ) -> dict[str, Any]:
        response = self.request("POST", path, json_body=body, context=context)
        payload = self._safe_json(response)
        return payload if isinstance(payload, dict) else {}

    def patch(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        context: str | None = None,
    ) -> dict[str, Any]:
        response = self.request("PATCH", path, json_body=body, context=context)
        payload = self._safe_json(response)
        return payload if isinstance(payload, dict) else {}

    def delete(self, path: str, *, context: str | None = None) -> None:
        self.request("DELETE", path, context=context)


# --- Request body helpers ---------------------------------------------------


def resource_body(
    resource_type: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    relationships: Mapping[str, str | tuple[str, str]] | None = None,
    resource_id: str | None = None,
) -> dict[str, Any]:
    """Build a JSON:API request document.

    ``relationships`` maps a name to either ``(type, id)`` or just an id when the
    relationship name already equals the resource type — which it does for most
    of App Store Connect (``app``/``apps``, ``build``/``builds``, ...).
    """
    data: dict[str, Any] = {"type": resource_type}
    if resource_id is not None:
        data["id"] = resource_id
    if attributes:
        data["attributes"] = dict(attributes)
    if relationships:
        rels: dict[str, Any] = {}
        for name, target in relationships.items():
            if isinstance(target, tuple):
                rel_type, rel_id = target
            else:
                rel_type, rel_id = f"{name}s", target
            rels[name] = {"data": {"type": rel_type, "id": rel_id}}
        data["relationships"] = rels
    return {"data": data}


# --- Shared client instance -------------------------------------------------

_client_lock = threading.Lock()
_shared: AscClient | None = None


def shared_client() -> AscClient:
    """The process-wide client, built on first use.

    Shared so the throttle sees every request: a per-call client would each keep
    their own view of the minute window and collectively blow past Apple's cap.
    """
    global _shared
    with _client_lock:
        if _shared is None:
            _shared = AscClient()
        return _shared


def reset_client() -> None:
    """Close and drop the shared client. Call after config changes or in tests."""
    global _shared
    with _client_lock:
        if _shared is not None:
            _shared.close()
        _shared = None
    auth.reset_auth()


__all__ = [
    "DOCS_ASC_API",
    "DOCS_ASC_KEYS",
    "AscClient",
    "Page",
    "PagedResult",
    "RateLimitSnapshot",
    "Throttle",
    "attrs",
    "classify_asc_error",
    "index_included",
    "parse_rate_limit",
    "parse_retry_after",
    "reset_client",
    "resolve_related",
    "resource_body",
    "shared_client",
]
