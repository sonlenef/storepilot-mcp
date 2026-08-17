"""Cross-store MCP tools — the reason StorePilot exists.

Every other store MCP server wraps one store's API. None of them can answer "how
is my portfolio doing across both stores", because answering it needs three
things no single-store adapter has: a pairing between two unrelated identifier
systems, a normalization of two incompatible data models, and a failure policy
that survives one store being completely unavailable.

The failure policy is the part that decides whether these tools are impressive or
embarrassing, so it is stated up front and enforced everywhere below:

* **Degrade per cell, per app, per store.** A missing permission on one app, or
  an entirely unconfigured store, shrinks the table — it never empties it. Every
  blank carries a short reason code and the legend explains it.
* **Never present a missing measurement as a healthy one.** Apple publishes no
  crash rate through its API, so the App Store rows say ``no-vitals``, not "ok".
  An app whose vitals Google suppressed reads ``unmeasured``, not "ok".
* **Never sum across currencies.** Money is grouped and labelled per currency,
  per store. There is no exchange rate in this codebase and inventing one would
  be a wrong answer that looks right.
* **Surface freshness before the numbers.** Play stats land 3-7 days late,
  earnings around the 5th of the following month, Apple sales a day late. A
  confident "0 installs" for a period that has not published yet is the single
  most damaging output this server can produce.
* **Spend quota like it is scarce, because Apple's is.** The whole-portfolio
  revenue picture costs exactly ONE ``/v1/salesReports`` request (account-wide,
  monthly, cached forever once the month closes) rather than one per app, and the
  Play Reporting fan-out rides the adapter's 8 QPS throttle.

Structure: data collection is behind two small gateway objects, so the rendering
and degradation logic can be exercised end-to-end against fakes without a single
network call. The tool functions themselves are module-level and take the
gateways as keyword arguments; :func:`register` only wires them to MCP.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from storepilot.config import settings
from storepilot.core import metadata_mirror as mirror
from storepilot.core.errors import (
    NotFoundError,
    StorePilotError,
    ValidationError,
    play_reviews_empty_hint,
    render_error,
)
from storepilot.core.guards import (
    PRODUCTION_POLICY,
    UNTRUSTED_CONTENT_NOTE,
    Change,
    Operation,
    Preview,
    append_warning,
    audit,
    audit_execution,
    require_confirmation,
    resolve_track,
    target_for,
    untrusted,
)
from storepilot.core.models import (
    Freshness,
    ListingText,
    PortfolioEntry,
    Release,
    ReleaseStatus,
    Report,
    Review,
    Store,
)
from storepilot.cross import apps as registry_module
from storepilot.cross.apps import AppPair, Registry, StoreApp

__all__ = [
    "AppleGateway",
    "PlayGateway",
    "collect_portfolio",
    "compare_reviews",
    "list_app_pairs",
    "metadata_pull",
    "metadata_push",
    "pair_apps",
    "parity_check",
    "portfolio_overview",
    "register",
    "release_both",
    "release_both_operation",
    "render_portfolio",
    "suggest_app_pairs",
]

# --- Tool annotations --------------------------------------------------------
#
# Same risk semantics as both adapters: anything that publishes user-visible text
# is destructive. A client that auto-approves a cross-store write because it was
# labelled differently from the equivalent single-store write would be worse than
# either policy applied consistently.

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

#: Writes only to the local filesystem (the registry, the metadata mirror).
#: Not read-only, but nothing reaches a store or a user.
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

#: Publishes to a store. Two-step confirmation, always.
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)

_PLAY = Store.GOOGLE_PLAY.value
_APPLE = Store.APP_STORE.value

# --- Parameter schema --------------------------------------------------------
#
# Descriptions reach the model ONLY through Field(description=...); the SDK does
# not parse an "Args:" docstring section. `app` matters most: it is a registry
# key, not a store id, and a model that passes a package name to a tool expecting
# a key gets a confusing "no registered app matches" instead of an answer.

AppKey = Annotated[
    str,
    Field(
        description=(
            "Which registered app: its apps.toml key, display name, Play package name or "
            "Apple ID. Empty works only when exactly one app is registered."
        )
    ),
]
MonthArg = Annotated[
    str,
    Field(
        description=(
            "Month for revenue, installs and rating, as 'YYYY-MM'. Empty means the last "
            "COMPLETE month, because the current one is always partial."
        )
    ),
]
StoreArg = Annotated[
    str,
    Field(
        description=(
            "Which store to act on: 'both' (default), 'play' or 'ios'."
        )
    ),
]
LocalesArg = Annotated[
    str,
    Field(
        description=(
            "Comma-separated locale codes as each store spells them, e.g. 'en-US,vi'. "
            "Empty means every locale found."
        )
    ),
]
MetadataDirArg = Annotated[
    str,
    Field(
        description=(
            "Root of the fastlane-layout metadata tree. Empty uses the app's metadata_dir "
            "from apps.toml, otherwise ~/.storepilot/metadata/<key>."
        )
    ),
]


# --- Formatting --------------------------------------------------------------


def _boundary(fn: Callable[..., str], *args: Any, **kwargs: Any) -> str:
    """Tool boundary: an MCP client renders a traceback verbatim, and a traceback
    contains no remedy. Every failure becomes an actionable block instead."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - never surface a traceback to a model
        return render_error(exc)


def _table(headers: list[str], rows: list[list[str]], *, align_right: set[int] | None = None) -> str:
    """Fixed-width table. Alignment is what makes a portfolio scan readable at a glance."""
    if not rows:
        return "(no rows)"
    right = align_right or set()
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    return "\n".join(
        [line(headers), "  ".join("-" * w for w in widths).rstrip(), *(line(r) for r in rows)]
    )


def _number(value: float | None) -> str:
    if value is None:
        return ""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _money(amount: float, currency: str | None) -> str:
    """Amount with its currency code. A bare number across two stores is a wrong answer."""
    return f"{amount:,.2f} {currency or '???'}"


def _truncate(text: str | None, limit: int) -> str:
    """Flatten and clip any store string before it is rendered.

    Routed through ``untrusted``: review bodies, reviewer display names and app
    names reach this module from both stores, and every one of them is written by
    someone outside the operator's control.
    """
    return untrusted(text, limit=limit)


def _default_month(today: date | None = None) -> str:
    """The last complete calendar month — the most recent one that can have a full report."""
    today = today or datetime.now(UTC).date()
    previous = today.replace(day=1) - timedelta(days=1)
    return f"{previous.year:04d}-{previous.month:02d}"


def _normalize_month(month: str) -> str:
    text = (month or "").strip()
    if not text:
        return _default_month()
    digits = re.sub(r"\D", "", text)
    if len(digits) < 6:
        raise ValidationError(
            f"{month!r} is not a month.",
            remedy="Pass a calendar month as 'YYYY-MM', e.g. '2026-07', or leave it empty "
            "for the last complete month.",
        )
    return f"{digits[:4]}-{digits[4:6]}"


def _freshness_lines(sources: list[Freshness | None]) -> list[str]:
    """Deduplicated staleness warnings, printed ABOVE the numbers they qualify."""
    seen: dict[str, None] = {}
    for freshness in sources:
        if freshness is None:
            continue
        warning = freshness.warning()
        if warning:
            seen.setdefault(warning, None)
    return [f"! {w}" for w in seen]


# --- Cell reasons ------------------------------------------------------------
#
# A blank cell is a lie by omission: the reader fills it in with "zero". Every
# missing value therefore carries a short code, and only the codes that actually
# appear get a legend line.

REASON_TEXT: dict[str, str] = {
    "off": "store not configured on this machine — run setup_doctor",
    "no-store": "this app does not exist on that store",
    "no-api": "the store's API publishes no such figure (not a StorePilot limitation)",
    "no-bucket": "Play reports bucket not configured (STOREPILOT_GOOGLE_REPORTS_BUCKET)",
    "no-perm": "authenticated, but this account lacks permission for that data",
    "not-pub": "the store has not published this period yet — NOT zero",
    "no-data": "the store returned no rows for this period",
    "suppressed": "Android Vitals suppresses metrics below a minimum daily user count",
    "quota": "rate limited upstream; the value is available, this call just could not fetch it",
    "no-vendor": "STOREPILOT_ASC_VENDOR_NUMBER is unset, so Apple sales reports cannot be read",
    "error": "the call failed — see 'Per-app issues' below",
}

_KIND_TO_REASON = {
    "credentials_error": "off",
    "api_not_enabled": "off",
    "permission_error": "no-perm",
    "not_found": "no-data",
    "rate_limited": "quota",
    "validation_error": "error",
    "upstream_error": "error",
}


def _reason_for(exc: BaseException) -> str:
    kind = getattr(exc, "kind", "")
    return _KIND_TO_REASON.get(str(kind), "error")


# --- Gateways ----------------------------------------------------------------


class PlayGateway:
    """Everything the cross-store tools need from Google Play, in one seam.

    A seam rather than direct calls for two reasons: the degradation behaviour is
    the most important property of these tools and has to be testable without a
    Play account, and centralising the calls is what keeps the Reporting API
    fan-out inside its quota.
    """

    store = Store.GOOGLE_PLAY

    def available(self) -> str | None:
        """``None`` when usable, otherwise the reason it is not."""
        if not settings.google_play_enabled:
            return (
                "Google Play is not configured. Set STOREPILOT_GOOGLE_CREDENTIALS to a service "
                "account JSON key and grant that account access in Play Console -> Users and "
                "permissions. Run setup_doctor for the full checklist."
            )
        return None

    def reports_available(self) -> str | None:
        """Installs/ratings/earnings live only in a GCS bucket, which may be unset."""
        if not (settings.google_reports_bucket or "").strip():
            return (
                "STOREPILOT_GOOGLE_REPORTS_BUCKET is not set, so installs, ratings and earnings "
                "are unavailable for every Play app (no Play REST API exposes them). Copy the "
                "Cloud Storage URI from Play Console -> Download reports."
            )
        return None

    def list_apps(self) -> list[StoreApp]:
        from storepilot.google_play import reporting

        return [StoreApp.from_app(app) for app in reporting.search_apps()]

    def vitals(self, package: str, days: int) -> Any:
        from storepilot.google_play import reporting

        return reporting.query_vitals(package, days=days)

    def installs(self, package: str, month: str) -> Report:
        from storepilot.google_play import gcs_reports

        return gcs_reports.get_installs(package, month)

    def ratings(self, package: str, month: str) -> Report:
        from storepilot.google_play import gcs_reports

        return gcs_reports.get_ratings(package, month)

    def earnings(self, month: str) -> Report:
        """Account-wide, ONE listing plus one download per month, then cached forever."""
        from storepilot.google_play import gcs_reports

        return gcs_reports.get_earnings(month)

    def release(self, package: str, track: str = "production") -> Release | None:
        """The live release on a track, without opening an edit unless it must.

        ``tracks.releases.list`` is edit-free and answers the question for a
        finished rollout. Only a release actually mid-rollout needs the exact
        ``userFraction``, which is visible solely through ``edits.tracks.get`` —
        so the expensive path is paid for by the rare app, not by all thirty.
        """
        from storepilot.google_play import publisher

        releases = publisher.list_track_releases(package, track)
        chosen = publisher.current_release({"releases": releases})
        if chosen is None:
            return None
        fraction = chosen.get("userFraction")
        status = str(chosen.get("status") or "unknown")
        if status == "inProgress" and fraction is None:
            detailed = publisher.current_release(publisher.read_track(package, track))
            if detailed is not None:
                chosen = detailed
                fraction = chosen.get("userFraction")
        return _play_release_model(package, track, chosen)

    def reviews(self, package: str, limit: int) -> list[Review]:
        return _play_reviews(package, limit)

    def listing(self, package: str, locale: str) -> dict[str, Any]:
        from storepilot.google_play import publisher

        return publisher.read_listing(package, locale)

    def listing_locales(self, package: str) -> list[str]:
        return _play_listing_locales(package)


class AppleGateway:
    """Everything the cross-store tools need from App Store Connect, in one seam."""

    store = Store.APP_STORE

    def available(self) -> str | None:
        if not settings.app_store_enabled:
            return (
                "App Store Connect is not configured. Set STOREPILOT_ASC_KEY_PATH (the .p8), "
                "STOREPILOT_ASC_KEY_ID and STOREPILOT_ASC_ISSUER_ID. Run setup_doctor for the "
                "full checklist."
            )
        return None

    def sales_available(self) -> str | None:
        if not (settings.asc_vendor_number or "").strip():
            return (
                "STOREPILOT_ASC_VENDOR_NUMBER is not set, so App Store revenue and units are "
                "unavailable. Find the 8-digit number in App Store Connect -> Payments and "
                "Financial Reports."
            )
        return None

    def client(self) -> Any:
        from storepilot.app_store.client import shared_client

        return shared_client()

    def list_apps(self) -> list[StoreApp]:
        from storepilot.app_store import resources
        from storepilot.app_store.client import attrs

        found = resources.list_apps(self.client(), limit=200)
        out: list[StoreApp] = []
        for resource in found.data:
            a = attrs(resource)
            out.append(
                StoreApp(
                    store=Store.APP_STORE,
                    app_id=str(resource.get("id", "")),
                    name=str(a.get("name") or a.get("bundleId") or resource.get("id") or "unknown"),
                    bundle_id=a.get("bundleId"),
                )
            )
        return out

    def sales(self, month: str) -> Report:
        """One MONTHLY, account-wide sales report for the whole portfolio.

        Deliberately not per app. ``/v1/salesReports`` is rate limited far more
        aggressively than the rest of Apple's API — a few hundred fetches a day
        shared across every tool using the key — so a portfolio scan that fetched
        per app would spend a user's entire daily budget on one table. One
        request covers every app, and the adapter caches a closed month forever.
        """
        from storepilot.app_store import reports

        return reports.get_sales(
            self.client(), report_date=month, frequency=reports.Frequency.MONTHLY
        )

    def release(self, apple_id: str) -> Release | None:
        from storepilot.app_store import resources

        found = resources.list_versions(self.client(), apple_id, limit=5)
        if not found.data:
            return None
        return resources.to_release(found, apple_id, found.data[0])

    def reviews(self, apple_id: str, limit: int) -> list[Review]:
        from storepilot.app_store import resources

        found = resources.list_reviews(self.client(), apple_id, limit=max(1, min(limit, 200)))
        return [resources.to_review(found, apple_id, r) for r in found.data]

    def listing(self, apple_id: str) -> AppleListing:
        return _apple_listing(self.client(), apple_id)


# --- Play helpers not covered by the adapter --------------------------------

_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+")


def _play_release_model(package: str, track: str, release: dict[str, Any]) -> Release:
    """Normalize a Play ``TrackRelease`` into the shared ``Release`` model.

    Play has no dedicated version-name field on a release; the human-readable
    version lives inside ``name`` (Play Console defaults it to "1.2.3 (45)"), so
    it is extracted rather than assumed.
    """
    name = str(release.get("name") or "")
    match = _VERSION_TOKEN.search(name)
    codes = [str(c) for c in (release.get("versionCodes") or [])]
    status_raw = str(release.get("status") or "unknown")
    try:
        status = ReleaseStatus(status_raw)
    except ValueError:
        status = ReleaseStatus.UNKNOWN
    fraction = release.get("userFraction")
    return Release(
        store=Store.GOOGLE_PLAY,
        app_id=package,
        track=track,
        version_name=(match.group(0) if match else (name or None)) or (codes[0] if codes else None),
        version_codes=codes,
        status=status,
        user_fraction=float(fraction) if fraction is not None else None,
        release_notes={
            str(n.get("language")): str(n.get("text", ""))
            for n in (release.get("releaseNotes") or [])
            if n.get("language")
        },
    )


def _play_reviews(package: str, limit: int) -> list[Review]:
    """Play reviews normalized into the shared ``Review`` model.

    The Play adapter's own review tool formats straight to text, so the shared
    model is built here. Google's constraints are unavoidable and are reported by
    the caller: production track only, comment-bearing reviews only, roughly the
    last seven days only, and an EMPTY list (not an error) when the service
    account lacks 'Reply to reviews'.
    """
    from storepilot.google_play import auth

    client = auth.publisher_client()
    try:
        payload = (
            client.reviews().list(packageName=package, maxResults=max(1, min(limit, 100))).execute()
        ) or {}
    except Exception as exc:
        raise auth.classify_google_error(
            exc, context="calling reviews.list", package_name=package
        ) from exc

    out: list[Review] = []
    for entry in payload.get("reviews") or []:
        user_comment: dict[str, Any] = {}
        replied = False
        for comment in entry.get("comments") or []:
            if "userComment" in comment:
                user_comment = comment["userComment"] or {}
            if "developerComment" in comment:
                replied = True
        rating = user_comment.get("starRating")
        if rating is None:
            continue
        updated = None
        seconds = (user_comment.get("lastModified") or {}).get("seconds")
        if seconds:
            try:
                updated = datetime.fromtimestamp(int(seconds), tz=UTC)
            except (TypeError, ValueError, OSError):
                updated = None
        out.append(
            Review(
                store=Store.GOOGLE_PLAY,
                app_id=package,
                review_id=str(entry.get("reviewId") or ""),
                rating=int(rating),
                # Flattened at construction, like the App Store side: both are
                # written by the app's users and both end up rendered into the
                # model's context by compare_reviews.
                text=untrusted(user_comment.get("text")) or None,
                author=untrusted(entry.get("authorName"), limit=60) or None,
                updated_at=updated,
                has_developer_reply=replied,
            )
        )
    return out


def _play_listing_locales(package: str) -> list[str]:
    """Locales that have a Play store listing.

    ``edits.listings.list`` needs an open edit, so a throwaway one is opened and
    discarded — the same trick ``publisher.read_track`` uses, and equally
    harmless: an uncommitted edit changes nothing.
    """
    from storepilot.google_play import auth, publisher

    with publisher.PlayEdit(package, dry_run=True, label="list listings") as edit:
        request = edit.edits.listings().list(packageName=package, editId=edit.id)
        try:
            payload = request.execute() or {}
        except Exception as exc:
            raise auth.classify_google_error(
                exc, context="listing store listings", package_name=package
            ) from exc
    return sorted(
        str(item.get("language")) for item in payload.get("listings") or [] if item.get("language")
    )


_PLAY_LISTING_FIELDS = {
    "title": "title",
    "short_description": "shortDescription",
    "full_description": "fullDescription",
    "video_url": "video",
}


def _play_listing_fields(listing: dict[str, Any]) -> dict[str, str | None]:
    return {ours: listing.get(theirs) for ours, theirs in _PLAY_LISTING_FIELDS.items()}


# --- Apple listing helpers ---------------------------------------------------

_APPLE_FIELD_TO_ASC = {
    "name": "name",
    "subtitle": "subtitle",
    "privacy_url": "privacyPolicyUrl",
    "description": "description",
    "keywords": "keywords",
    "promotional_text": "promotionalText",
    "whats_new": "whatsNew",
    "marketing_url": "marketingUrl",
    "support_url": "supportUrl",
}

#: Which Apple resource actually owns each field. Writing a name to the version
#: localization fails, and Apple's error does not say why.
_APPLE_INFO_FIELDS = {"name", "subtitle", "privacy_url"}


@dataclass
class AppleListing:
    """One app's listing copy plus the resource ids a write would target."""

    app_id: str
    version_id: str
    version_string: str
    state: str
    editable: bool
    fields: dict[str, dict[str, str | None]] = field(default_factory=dict)  # locale -> field -> text
    version_loc_ids: dict[str, str] = field(default_factory=dict)
    info_loc_ids: dict[str, str] = field(default_factory=dict)

    def locales(self) -> list[str]:
        return sorted(self.fields)

    def as_listing_text(self, locale: str) -> ListingText:
        values = self.fields.get(locale, {})
        return ListingText(
            store=Store.APP_STORE,
            app_id=self.app_id,
            locale=locale,
            title=values.get("name"),
            short_description=values.get("subtitle"),
            full_description=values.get("description"),
            keywords=values.get("keywords"),
            video_url=values.get("marketing_url"),
        )


def _apple_listing(client: Any, apple_id: str) -> AppleListing:
    """Read the listing copy of the editable version, falling back to the newest.

    ``resources.editable_version`` raises when every version is live, which is
    correct for a write and wrong for a read: a parity check still needs to see
    the text users are looking at right now. So the fallback is explicit and the
    result records whether the version can actually be written to.
    """
    from storepilot.app_store import resources
    from storepilot.app_store.client import attrs

    versions = resources.list_versions(client, apple_id, limit=10)
    if not versions.data:
        raise NotFoundError(
            f"App {apple_id} has no App Store versions.",
            remedy=(
                "The app record exists but has no version yet. Create the first version in App "
                "Store Connect; the API cannot create app records or their first version."
            ),
        )
    editable = next(
        (v for v in versions.data if resources.version_state(attrs(v)) in resources.EDITABLE_STATES),
        None,
    )
    chosen = editable or versions.data[0]
    version_id = str(chosen.get("id"))
    listing = AppleListing(
        app_id=apple_id,
        version_id=version_id,
        version_string=str(attrs(chosen).get("versionString") or "?"),
        state=resources.version_state(attrs(chosen)),
        editable=editable is not None,
    )

    version_locs = resources.version_localizations(client, version_id)
    for resource in version_locs.data:
        a = attrs(resource)
        locale = str(a.get("locale") or "")
        if not locale:
            continue
        listing.version_loc_ids[locale] = str(resource.get("id"))
        bucket = listing.fields.setdefault(locale, {})
        for ours, theirs in _APPLE_FIELD_TO_ASC.items():
            if ours in _APPLE_INFO_FIELDS:
                continue
            bucket[ours] = a.get(theirs)

    _info_id, info_locs = resources.app_info_localizations(client, apple_id)
    if info_locs is not None:
        for resource in info_locs.data:
            a = attrs(resource)
            locale = str(a.get("locale") or "")
            if not locale:
                continue
            listing.info_loc_ids[locale] = str(resource.get("id"))
            bucket = listing.fields.setdefault(locale, {})
            for ours in _APPLE_INFO_FIELDS:
                bucket[ours] = a.get(_APPLE_FIELD_TO_ASC[ours])
    return listing


# --- Portfolio collection ----------------------------------------------------


@dataclass
class StoreRow:
    """One app's numbers on one store, with a reason for every value that is missing."""

    store: Store
    app_id: str
    entry: PortfolioEntry
    reasons: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    freshness: list[Freshness] = field(default_factory=list)
    release: Release | None = None

    def cell(self, name: str, rendered: str | None) -> str:
        if rendered:
            return rendered
        return self.reasons.get(name, "n/a")


@dataclass
class PortfolioApp:
    pair: AppPair
    rows: dict[Store, StoreRow] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Portfolio:
    """Everything ``portfolio_overview`` renders, and nothing that renders it."""

    month: str
    days: int
    apps: list[PortfolioApp] = field(default_factory=list)
    store_errors: dict[Store, str] = field(default_factory=dict)
    store_notes: dict[Store, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    freshness: list[Freshness | None] = field(default_factory=list)
    unattributed: dict[str, dict[str, float]] = field(default_factory=dict)
    registry: Registry | None = None
    proposals_pending: int = 0


def _play_earnings_index(report: Report, packages: set[str]) -> tuple[
    dict[str, dict[str, float]], dict[str, float]
]:
    """Group Play earnings rows onto packages, keeping the leftovers visible.

    Earnings rows are keyed by *product* id, which is the package for an app sale
    and something like ``com.acme.todo.premium`` — or a bare SKU with no package
    in it at all — for in-app products. Anything that cannot be attributed to a
    known package is reported as unattributed rather than dropped (which would
    understate revenue) or spread around (which would invent it).
    """
    from storepilot.core.csv_reports import belongs_to_package

    per_app: dict[str, dict[str, float]] = {}
    unattributed: dict[str, float] = {}
    # Longest package first: com.acme.todo.pro must win over com.acme.todo when
    # both are registered and a product id could plausibly belong to either.
    ordered = sorted(packages, key=len, reverse=True)
    for row in report.rows:
        if row.metric != "earnings":
            continue
        currency = row.currency or "unknown"
        owner = next(
            (pkg for pkg in ordered if belongs_to_package(row.app_id, pkg)),
            None,
        )
        if owner is None:
            unattributed[currency] = unattributed.get(currency, 0.0) + row.value
            continue
        bucket = per_app.setdefault(owner, {})
        bucket[currency] = bucket.get(currency, 0.0) + row.value
    return per_app, unattributed


def _apple_sales_index(report: Report) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Group one account-wide sales report by Apple ID: ``{app_id: {units/currency}}``."""
    per_app: dict[str, dict[str, float]] = {}
    for row in report.rows:
        bucket = per_app.setdefault(row.app_id, {})
        if row.metric == "units":
            bucket["units"] = bucket.get("units", 0.0) + row.value
        elif row.metric == "proceeds":
            key = f"proceeds:{row.currency or 'unknown'}"
            bucket[key] = bucket.get(key, 0.0) + row.value
    return per_app, {}


def _install_total(report: Report) -> float | None:
    from storepilot.core import csv_reports

    totals = csv_reports.summarize(report.rows)
    for metric in ("daily_device_installs", "install_events", "daily_user_installs"):
        if metric in totals:
            return totals[metric]
    return None


def _latest_rating(report: Report) -> float | None:
    rows = [r for r in report.rows if r.metric == "total_average_rating"]
    if not rows:
        rows = [r for r in report.rows if r.metric == "daily_average_rating"]
    if not rows:
        return None
    return max(rows, key=lambda r: r.period).value


def _collect_play_row(
    play: PlayGateway,
    pair: AppPair,
    month: str,
    days: int,
    *,
    bucket_reason: str | None,
    earnings: dict[str, dict[str, float]] | None,
    earnings_reason: str | None,
) -> StoreRow:
    package = pair.play_package or ""
    row = StoreRow(
        store=Store.GOOGLE_PLAY,
        app_id=package,
        entry=PortfolioEntry(store=Store.GOOGLE_PLAY, app_id=package, name=pair.name),
    )

    try:
        vitals = play.vitals(package, days)
        row.entry.crash_rate = vitals.snapshot.crash_rate
        row.entry.anr_rate = vitals.snapshot.anr_rate
        row.freshness.append(vitals.freshness)
        if vitals.snapshot.exceeds_crash_threshold:
            row.entry.health_flags.append("CRASH")
        if vitals.snapshot.exceeds_anr_threshold:
            row.entry.health_flags.append("ANR")
        if row.entry.crash_rate is None and row.entry.anr_rate is None:
            row.reasons["crash"] = row.reasons["anr"] = "suppressed"
        for reading in getattr(vitals, "readings", {}).values():
            if getattr(reading, "error", None):
                row.errors.append(f"vitals {reading.key}: {reading.error}")
    except StorePilotError as exc:
        code = _reason_for(exc)
        row.reasons["crash"] = row.reasons["anr"] = code
        row.errors.append(f"vitals unavailable — {exc.message}")

    try:
        release = play.release(package)
        row.release = release
        if release is not None:
            row.entry.live_version = release.version_name
            row.entry.live_track = release.track
            row.entry.rollout_fraction = release.user_fraction
            if release.status is ReleaseStatus.HALTED:
                row.entry.health_flags.append("halted")
        else:
            row.reasons["version"] = "no-data"
    except StorePilotError as exc:
        row.reasons["version"] = _reason_for(exc)
        row.errors.append(f"production track unreadable — {exc.message}")

    if bucket_reason is not None:
        for cell in ("rating", "installs"):
            row.reasons[cell] = "no-bucket"
    else:
        try:
            ratings = play.ratings(package, month)
            row.entry.average_rating = _latest_rating(ratings)
            row.freshness.append(ratings.freshness)
            if row.entry.average_rating is None:
                row.reasons["rating"] = "no-data"
        except StorePilotError as exc:
            row.reasons["rating"] = _reason_for(exc)
            row.errors.append(f"ratings unavailable — {exc.message}")
        try:
            installs = play.installs(package, month)
            row.entry.installs_last_30d = _install_total(installs)
            row.freshness.append(installs.freshness)
            if row.entry.installs_last_30d is None:
                row.reasons["installs"] = "no-data"
        except StorePilotError as exc:
            row.reasons["installs"] = _reason_for(exc)
            row.errors.append(f"installs unavailable — {exc.message}")

    if earnings_reason is not None:
        row.reasons["revenue"] = earnings_reason
    else:
        by_currency = (earnings or {}).get(package, {})
        if by_currency:
            currency, amount = max(by_currency.items(), key=lambda kv: abs(kv[1]))
            row.entry.revenue_last_month = amount
            row.entry.revenue_currency = currency
            if len(by_currency) > 1:
                row.errors.append(
                    "earned in "
                    + ", ".join(_money(v, c) for c, v in sorted(by_currency.items()))
                    + " — the table shows the largest; see the revenue section for all of them"
                )
        else:
            row.reasons["revenue"] = "no-data"
    return row


def _collect_apple_row(
    apple: AppleGateway,
    pair: AppPair,
    *,
    sales: dict[str, dict[str, float]] | None,
    sales_reason: str | None,
) -> StoreRow:
    apple_id = pair.apple_id or ""
    row = StoreRow(
        store=Store.APP_STORE,
        app_id=apple_id,
        entry=PortfolioEntry(store=Store.APP_STORE, app_id=apple_id, name=pair.name),
    )
    # Apple publishes no crash or ANR rate through App Store Connect. Printing
    # "ok" here would claim a check that never happened.
    row.reasons["crash"] = row.reasons["anr"] = "no-api"
    row.reasons["rating"] = "no-api"

    try:
        release = apple.release(apple_id)
        row.release = release
        if release is not None:
            row.entry.live_version = release.version_name
            row.entry.live_track = release.track
            row.entry.rollout_fraction = release.user_fraction
            if release.status is ReleaseStatus.HALTED:
                row.entry.health_flags.append("halted")
            elif release.status is ReleaseStatus.IN_PROGRESS and release.user_fraction is None:
                row.entry.health_flags.append("in-review")
        else:
            row.reasons["version"] = "no-data"
    except StorePilotError as exc:
        row.reasons["version"] = _reason_for(exc)
        row.errors.append(f"versions unreadable — {exc.message}")

    if sales_reason is not None:
        row.reasons["revenue"] = row.reasons["installs"] = sales_reason
        return row

    bucket = (sales or {}).get(apple_id, {})
    units = bucket.get("units")
    if units is None:
        row.reasons["installs"] = "no-data"
    else:
        row.entry.installs_last_30d = units
    proceeds = {k.split(":", 1)[1]: v for k, v in bucket.items() if k.startswith("proceeds:")}
    if proceeds:
        currency, amount = max(proceeds.items(), key=lambda kv: abs(kv[1]))
        row.entry.revenue_last_month = amount
        row.entry.revenue_currency = currency
        if len(proceeds) > 1:
            row.errors.append(
                "earned in "
                + ", ".join(_money(v, c) for c, v in sorted(proceeds.items()))
                + " — the table shows the largest; see the revenue section for all of them"
            )
    else:
        row.reasons["revenue"] = "no-data"
    return row


def collect_portfolio(
    *,
    month: str,
    days: int,
    play: PlayGateway,
    apple: AppleGateway,
    registry: Registry,
) -> Portfolio:
    """Gather the whole cross-store portfolio, degrading at every level.

    The order of operations is chosen so a store-wide failure is discovered once
    rather than thirty times: the app lists and the two account-wide money
    reports are fetched first, and their failures become a single reason code
    stamped on every cell they would have filled.
    """
    portfolio = Portfolio(month=month, days=days, registry=registry)
    portfolio.warnings.extend(registry.warnings)

    play_apps: list[StoreApp] = []
    apple_apps: list[StoreApp] = []

    play_down = play.available()
    if play_down:
        portfolio.store_errors[Store.GOOGLE_PLAY] = play_down
    else:
        try:
            play_apps = play.list_apps()
        except StorePilotError as exc:
            portfolio.store_errors[Store.GOOGLE_PLAY] = render_error(exc)
            play_down = exc.message

    apple_down = apple.available()
    if apple_down:
        portfolio.store_errors[Store.APP_STORE] = apple_down
    else:
        try:
            apple_apps = apple.list_apps()
        except StorePilotError as exc:
            portfolio.store_errors[Store.APP_STORE] = render_error(exc)
            apple_down = exc.message

    unavailable = {store for store in Store if store in portfolio.store_errors}
    pairs, pair_warnings = registry_module.build_portfolio(
        play_apps, apple_apps, registry, unavailable=unavailable
    )
    portfolio.warnings.extend(pair_warnings)

    if not play_down and not apple_down:
        proposals, _, _ = registry_module.propose(play_apps, apple_apps, registry)
        portfolio.proposals_pending = len(proposals)

    # --- account-wide money, fetched once ---
    bucket_reason = play.reports_available() if not play_down else "store down"
    if bucket_reason and not play_down:
        portfolio.store_notes[Store.GOOGLE_PLAY] = bucket_reason

    play_earnings: dict[str, dict[str, float]] | None = None
    play_earnings_reason: str | None = None
    if play_down:
        play_earnings_reason = "off"
    elif bucket_reason:
        play_earnings_reason = "no-bucket"
    else:
        try:
            report = play.earnings(month)
            portfolio.freshness.append(report.freshness)
            packages = {p.play_package for p in pairs if p.play_package}
            play_earnings, unattributed = _play_earnings_index(report, packages)
            if unattributed:
                portfolio.unattributed["Google Play"] = unattributed
            if not report.rows:
                play_earnings_reason = "not-pub" if not report.freshness.is_complete else "no-data"
        except StorePilotError as exc:
            play_earnings_reason = _reason_for(exc)
            portfolio.warnings.append(f"Play earnings unavailable — {exc.message}")

    apple_sales: dict[str, dict[str, float]] | None = None
    apple_sales_reason: str | None = None
    vendor_missing = apple.sales_available() if not apple_down else None
    if apple_down:
        apple_sales_reason = "off"
    elif vendor_missing:
        apple_sales_reason = "no-vendor"
        portfolio.store_notes[Store.APP_STORE] = vendor_missing
    else:
        try:
            report = apple.sales(month)
            portfolio.freshness.append(report.freshness)
            apple_sales, _ = _apple_sales_index(report)
            if not report.rows:
                apple_sales_reason = "not-pub" if not report.freshness.is_complete else "no-data"
        except StorePilotError as exc:
            apple_sales_reason = _reason_for(exc)
            portfolio.warnings.append(f"App Store sales unavailable — {exc.message}")

    # --- per app ---
    for pair in pairs:
        item = PortfolioApp(pair=pair, notes=list(pair.notes))
        if pair.play_package:
            if play_down:
                row = StoreRow(
                    store=Store.GOOGLE_PLAY,
                    app_id=pair.play_package,
                    entry=PortfolioEntry(
                        store=Store.GOOGLE_PLAY, app_id=pair.play_package, name=pair.name
                    ),
                    reasons=dict.fromkeys(
                        ("version", "rating", "installs", "revenue", "crash", "anr"), "off"
                    ),
                )
            else:
                row = _collect_play_row(
                    play,
                    pair,
                    month,
                    days,
                    bucket_reason=bucket_reason,
                    earnings=play_earnings,
                    earnings_reason=play_earnings_reason,
                )
            item.rows[Store.GOOGLE_PLAY] = row
            portfolio.freshness.extend(row.freshness)
        if pair.apple_id:
            if apple_down:
                row = StoreRow(
                    store=Store.APP_STORE,
                    app_id=pair.apple_id,
                    entry=PortfolioEntry(
                        store=Store.APP_STORE, app_id=pair.apple_id, name=pair.name
                    ),
                    reasons=dict.fromkeys(
                        ("version", "rating", "installs", "revenue", "crash", "anr"), "off"
                    ),
                )
            else:
                row = _collect_apple_row(
                    apple, pair, sales=apple_sales, sales_reason=apple_sales_reason
                )
            item.rows[Store.APP_STORE] = row
            portfolio.freshness.extend(row.freshness)
        portfolio.apps.append(item)

    return portfolio


# --- Portfolio rendering -----------------------------------------------------

_STORE_LABEL = {Store.GOOGLE_PLAY: "play", Store.APP_STORE: "ios"}
_STORE_TITLE = {Store.GOOGLE_PLAY: "Google Play", Store.APP_STORE: "App Store"}
_TRACK_SHORT = {
    "production": "prod",
    "internal": "internal",
    "alpha": "alpha",
    "beta": "beta",
    "appstore": "live",
    "testflight": "tf",
}


def _version_cell(row: StoreRow) -> str:
    entry = row.entry
    if not entry.live_version:
        return row.reasons.get("version", "n/a")
    parts = [entry.live_version]
    if entry.live_track:
        parts.append(_TRACK_SHORT.get(entry.live_track, entry.live_track))
    if entry.rollout_fraction is not None and 0 < entry.rollout_fraction < 1:
        parts.append(f"{entry.rollout_fraction * 100:g}%")
    return " ".join(parts)


def _rate_cell(value: float | None, limit: float, reason: str | None) -> str:
    if value is None:
        return reason or "n/a"
    return f"{value:.2f}%" + ("!" if value > limit else "")


def _health_cell(row: StoreRow) -> str:
    breaches = [f for f in row.entry.health_flags if f in ("CRASH", "ANR")]
    other = [f for f in row.entry.health_flags if f not in ("CRASH", "ANR")]
    if breaches:
        return "+".join(breaches) + ("," + ",".join(other) if other else "")
    if row.store is Store.APP_STORE:
        # Not "ok": Apple never told us anything about stability.
        return ",".join([*other, "no-vitals"])
    if row.entry.crash_rate is None and row.entry.anr_rate is None:
        return ",".join([*other, "unmeasured"])
    return ",".join([*other, "ok"])


def render_portfolio(portfolio: Portfolio) -> str:
    """The flagship table. Pure function of collected data — no I/O, fully testable."""
    from storepilot.google_play.reporting import (
        ANR_RATE_THRESHOLD_PERCENT,
        CRASH_RATE_THRESHOLD_PERCENT,
    )

    rows: list[list[str]] = []
    used_reasons: set[str] = set()
    app_count = len(portfolio.apps)
    paired = sum(1 for a in portfolio.apps if a.pair.is_paired)

    for item in portfolio.apps:
        first = True
        for store in (Store.GOOGLE_PLAY, Store.APP_STORE):
            row = item.rows.get(store)
            if row is None:
                continue
            entry = row.entry
            crash = _rate_cell(entry.crash_rate, CRASH_RATE_THRESHOLD_PERCENT, row.reasons.get("crash"))
            anr = _rate_cell(entry.anr_rate, ANR_RATE_THRESHOLD_PERCENT, row.reasons.get("anr"))
            rating = (
                f"{entry.average_rating:.2f}"
                if entry.average_rating is not None
                else row.reasons.get("rating", "n/a")
            )
            installs = _number(entry.installs_last_30d) or row.reasons.get("installs", "n/a")
            revenue = (
                _money(entry.revenue_last_month, entry.revenue_currency)
                if entry.revenue_last_month is not None
                else row.reasons.get("revenue", "n/a")
            )
            used_reasons.update(
                value
                for key, value in row.reasons.items()
                if key in ("crash", "anr", "rating", "installs", "revenue", "version")
            )
            rows.append(
                [
                    _truncate(item.pair.name, 24) if first else "",
                    _STORE_LABEL[store],
                    _truncate(row.app_id, 24),
                    _version_cell(row),
                    rating,
                    installs,
                    revenue,
                    crash,
                    anr,
                    _health_cell(row),
                ]
            )
            first = False
        if not item.rows:
            rows.append(
                [_truncate(item.pair.name, 24), "-", "-", "off", "off", "off", "off", "off", "off", "off"]
            )
            used_reasons.add("off")

    lines: list[str] = [
        f"StorePilot portfolio — {app_count} app(s), {paired} paired across both stores",
        f"Vitals: trailing {portfolio.days} days. Installs, revenue and rating: {portfolio.month}.",
        (
            f"Play thresholds: user-perceived crash {CRASH_RATE_THRESHOLD_PERCENT}%, "
            f"ANR {ANR_RATE_THRESHOLD_PERCENT}% ('!' marks a breach)."
        ),
    ]
    if any(Store.APP_STORE in item.rows for item in portfolio.apps):
        # The column holds two different measurements. Play's is device installs
        # from the stats report; Apple's is sales UNITS, which counts first-time
        # downloads and re-downloads differently. Reading one row against the
        # other as if they were the same metric is a wrong comparison, so the
        # table says which is which rather than leaving the header to imply it.
        lines.append(
            "Installs column: Google Play rows are device installs; App Store rows are "
            "Apple sales UNITS. The two are not the same measurement — compare each store "
            "against its own history, not against the other."
        )
    lines.extend(_freshness_lines(portfolio.freshness))
    lines.append("")

    if rows:
        lines.append(
            _table(
                [
                    "App",
                    "Store",
                    "ID",
                    "Live version",
                    "Rating",
                    "Installs",
                    "Revenue",
                    "Crash",
                    "ANR",
                    "Health",
                ],
                rows,
                align_right={5},
            )
        )
    else:
        lines.append("(no apps visible on either store)")
    lines.append("")

    legend = [f"  {code}: {REASON_TEXT[code]}" for code in sorted(used_reasons) if code in REASON_TEXT]
    if legend:
        lines.append("Cells that could not be filled:")
        lines.extend(legend)
        lines.append("")

    lines.extend(_revenue_section(portfolio))
    lines.extend(_attention_section(portfolio))
    lines.extend(_problem_section(portfolio))
    return "\n".join(lines).rstrip()


def _revenue_section(portfolio: Portfolio) -> list[str]:
    """Money, grouped per currency and per store, and never added across currencies."""
    totals: dict[str, dict[str, float]] = {}
    for item in portfolio.apps:
        for store, row in item.rows.items():
            amount = row.entry.revenue_last_month
            if amount is None:
                continue
            currency = row.entry.revenue_currency or "unknown"
            per_store = totals.setdefault(currency, {})
            label = _STORE_TITLE[store]
            per_store[label] = per_store.get(label, 0.0) + amount
    if not totals and not portfolio.unattributed:
        return []

    lines = [f"Revenue — {portfolio.month}"]
    for currency in sorted(totals, key=lambda c: -sum(abs(v) for v in totals[c].values())):
        per_store = totals[currency]
        breakdown = ", ".join(f"{store} {value:,.2f}" for store, value in sorted(per_store.items()))
        lines.append(f"  {currency:<6} {sum(per_store.values()):>14,.2f}   ({breakdown})")
    for store, buckets in portfolio.unattributed.items():
        for currency, amount in sorted(buckets.items()):
            lines.append(
                f"  {currency:<6} {amount:>14,.2f}   ({store}, could not be attributed to a "
                f"known app — in-app product SKUs that do not carry a package name)"
            )
    if len(totals) > 1:
        lines.append(
            "  Currencies are listed separately and never added together: StorePilot holds no "
            "exchange rate, and inventing one would produce a number that looks right and is not."
        )
    lines.append("")
    return lines


def _attention_section(portfolio: Portfolio) -> list[str]:
    """The part someone actually acts on."""
    breaching: list[str] = []
    unmeasured: list[str] = []
    rolling: list[str] = []
    drifting: list[str] = []
    measured = 0

    for item in portfolio.apps:
        play_row = item.rows.get(Store.GOOGLE_PLAY)
        apple_row = item.rows.get(Store.APP_STORE)
        for store, row in item.rows.items():
            flags = [f for f in row.entry.health_flags if f in ("CRASH", "ANR")]
            if flags:
                breaching.append(
                    f"  - {item.pair.name} ({_STORE_TITLE[store]} {row.app_id}): "
                    f"{' and '.join(flags)} above Google's threshold. "
                    f"Run play_get_anomalies('{row.app_id}') to localize the spike."
                )
            if store is Store.GOOGLE_PLAY:
                if row.entry.crash_rate is None and row.entry.anr_rate is None:
                    if not flags:
                        unmeasured.append(f"{item.pair.name} ({row.app_id})")
                else:
                    measured += 1
            fraction = row.entry.rollout_fraction
            if fraction is not None and 0 < fraction < 1:
                rolling.append(
                    f"  - {item.pair.name} ({_STORE_TITLE[store]}): "
                    f"{row.entry.live_version or '?'} is at {fraction * 100:g}% of users"
                )
        if play_row and apple_row:
            left = play_row.entry.live_version
            right = apple_row.entry.live_version
            if left and right and left != right:
                drifting.append(f"  - {item.pair.name}: Play {left} vs App Store {right}")

    lines: list[str] = []
    if breaching:
        lines.append(f"NEEDS ATTENTION — {len(breaching)} store listing(s) over a Google threshold:")
        lines.extend(breaching)
        lines.append("")
    if rolling:
        lines.append("Rollouts in flight:")
        lines.extend(rolling)
        lines.append("")
    if drifting:
        lines.append("Version drift between stores (parity_check explains each one):")
        lines.extend(drifting)
        lines.append("")
    if unmeasured:
        lines.append(
            f"{len(unmeasured)} Play app(s) have NO vitals reading and were therefore not "
            f"checked against any threshold — this is not a clean bill of health, it is an "
            f"absence of measurement: {', '.join(unmeasured[:8])}"
            + (f" (+{len(unmeasured) - 8} more)" if len(unmeasured) > 8 else "")
        )
        lines.append("")
    if not breaching and not lines:
        # Only claim a clean bill of health if something was actually measured.
        # "No app exceeds a threshold" is a very different sentence when the
        # answer is "because no app was checked".
        lines.append(
            f"No app exceeds a Google crash or ANR threshold ({measured} app(s) measured)."
            if measured
            else (
                "NOTHING was measured against Google's crash or ANR thresholds in this run, so "
                "no app can be called healthy from this table — see the store errors below."
            )
        )
        lines.append("")
    return lines


def _problem_section(portfolio: Portfolio) -> list[str]:
    lines: list[str] = []
    for store, message in portfolio.store_errors.items():
        lines.append(f"{_STORE_TITLE[store]} columns are unavailable for every app:")
        lines.append("  " + message.replace("\n", "\n  "))
        lines.append("")
    for store, message in portfolio.store_notes.items():
        if message and store not in portfolio.store_errors:
            lines.append(f"{_STORE_TITLE[store]}: {message}")
            lines.append("")

    issues: list[str] = []
    for item in portfolio.apps:
        for note in item.notes:
            issues.append(f"  - {item.pair.name}: {note}")
        for store, row in item.rows.items():
            issues.extend(
                f"  - {item.pair.name} ({_STORE_LABEL[store]} {row.app_id}): {error}"
                for error in row.errors
            )
    if issues:
        lines.append("Per-app issues:")
        lines.extend(issues[:40])
        if len(issues) > 40:
            lines.append(f"  ... and {len(issues) - 40} more")
        lines.append("")
    if portfolio.warnings:
        lines.append("Registry notes:")
        lines.extend(f"  - {w}" for w in portfolio.warnings[:20])
        lines.append("")
    if portfolio.proposals_pending:
        lines.append(
            f"{portfolio.proposals_pending} app(s) look like they exist on both stores but are "
            f"not paired in {registry_module.registry_path()}. Run suggest_app_pairs to see the "
            f"proposals — until they are written down, each store's copy gets its own row."
        )
    return lines


# --- Tool: portfolio_overview ------------------------------------------------


def portfolio_overview(
    month: str = "",
    days: int = 28,
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    target = _normalize_month(month)
    if days < 1 or days > 365:
        raise ValidationError(
            f"days={days} is out of range.",
            remedy="Pass a vitals window between 1 and 365 days; 28 (the default) and 7 match "
            "Google's own rolling averages.",
        )
    portfolio = collect_portfolio(
        month=target,
        days=days,
        play=play or PlayGateway(),
        apple=apple or AppleGateway(),
        registry=registry if registry is not None else registry_module.load(),
    )
    return render_portfolio(portfolio)


# --- Tool: registry management ----------------------------------------------


def _resolve_pair(
    query: str,
    registry: Registry,
    *,
    require_paired: bool = False,
    what: str = "this tool",
) -> AppPair:
    """Turn an ``app`` argument into exactly one registry entry, or explain why not."""
    pairs = registry.pairs
    if not pairs:
        raise ValidationError(
            f"No apps are registered, so {what} has nothing to work on.",
            remedy=(
                f"Cross-store tools need to know which Play package is which App Store app; "
                f"no API says so. Run suggest_app_pairs to get proposals, then pair_apps to "
                f"write them to {registry.path}."
            ),
        )
    candidates = [p for p in pairs if p.matches(query)] if query.strip() else list(pairs)
    if not candidates:
        raise ValidationError(
            f"No registered app matches {query!r}.",
            remedy=(
                "Registered apps: "
                + ", ".join(f"{p.key} ({p.describe()})" for p in pairs[:12])
                + ". Run list_app_pairs for the full list."
            ),
        )
    if require_paired:
        both = [p for p in candidates if p.is_paired]
        if not both:
            single = candidates[0]
            raise ValidationError(
                f"{single.name} is registered on only one store, so {what} has nothing to "
                f"compare.",
                remedy=(
                    f"Add the missing side to [apps.{single.key}] in {registry.path} — "
                    f"pair_apps(key='{single.key}', play='com.example.app', "
                    f"appstore='1234567890') writes it. Run suggest_app_pairs for proposals."
                ),
            )
        candidates = both
    if len(candidates) > 1:
        raise ValidationError(
            f"{len(candidates)} apps are registered, so {what} needs to know which one."
            if not query.strip()
            else f"{query!r} matches {len(candidates)} apps.",
            remedy=(
                "Pass app= with one of: "
                + ", ".join(f"'{p.key}'" for p in candidates[:12])
                + ("." if len(candidates) <= 12 else ", ... (list_app_pairs shows them all).")
            ),
        )
    return candidates[0]


def list_app_pairs(*, registry: Registry | None = None) -> str:
    reg = registry if registry is not None else registry_module.load()
    lines = [f"App registry: {reg.path}"]
    if not reg.exists:
        lines.append("(the file does not exist yet — nothing is paired)")
    lines.append("")
    if reg.pairs:
        lines.append(
            _table(
                ["Key", "Name", "Play package", "Apple ID", "Metadata dir"],
                [
                    [
                        p.key,
                        _truncate(p.name, 28),
                        p.play_package or "-",
                        p.apple_id or "-",
                        p.metadata_dir or "(default)",
                    ]
                    for p in reg.pairs
                ],
            )
        )
    else:
        lines.append("No apps are paired yet.")
    lines.append("")
    if reg.warnings:
        lines.append("Problems in the file:")
        lines.extend(f"  ! {w}" for w in reg.warnings)
        lines.append("")
    lines.append(
        "Neither store's API can tell you that a Play package and an App Store app are the same "
        "product, so this mapping has to be written down. suggest_app_pairs proposes pairs from "
        "bundle-id and name evidence; pair_apps writes them here. Apps on only one store are "
        "fine — they still appear in portfolio_overview."
    )
    return "\n".join(lines)


def suggest_app_pairs(
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    play_gw = play or PlayGateway()
    apple_gw = apple or AppleGateway()
    reg = registry if registry is not None else registry_module.load()

    problems: list[str] = []
    play_apps: list[StoreApp] = []
    apple_apps: list[StoreApp] = []
    down = play_gw.available()
    if down:
        problems.append(down)
    else:
        try:
            play_apps = play_gw.list_apps()
        except StorePilotError as exc:
            problems.append(render_error(exc))
    down = apple_gw.available()
    if down:
        problems.append(down)
    else:
        try:
            apple_apps = apple_gw.list_apps()
        except StorePilotError as exc:
            problems.append(render_error(exc))

    if not play_apps or not apple_apps:
        return "\n\n".join(
            [
                (
                    "Auto-pairing needs both stores reachable — a pair is a claim about two "
                    "apps, and it cannot be made from one side."
                ),
                *problems,
                f"Registered so far: {len(reg.pairs)} app(s) in {reg.path}.",
            ]
        )

    proposals, unmatched_play, unmatched_apple = registry_module.propose(play_apps, apple_apps, reg)

    lines = [
        (
            f"Auto-pairing: {len(play_apps)} Play app(s) x {len(apple_apps)} App Store app(s), "
            f"{len(reg.pairs)} already registered"
        ),
        "",
        (
            "NOTHING BELOW IS ACTIVE. These are proposals; a pair only counts once it is "
            f"written to {reg.path}. That is deliberate: a wrong pair silently attributes one "
            "app's revenue, reviews and crash rate to another, and nothing downstream would "
            "look wrong."
        ),
        "",
    ]
    if proposals:
        lines.append(
            _table(
                ["Confidence", "Key", "Play package", "Apple ID", "App Store name", "Evidence"],
                [
                    [
                        f"{p.confidence} {p.score:.2f}",
                        p.key,
                        p.play.app_id,
                        p.apple.app_id,
                        _truncate(p.apple.name, 24),
                        "; ".join(p.reasons) or "weak",
                    ]
                    for p in proposals
                ],
            )
        )
        lines.append("")
        lines.append("To accept them, call pair_apps once per app:")
        for proposal in proposals[:10]:
            lines.append(
                f"  pair_apps(key='{proposal.key}', play='{proposal.play.app_id}', "
                f"appstore='{proposal.apple.app_id}', name='{proposal.play.name}')"
            )
        if len(proposals) > 10:
            lines.append(f"  ... and {len(proposals) - 10} more")
        lines.append("")
    else:
        lines.append("No pair reached the confidence floor. Write them by hand with pair_apps.")
        lines.append("")

    if unmatched_play or unmatched_apple:
        lines.append("Unmatched — these look like single-store apps (which is a normal state):")
        for app in unmatched_play[:15]:
            lines.append(f"  play  {app.app_id}  {_truncate(app.name, 40)}")
        for app in unmatched_apple[:15]:
            lines.append(f"  ios   {app.app_id}  {_truncate(app.name, 40)} ({app.bundle_id or '?'})")
        lines.append("")
    lines.append(
        "They still appear in portfolio_overview on their own row — StorePilot never hides an "
        "app because it could not pair it."
    )
    return "\n".join(lines)


def pair_apps(
    key: str = "",
    play: str = "",
    appstore: str = "",
    name: str = "",
    bundle_id: str = "",
    metadata_dir: str = "",
    locales: str = "",
    *,
    registry: Registry | None = None,
) -> str:
    reg = registry if registry is not None else registry_module.load()
    play_id = play.strip()
    apple_id = appstore.strip()
    if not play_id and not apple_id:
        raise ValidationError(
            "pair_apps needs at least one store id.",
            remedy=(
                "Pass play='com.example.app' and/or appstore='1234567890'. An app on one store "
                "only is a legitimate entry — it keeps its name and metadata directory in the "
                "registry and still appears in portfolio_overview."
            ),
        )
    if apple_id and not apple_id.isdigit():
        raise ValidationError(
            f"appstore={apple_id!r} is not a numeric Apple ID.",
            remedy=(
                "App Store Connect identifies apps by their numeric Apple ID (e.g. 1234567890), "
                "which asc_list_apps prints next to each app. The bundle id goes in bundle_id."
            ),
        )

    resolved_key = registry_module.slugify(
        key.strip() or name.strip() or play_id.rsplit(".", 1)[-1] or f"apple-{apple_id}"
    )
    # Adding the second store to an app that is already registered must not
    # rename it: an omitted `name` means "leave it alone", not "call it 1234567890".
    existing = reg.get(resolved_key) or next(
        (
            p
            for p in reg.pairs
            if (play_id and p.play_package == play_id) or (apple_id and p.apple_id == apple_id)
        ),
        None,
    )
    display_name = (
        name.strip()
        or (existing.name if existing else "")
        or play_id
        or apple_id
        or resolved_key
    )
    pair = AppPair(
        key=resolved_key,
        name=display_name,
        play_package=play_id or None,
        apple_id=apple_id or None,
        bundle_id=bundle_id.strip() or (play_id or None),
        metadata_dir=metadata_dir.strip() or None,
        locales=tuple(loc.strip() for loc in locales.split(",") if loc.strip()),
    )
    pairs, what = registry_module.upsert(reg, pair)
    path = registry_module.save(pairs)
    lines = [
        f"{what}",
        f"Written to {path}.",
        "",
        _table(
            ["Key", "Name", "Play package", "Apple ID"],
            [[p.key, _truncate(p.name, 28), p.play_package or "-", p.apple_id or "-"] for p in pairs],
        ),
        "",
        (
            "portfolio_overview, compare_reviews, parity_check, release_both and the metadata "
            "tools all read this file."
        ),
    ]
    return "\n".join(lines)


# --- Tool: compare_reviews ---------------------------------------------------

_PLAY_REVIEW_LIMITS = (
    "Google Play returns PRODUCTION-track reviews that carry a comment, from roughly the last "
    "7 days only. A bare star rating with no text never appears, and neither does testing-track "
    "feedback. That is a Play API limitation, not a filter applied here — so a smaller Play "
    "sample than App Store sample is expected and does not mean the app is quieter on Android."
)


def compare_reviews(
    app: str = "",
    days: int = 30,
    limit: int = 50,
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    reg = registry if registry is not None else registry_module.load()
    pair = _resolve_pair(app, reg, what="compare_reviews")
    play_gw = play or PlayGateway()
    apple_gw = apple or AppleGateway()
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    per_store: dict[Store, list[Review]] = {}
    problems: list[str] = []

    if pair.play_package:
        down = play_gw.available()
        if down:
            problems.append(f"Google Play: {down}")
        else:
            try:
                found = play_gw.reviews(pair.play_package, limit)
                if not found:
                    problems.append(play_reviews_empty_hint(pair.play_package))
                per_store[Store.GOOGLE_PLAY] = found
            except StorePilotError as exc:
                problems.append(f"Google Play: {render_error(exc)}")
    else:
        problems.append(f"{pair.name} is not registered on Google Play.")

    if pair.apple_id:
        down = apple_gw.available()
        if down:
            problems.append(f"App Store: {down}")
        else:
            try:
                found = apple_gw.reviews(pair.apple_id, limit * 2)
                kept = [
                    r for r in found if r.updated_at is None or r.updated_at >= cutoff
                ][:limit]
                per_store[Store.APP_STORE] = kept
                if found and not kept:
                    problems.append(
                        f"App Store: {len(found)} review(s) exist but none in the last {days} "
                        f"days. Widen `days` to see them."
                    )
            except StorePilotError as exc:
                problems.append(f"App Store: {render_error(exc)}")
    else:
        problems.append(f"{pair.name} is not registered on the App Store.")

    lines = [
        f"Review comparison — {pair.name}",
        f"Window: last {days} days (App Store), see the Play caveat below. Cap: {limit} per store.",
        "",
    ]

    counts: list[list[str]] = []
    for rating in range(5, 0, -1):
        row = [f"{rating}*"]
        for store in (Store.GOOGLE_PLAY, Store.APP_STORE):
            reviews = per_store.get(store)
            if reviews is None:
                row.append("-")
                continue
            row.append(str(sum(1 for r in reviews if r.rating == rating)))
        counts.append(row)
    totals = ["total"]
    averages = ["average"]
    for store in (Store.GOOGLE_PLAY, Store.APP_STORE):
        reviews = per_store.get(store)
        if reviews is None:
            totals.append("-")
            averages.append("-")
            continue
        totals.append(str(len(reviews)))
        averages.append(
            f"{sum(r.rating for r in reviews) / len(reviews):.2f}" if reviews else "n/a"
        )
    lines.append(
        _table(
            ["Rating", "Google Play", "App Store"],
            [*counts, totals, averages],
            align_right={1, 2},
        )
    )
    lines.append("")
    lines.append(
        "The two samples are NOT comparable as populations — the averages above describe the "
        "reviews each store returned, not each store's rating. Play's own rating average comes "
        "from play_get_stats; Apple publishes no rating average through its API at all."
    )
    lines.append("")

    for rating in range(1, 6):
        block: list[str] = []
        for store in (Store.GOOGLE_PLAY, Store.APP_STORE):
            for review in per_store.get(store, []):
                if review.rating != rating:
                    continue
                when = review.updated_at.date().isoformat() if review.updated_at else "?"
                reply = " [replied]" if review.has_developer_reply else ""
                block.append(
                    f"  [{_STORE_TITLE[store]} | {when} | "
                    f"{_truncate(review.author, 60) or 'anonymous'}]{reply}\n"
                    f"    {_truncate(review.text, 500) or '(no text)'}"
                )
        if block:
            lines.append(f"--- {rating}-star ({len(block)}) ---")
            lines.extend(block)
            lines.append("")

    lines.append(UNTRUSTED_CONTENT_NOTE)
    lines.append("")
    lines.append(_PLAY_REVIEW_LIMITS)
    if problems:
        lines.append("")
        lines.append("Gaps in this comparison:")
        lines.extend(f"  ! {p}" for p in problems)
    return "\n".join(lines)


# --- Tool: parity_check ------------------------------------------------------


def _play_version_of(release: Release | None) -> str | None:
    return release.version_name if release else None


def parity_check(
    app: str = "",
    locale: str = "",
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    reg = registry if registry is not None else registry_module.load()
    targets = (
        [_resolve_pair(app, reg, require_paired=True, what="parity_check")]
        if app.strip()
        else [p for p in reg.pairs if p.is_paired]
    )
    if not targets:
        raise ValidationError(
            "No app is registered on both stores, so there is no parity to check.",
            remedy=(
                "parity_check compares one product's two store listings. Pair an app first: "
                "suggest_app_pairs proposes, pair_apps writes."
            ),
        )
    play_gw = play or PlayGateway()
    apple_gw = apple or AppleGateway()

    blocked = [m for m in (play_gw.available(), apple_gw.available()) if m]
    if blocked:
        return "parity_check needs both stores configured.\n\n" + "\n\n".join(blocked)

    sections: list[str] = [f"Cross-store parity — {len(targets)} paired app(s)", ""]
    for pair in targets:
        sections.extend(_parity_for(pair, play_gw, apple_gw, locale))
    sections.append(
        "Only differences are listed. Fields that already match are not repeated — a parity "
        "report that prints everything is a data dump, not a report."
    )
    return "\n".join(sections)


def _parity_for(
    pair: AppPair, play_gw: PlayGateway, apple_gw: AppleGateway, locale_hint: str
) -> list[str]:
    lines = [f"== {pair.name} ({pair.play_package} / {pair.apple_id})", ""]
    differences = 0

    play_release: Release | None = None
    apple_release: Release | None = None
    try:
        play_release = play_gw.release(pair.play_package or "")
    except StorePilotError as exc:
        lines.append(f"  ! Play release unreadable — {exc.message}")
    try:
        apple_release = apple_gw.release(pair.apple_id or "")
    except StorePilotError as exc:
        lines.append(f"  ! App Store version unreadable — {exc.message}")

    left = _play_version_of(play_release)
    right = _play_version_of(apple_release)
    if left and right and left != right:
        differences += 1
        lines.append(f"  version: Play {left}  !=  App Store {right}")
    elif left and right:
        lines.append(f"  version: {left} on both stores")
    else:
        lines.append(f"  version: Play {left or 'unknown'} / App Store {right or 'unknown'}")

    for release, label in ((play_release, "Play"), (apple_release, "App Store")):
        if release is None:
            continue
        if release.is_staged_rollout:
            differences += 1
            lines.append(
                f"  rollout: {label} is mid-rollout at "
                f"{(release.user_fraction or 0) * 100:g}% on '{release.track}' "
                f"(status {release.status.value})"
            )
        elif release.status is ReleaseStatus.HALTED:
            differences += 1
            lines.append(f"  rollout: {label} release is HALTED on '{release.track}'")

    # --- listing text ---
    play_locale = (locale_hint or (pair.locales[0] if pair.locales else "en-US")).strip()
    apple_locale = mirror.map_locale(play_locale, source=Store.GOOGLE_PLAY, target=Store.APP_STORE)
    if apple_locale is None:
        lines.append(
            "  locale: "
            + mirror.unmapped_locale_note(
                play_locale, source=Store.GOOGLE_PLAY, target=Store.APP_STORE
            )
        )
        lines.append("")
        return lines
    if apple_locale != play_locale:
        lines.append(f"  locale: Play '{play_locale}' maps to App Store '{apple_locale}'")

    play_fields: dict[str, str | None] = {}
    apple_fields: dict[str, str | None] = {}
    try:
        play_fields = _play_listing_fields(play_gw.listing(pair.play_package or "", play_locale))
    except StorePilotError as exc:
        lines.append(f"  ! Play listing unreadable — {exc.message}")
    try:
        listing = apple_gw.listing(pair.apple_id or "")
        apple_fields = dict(listing.fields.get(apple_locale, {}))
        if apple_locale not in listing.fields:
            lines.append(
                f"  ! App Store has no '{apple_locale}' localization "
                f"(has: {', '.join(listing.locales()) or 'none'})"
            )
        if not listing.editable:
            lines.append(
                f"  note: App Store copy read from version {listing.version_string} "
                f"({listing.state}), which is frozen — metadata_push would need a new version"
            )
    except StorePilotError as exc:
        lines.append(f"  ! App Store listing unreadable — {exc.message}")

    for play_field, apple_field in mirror.PARITY_PAIRS:
        left_value = (play_fields.get(play_field) or "").strip()
        right_value = (apple_fields.get(apple_field) or "").strip()
        if not left_value and not right_value:
            continue
        play_spec = mirror.spec_for(Store.GOOGLE_PLAY, play_field)
        apple_spec = mirror.spec_for(Store.APP_STORE, apple_field)
        play_label = play_spec.label if play_spec else play_field
        apple_label = apple_spec.label if apple_spec else apple_field
        label = f"{play_label}/{apple_label}"
        if left_value == right_value:
            continue
        differences += 1
        if not right_value:
            lines.append(f"  {label}: only on Play ({len(left_value)} chars)")
        elif not left_value:
            lines.append(f"  {label}: only on the App Store ({len(right_value)} chars)")
        else:
            lines.append(f"  {label}: differs")
            lines.append(f"      play | {_truncate(left_value, 110)}")
            lines.append(f"      ios  | {_truncate(right_value, 110)}")
        # The check that saves a review cycle: would this text even fit if copied?
        over_to_apple = mirror.over_limit(Store.APP_STORE, apple_field, left_value or None)
        over_to_play = mirror.over_limit(Store.GOOGLE_PLAY, play_field, right_value or None)
        if over_to_apple:
            lines.append(
                f"      -> copying Play's text to the App Store would exceed Apple's "
                f"{apple_spec.limit if apple_spec else '?'}-character {apple_field} limit by "
                f"{over_to_apple}"
            )
        if over_to_play:
            lines.append(
                f"      -> copying the App Store text to Play would exceed Play's "
                f"{play_spec.limit if play_spec else '?'}-character {play_field} limit by "
                f"{over_to_play}"
            )

    apple_only = (apple_fields.get("keywords") or "").strip()
    if apple_only:
        lines.append(
            f"  keywords: App Store only ({len(apple_only)}/100 chars). Google Play has no "
            f"keyword field — Play discovery reads the title and descriptions instead."
        )
    if differences == 0:
        lines.append("  no drift found on version, rollout, or the compared listing fields")
    lines.append("")
    return lines


# --- Tool: release_both ------------------------------------------------------


def release_both_operation(
    *,
    pair: AppPair,
    version_name: str,
    play_track: str,
    play_status: str,
    play_fraction: float | None,
    aab_sha256: str | None,
    aab_size: int | None,
    aab_path: str | None,
    release_notes: str,
    apple_build_id: str | None,
    apple_build_number: str | None,
    testflight_locale: str,
    call_args: dict[str, Any],
) -> Operation:
    """The ONE operation covering both stores, hence one confirmation token.

    Every parameter that changes what either store does is in ``params``, so
    altering the Play track *or* the Apple build between preview and confirmation
    moves the fingerprint and the token stops verifying. A half-confirmed
    two-store release — the user approved the Play half and got the Apple half
    too, or vice versa — is worse than no release at all, which is why this is
    one operation and not two.
    """
    return Operation(
        tool="release_both",
        target=f"{target_for(_PLAY, pair.play_package or '-')}+"
        f"{target_for(_APPLE, pair.apple_id or '-')}",
        params={
            "app_key": pair.key,
            "play_package": pair.play_package,
            "apple_id": pair.apple_id,
            "version_name": version_name,
            "play_track": play_track,
            "play_status": play_status,
            "play_user_fraction": play_fraction,
            # The digest, not the path: rebuilding the AAB between preview and
            # confirmation must invalidate the token.
            "aab_sha256": aab_sha256,
            "aab_size_bytes": aab_size,
            "aab_path": aab_path,
            "release_notes": release_notes,
            "apple_build_id": apple_build_id,
            "apple_build_number": apple_build_number,
            "testflight_locale": testflight_locale,
        },
        call_args=call_args,
    )


@dataclass
class LegResult:
    """What one store's half of a two-store release actually did."""

    store: Store
    status: str  # "ok" | "failed" | "skipped" | "not-attempted"
    detail: str = ""
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _apple_beta_localizations(client: Any, build_id: str) -> list[dict[str, Any]]:
    return list(
        client.get_all(
            f"/v1/builds/{build_id}/betaBuildLocalizations",
            limit=50,
            context=f"reading TestFlight release notes for build {build_id}",
        ).data
    )


def _apple_set_beta_notes(client: Any, build_id: str, locale: str, text: str) -> str:
    """Publish TestFlight "What to Test" notes for a build.

    Testers see this text, so it is a user-visible publish and is gated with
    everything else in ``release_both``.
    """
    from storepilot.app_store.client import attrs, resource_body

    existing = _apple_beta_localizations(client, build_id)
    match = next(
        (r for r in existing if str(attrs(r).get("locale", "")).lower() == locale.lower()), None
    )
    if match is not None:
        client.patch(
            f"/v1/betaBuildLocalizations/{match.get('id')}",
            resource_body(
                "betaBuildLocalizations",
                attributes={"whatsNew": text},
                resource_id=str(match.get("id")),
            ),
            context=f"updating TestFlight notes for build {build_id}",
        )
        return "updated"
    client.post(
        "/v1/betaBuildLocalizations",
        resource_body(
            "betaBuildLocalizations",
            attributes={"whatsNew": text, "locale": locale},
            relationships={"build": ("builds", build_id)},
        ),
        context=f"creating TestFlight notes for build {build_id}",
    )
    return "created"


def release_both(
    version_name: str,
    aab_path: str = "",
    release_notes: str = "",
    play_track: str = "internal",
    app: str = "",
    testflight_locale: str = "en-US",
    confirm: bool = False,
    confirmation_token: str | None = None,
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    from storepilot.app_store import resources
    from storepilot.app_store.client import attrs
    from storepilot.google_play import publisher

    reg = registry if registry is not None else registry_module.load()
    pair = _resolve_pair(app, reg, require_paired=True, what="release_both")
    play_gw = play or PlayGateway()
    apple_gw = apple or AppleGateway()
    version = version_name.strip()
    if not version:
        raise ValidationError(
            "version_name is required.",
            remedy="Pass the marketing version both stores will show, e.g. '3.2.1'.",
        )

    for gateway, label in ((play_gw, "Google Play"), (apple_gw, "App Store Connect")):
        down = gateway.available()
        if down:
            raise ValidationError(
                f"release_both needs both stores configured; {label} is not.",
                remedy=down,
            )

    track = resolve_track(play_track)
    decision = PRODUCTION_POLICY.decide(track, operation="release_both")
    notes_body = {testflight_locale: release_notes.strip()} if release_notes.strip() else None

    # --- Play side: inspect the artifact locally before anything is uploaded ---
    aab = None
    if aab_path.strip():
        aab = publisher.inspect_aab(aab_path.strip())

    # --- Apple side: find the build that is already in TestFlight -------------
    client = apple_gw.client()
    builds = resources.list_builds(client, pair.apple_id or "", version=version, limit=10)
    build = builds.data[0] if builds.data else None
    build_id = str(build.get("id")) if build else None
    build_number = attrs(build).get("version") if build else None
    build_state = str(attrs(build).get("processingState") or "").upper() if build else ""

    op = release_both_operation(
        pair=pair,
        version_name=version,
        play_track=track,
        play_status=decision.status,
        play_fraction=decision.user_fraction,
        aab_sha256=aab.sha256 if aab else None,
        aab_size=aab.size_bytes if aab else None,
        aab_path=str(aab.path) if aab else None,
        release_notes=release_notes.strip(),
        apple_build_id=build_id,
        apple_build_number=str(build_number) if build_number else None,
        testflight_locale=testflight_locale,
        call_args={
            "version_name": version_name,
            "aab_path": aab_path,
            "release_notes": release_notes,
            "play_track": play_track,
            "app": app,
            "testflight_locale": testflight_locale,
        },
    )

    play_state: dict[str, Any] = {}

    def run_play(*, dry_run: bool) -> publisher.PlayEdit:
        with publisher.PlayEdit(
            pair.play_package or "", dry_run=dry_run, label="release_both"
        ) as edit:
            existing = publisher.current_release(edit.get_track(track))
            play_state["before"] = (
                publisher.describe_release(existing) if existing else "no active release"
            )
            if aab is not None:
                info = edit.upload_bundle(aab)
                play_state["version_code"] = info.version_code
                codes: list[Any] = [info.version_code]
            else:
                codes = list((existing or {}).get("versionCodes") or [])
                if not codes:
                    raise ValidationError(
                        f"No aab_path was given and '{track}' has no existing build to re-release.",
                        remedy=(
                            "Pass aab_path to upload a new bundle, or promote an existing build "
                            "with play_promote_release."
                        ),
                    )
                play_state["version_code"] = ", ".join(str(c) for c in codes)
            edit.set_release(
                track,
                codes,
                status=decision.status,
                user_fraction=decision.user_fraction,
                release_notes=notes_body,
            )
        return edit

    def build_preview() -> Preview:
        edit = run_play(dry_run=True)
        apple_effect = (
            f"TestFlight build {build_number} ({build_state}) already holds {version}; its "
            f"'What to Test' notes will be {'set' if release_notes.strip() else 'left unchanged'}"
            if build
            else f"NO TestFlight build with version {version} exists"
        )
        warnings = [
            *_release_both_warnings(decision, build, build_state, version),
        ]
        return Preview(
            summary=(
                f"Release {version} to BOTH stores: Google Play '{track}' "
                f"({decision.audience}) and Apple TestFlight."
            ),
            changes=[
                Change(
                    f"Google Play track '{track}'",
                    play_state.get("before"),
                    f"version code {play_state.get('version_code')} — {decision.status} — "
                    f"{decision.audience}",
                ),
                Change(
                    "Apple TestFlight",
                    f"build {build_number or 'none'} ({build_state or 'not found'})",
                    apple_effect,
                ),
                Change(
                    f"release notes ({testflight_locale})",
                    None,
                    release_notes.strip() or None,
                ),
            ],
            warnings=warnings,
            notes=[
                *decision.notes,
                (
                    "Apple has NO API that accepts a binary. The .ipa must already be in "
                    "TestFlight (xcrun iTMSTransporter / altool / Xcode Organizer); this tool "
                    "verifies the build is there and publishes its tester-visible notes."
                ),
                (
                    f"Play bundle sha256 {aab.sha256[:16]}… — rebuilding the AAB invalidates "
                    f"the token."
                    if aab
                    else "No AAB given: the build already on the track is re-released."
                ),
                (
                    "ONE token covers BOTH stores. Changing either store's parameters — the "
                    "track, the bundle, the Apple build — invalidates it, because approving "
                    "half a two-store release is not approving this release."
                ),
            ],
            reversal=(
                "Play: play_halt_rollout stops new users receiving the build (installs are not "
                "rolled back). TestFlight: expire the build in App Store Connect."
            ),
            verified_by=(
                f"Google Play accepted edits.validate on throwaway edit {edit.id}, which was "
                f"then discarded — the version code above is what Google actually assigned, not "
                f"an estimate. Nothing was published."
            ),
        )

    gate = require_confirmation(
        op, build_preview, confirm=confirm, confirmation_token=confirmation_token
    )
    if gate is not None:
        return gate

    # --- execution: Play first, then Apple -----------------------------------
    #
    # Play first because it is the leg that can fail for a reason worth stopping
    # on (a rejected bundle, a bad track). If it fails, Apple is never attempted,
    # which is the only outcome where "nothing happened" is still true.
    legs: list[LegResult] = []
    with audit_execution(op) as record:
        try:
            edit = run_play(dry_run=False)
            legs.append(
                LegResult(
                    Store.GOOGLE_PLAY,
                    "ok",
                    f"version code {play_state.get('version_code')} is live on '{track}' "
                    f"({decision.status}, {decision.audience}); edit {edit.id} committed",
                )
            )
            record.set("play_version_code", play_state.get("version_code"))
        except StorePilotError as exc:
            legs.append(
                LegResult(Store.GOOGLE_PLAY, "failed", exc.message, exc.remedy)
            )
            legs.append(
                LegResult(
                    Store.APP_STORE,
                    "not-attempted",
                    "the Play leg failed first, so nothing was sent to Apple",
                    "Fix the Play failure above and re-run release_both from the preview step.",
                )
            )
            record.note("play leg failed; apple leg not attempted")

        if legs[0].ok:
            legs.append(_run_apple_leg(client, build_id, build_state, version, release_notes,
                                       testflight_locale, pair))
        record.set("legs", {leg.store.value: leg.status for leg in legs})
        record.note("; ".join(f"{leg.store.value}={leg.status}" for leg in legs))

    partial = any(leg.status == "failed" for leg in legs) and any(leg.ok for leg in legs)
    if partial:
        audit(
            op,
            outcome="failed",
            detail="PARTIAL: " + "; ".join(f"{leg.store.value}={leg.status}" for leg in legs),
        )
    return append_warning(_render_release_result(pair, version, track, legs, partial))


def _release_both_warnings(
    decision: Any, build: dict[str, Any] | None, build_state: str, version: str
) -> list[str]:
    warnings: list[str] = []
    if decision.is_production:
        warnings.append(
            f"PRODUCTION on Google Play: this build reaches REAL USERS — {decision.audience}. "
            f"Users who install it keep it; halting stops new installs only."
        )
    if build is None:
        warnings.append(
            f"App Store Connect has NO TestFlight build for version {version}. The Play half "
            f"will still run and the Apple half will be reported as SKIPPED — this call cannot "
            f"produce a matching release on both stores. Upload the build first with "
            f"`xcrun iTMSTransporter -m upload -assetFile App.ipa -apiKey <KEY_ID> "
            f"-apiIssuer <ISSUER_ID>`, then re-run."
        )
    elif build_state and build_state != "VALID":
        warnings.append(
            f"The TestFlight build for {version} is in processingState {build_state}, not VALID. "
            f"PROCESSING resolves on its own; FAILED and INVALID do not."
        )
    return warnings


def _run_apple_leg(
    client: Any,
    build_id: str | None,
    build_state: str,
    version: str,
    release_notes: str,
    locale: str,
    pair: AppPair,
) -> LegResult:
    if build_id is None:
        return LegResult(
            Store.APP_STORE,
            "skipped",
            f"no TestFlight build carries version {version}, and no Apple API accepts a binary",
            (
                "Upload the .ipa, then re-run release_both to publish its notes:\n"
                "    xcrun iTMSTransporter -m upload -assetFile App.ipa \\\n"
                "      -apiKey <KEY_ID> -apiIssuer <ISSUER_ID>\n"
                "  asc_list_builds shows it once Apple finishes processing."
            ),
        )
    if not release_notes.strip():
        return LegResult(
            Store.APP_STORE,
            "ok",
            f"build {version} is already in TestFlight ({build_state or 'state unknown'}); "
            f"no release notes were given, so nothing was published",
        )
    try:
        action = _apple_set_beta_notes(client, build_id, locale, release_notes.strip())
    except StorePilotError as exc:
        return LegResult(Store.APP_STORE, "failed", exc.message, exc.remedy)
    return LegResult(
        Store.APP_STORE,
        "ok",
        f"TestFlight 'What to Test' notes {action} for build {version} ({locale}); testers see "
        f"them on their next refresh",
    )


def _render_release_result(
    pair: AppPair, version: str, track: str, legs: list[LegResult], partial: bool
) -> str:
    header = (
        f"!! PARTIAL RELEASE — {version} did NOT land on both stores"
        if partial
        else f"[done] release_both {version} — {pair.name}"
    )
    if all(leg.status in ("failed", "not-attempted") for leg in legs):
        header = f"!! RELEASE FAILED — {version} did not land anywhere"

    lines = [header, ""]
    for leg in legs:
        icon = {"ok": "[ok]", "failed": "[FAILED]", "skipped": "[skipped]", "not-attempted": "[not attempted]"}[
            leg.status
        ]
        lines.append(f"{icon} {_STORE_TITLE[leg.store]}: {leg.detail}")
        if leg.remedy:
            lines.append("    Next: " + leg.remedy.replace("\n", "\n    "))
    lines.append("")

    if partial:
        landed = [leg for leg in legs if leg.ok]
        failed = [leg for leg in legs if leg.status == "failed"]
        lines.append(
            "This is a partial release. "
            + " ".join(f"{_STORE_TITLE[leg.store]} DID change." for leg in landed)
            + " "
            + " ".join(f"{_STORE_TITLE[leg.store]} did NOT." for leg in failed)
        )
        lines.append(
            "Nothing was rolled back. Undoing the successful half is a separate, deliberate act "
            "— an automatic rollback here would pull a working build from users because the "
            "other store had an API error, which is a worse outcome than the drift."
        )
        lines.append(
            f"To finish: fix the failure above, then re-run release_both for {version}. The Play "
            f"half is idempotent for the same version code; re-running will not double-publish."
            if any(leg.store is Store.APP_STORE for leg in failed)
            else "To finish: re-run release_both once the failing store is reachable."
        )
        lines.append("")
    skipped = [leg for leg in legs if leg.status == "skipped"]
    if skipped and not partial:
        lines.append(
            "One store was skipped rather than failed — nothing went wrong there, the work "
            "simply cannot be done through that store's API. The stores are NOT in sync."
        )
        lines.append("")
    lines.append(f"Track: Google Play '{track}'. Version: {version}. App: {pair.describe()}.")
    lines.append("Verify with portfolio_overview or parity_check.")
    return "\n".join(lines)


# --- Tools: metadata_pull / metadata_push -----------------------------------


def _metadata_base(pair: AppPair, override: str) -> Path:
    return Path(override).expanduser() if override.strip() else registry_module.default_metadata_dir(pair)


def _pull_play(
    play_gw: PlayGateway, pair: AppPair, base: Path, locales: list[str], state: mirror.MirrorState
) -> tuple[list[str], list[str]]:
    written: list[str] = []
    problems: list[str] = []
    if not locales:
        try:
            locales = play_gw.listing_locales(pair.play_package or "")
        except StorePilotError as exc:
            problems.append(f"Google Play: cannot list listing locales — {exc.message}")
            return written, problems
    for locale in locales:
        try:
            fields = _play_listing_fields(play_gw.listing(pair.play_package or "", locale))
        except StorePilotError as exc:
            problems.append(f"Google Play {locale}: {exc.message}")
            continue
        writes = mirror.write_locale(base, Store.GOOGLE_PLAY, locale, fields)
        mirror.record_state(state, Store.GOOGLE_PLAY, locale, writes)
        for write in writes:
            written.append(
                f"  {write.status:<9} metadata/android/{locale}/{write.path.name} "
                f"({write.chars} chars)"
            )
    # Play release notes live per version code, which is fastlane supply's layout.
    try:
        release = play_gw.release(pair.play_package or "")
    except StorePilotError:
        release = None
    if release and release.version_codes and release.release_notes:
        for locale, text in release.release_notes.items():
            try:
                write = mirror.write_changelog(base, locale, release.version_codes[0], text)
            except ValidationError as exc:
                problems.append(f"changelog {locale}: {exc.message}")
                continue
            written.append(
                f"  {write.status:<9} metadata/android/{locale}/changelogs/"
                f"{release.version_codes[0]}.txt"
            )
    return written, problems


def _pull_apple(
    apple_gw: AppleGateway, pair: AppPair, base: Path, locales: list[str], state: mirror.MirrorState
) -> tuple[list[str], list[str]]:
    written: list[str] = []
    problems: list[str] = []
    try:
        listing = apple_gw.listing(pair.apple_id or "")
    except StorePilotError as exc:
        problems.append(f"App Store: {exc.message}")
        return written, problems

    wanted = locales or listing.locales()
    for locale in wanted:
        values = listing.fields.get(locale)
        if values is None:
            problems.append(
                f"App Store: no '{locale}' localization on version {listing.version_string} "
                f"(has: {', '.join(listing.locales()) or 'none'})"
            )
            continue
        writes = mirror.write_locale(base, Store.APP_STORE, locale, values)
        mirror.record_state(state, Store.APP_STORE, locale, writes)
        for write in writes:
            written.append(
                f"  {write.status:<9} metadata/ios/{locale}/{write.path.name} "
                f"({write.chars} chars)"
            )
    if not listing.editable:
        problems.append(
            f"App Store copy was read from version {listing.version_string} ({listing.state}), "
            f"which is frozen. metadata_push would need an editable version."
        )
    return written, problems


def metadata_pull(
    app: str = "",
    store: str = "both",
    locales: str = "",
    metadata_dir: str = "",
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    reg = registry if registry is not None else registry_module.load()
    pair = _resolve_pair(app, reg, what="metadata_pull")
    base = _metadata_base(pair, metadata_dir)
    wanted = _stores_arg(store)
    requested = [loc.strip() for loc in locales.split(",") if loc.strip()] or list(pair.locales)
    state = mirror.load_state(base)

    lines = [f"Pulling store metadata for {pair.name} into {base}", ""]
    problems: list[str] = []

    if Store.GOOGLE_PLAY in wanted:
        if not pair.play_package:
            problems.append(f"{pair.name} is not registered on Google Play.")
        else:
            down = (play or PlayGateway()).available()
            if down:
                problems.append(f"Google Play: {down}")
            else:
                written, issues = _pull_play(
                    play or PlayGateway(), pair, base, requested, state
                )
                lines.append(f"Google Play -> metadata/android/ ({len(written)} file(s))")
                lines.extend(written or ["  (nothing written)"])
                lines.append("")
                problems.extend(issues)

    if Store.APP_STORE in wanted:
        if not pair.apple_id:
            problems.append(f"{pair.name} is not registered on the App Store.")
        else:
            down = (apple or AppleGateway()).available()
            if down:
                problems.append(f"App Store: {down}")
            else:
                written, issues = _pull_apple(
                    apple or AppleGateway(), pair, base, requested, state
                )
                lines.append(f"App Store -> metadata/ios/ ({len(written)} file(s))")
                lines.extend(written or ["  (nothing written)"])
                lines.append("")
                problems.extend(issues)

    mirror.save_state(base, state)
    lines.append(
        "Layout is fastlane's: `supply` reads metadata/android and `deliver` reads metadata/ios "
        "with metadata_path: \"metadata/ios\". Files whose content already matched the store are "
        "reported as 'unchanged' and were not rewritten, so git sees only real edits."
    )
    lines.append(
        "Not pulled: screenshots and images (neither adapter uploads them yet) and "
        "review_information/ (Apple's appStoreReviewDetail is not wired in). Those files are "
        "left exactly as they are for fastlane to keep handling."
    )
    if problems:
        lines.append("")
        lines.append("Problems:")
        lines.extend(f"  ! {p}" for p in problems)
    return "\n".join(lines)


def _stores_arg(store: str) -> set[Store]:
    value = (store or "both").strip().lower()
    if value in ("both", "all", ""):
        return {Store.GOOGLE_PLAY, Store.APP_STORE}
    if value in ("play", "google", "google_play", "android"):
        return {Store.GOOGLE_PLAY}
    if value in ("apple", "ios", "app_store", "appstore", "asc"):
        return {Store.APP_STORE}
    raise ValidationError(
        f"Unknown store {store!r}.",
        remedy="Use 'both' (default), 'play' or 'ios'.",
    )


def metadata_push(
    app: str = "",
    store: str = "both",
    locales: str = "",
    metadata_dir: str = "",
    confirm: bool = False,
    confirmation_token: str | None = None,
    *,
    play: PlayGateway | None = None,
    apple: AppleGateway | None = None,
    registry: Registry | None = None,
) -> str:
    from storepilot.app_store import resources
    from storepilot.google_play import publisher

    reg = registry if registry is not None else registry_module.load()
    pair = _resolve_pair(app, reg, what="metadata_push")
    base = _metadata_base(pair, metadata_dir)
    wanted = _stores_arg(store)
    requested = [loc.strip() for loc in locales.split(",") if loc.strip()] or list(pair.locales)
    play_gw = play or PlayGateway()
    apple_gw = apple or AppleGateway()

    plan: list[tuple[Store, str, list[mirror.FieldDiff]]] = []
    problems: list[str] = []
    apple_listing: AppleListing | None = None

    if Store.GOOGLE_PLAY in wanted and pair.play_package:
        down = play_gw.available()
        if down:
            problems.append(f"Google Play: {down}")
        else:
            for locale in requested or mirror.locales_present(base, Store.GOOGLE_PLAY):
                local = mirror.read_locale(base, Store.GOOGLE_PLAY, locale)
                if not local:
                    continue
                try:
                    remote = _play_listing_fields(play_gw.listing(pair.play_package, locale))
                except StorePilotError as exc:
                    problems.append(f"Google Play {locale}: {exc.message}")
                    continue
                plan.append((Store.GOOGLE_PLAY, locale, mirror.diff_fields(
                    Store.GOOGLE_PLAY, local, remote)))

    if Store.APP_STORE in wanted and pair.apple_id:
        down = apple_gw.available()
        if down:
            problems.append(f"App Store: {down}")
        else:
            try:
                apple_listing = apple_gw.listing(pair.apple_id)
            except StorePilotError as exc:
                problems.append(f"App Store: {exc.message}")
            if apple_listing is not None:
                if not apple_listing.editable:
                    problems.append(
                        f"App Store version {apple_listing.version_string} is in state "
                        f"{apple_listing.state} and cannot be edited. Create the next version in "
                        f"App Store Connect first — a live version is frozen on every path, "
                        f"including the web UI."
                    )
                else:
                    for locale in requested or mirror.locales_present(base, Store.APP_STORE):
                        local = mirror.read_locale(base, Store.APP_STORE, locale)
                        if not local:
                            continue
                        remote = apple_listing.fields.get(locale)
                        if remote is None:
                            problems.append(
                                f"App Store has no '{locale}' localization; add the language in "
                                f"App Store Connect first (the API cannot create one)."
                            )
                            continue
                        plan.append(
                            (Store.APP_STORE, locale, mirror.diff_fields(
                                Store.APP_STORE, local, remote))
                        )

    pushable = [
        (store_, locale, [d for d in diffs if d.will_push])
        for store_, locale, diffs in plan
    ]
    pushable = [item for item in pushable if item[2]]
    blocked = [d for _, _, diffs in plan for d in diffs if d.over_by]
    if blocked:
        raise ValidationError(
            f"{len(blocked)} field(s) exceed a store's length limit, so nothing was sent.",
            remedy=(
                "The store would reject the write — on Apple that costs a review cycle. Fix "
                "these files first:\n"
                + "\n".join(f"  - {d.summarize()}" for d in blocked)
            ),
        )
    if not pushable:
        skipped = sum(1 for _, _, diffs in plan for d in diffs if d.status == mirror.UNCHANGED)
        return "\n".join(
            [
                f"Nothing to push for {pair.name} from {base}.",
                (
                    f"{skipped} field(s) already match the stores exactly and were skipped "
                    f"(compared by content digest, not timestamp — a re-checkout or a "
                    f"formatter must not trigger a store write)."
                ),
                *(["", "Problems:", *(f"  ! {p}" for p in problems)] if problems else []),
            ]
        )

    op = Operation(
        tool="metadata_push",
        target=f"{target_for(_PLAY, pair.play_package or '-')}+"
        f"{target_for(_APPLE, pair.apple_id or '-')}",
        params={
            "app_key": pair.key,
            "metadata_dir": str(base),
            "changes": {
                f"{store_.value}:{locale}": {d.field: mirror.digest(d.local) for d in diffs}
                for store_, locale, diffs in pushable
            },
        },
        call_args={
            "app": app or pair.key,
            "store": store,
            "locales": locales,
            "metadata_dir": metadata_dir,
        },
    )

    def build_preview() -> Preview:
        changes: list[Change] = []
        for store_, locale, diffs in pushable:
            for diff in diffs:
                changes.append(
                    Change(f"{_STORE_LABEL[store_]} {locale} {diff.field}", diff.remote, diff.local)
                )
        unchanged = sum(
            1 for _, _, diffs in plan for d in diffs if d.status == mirror.UNCHANGED
        )
        return Preview(
            summary=(
                f"Publish {len(changes)} listing field(s) for {pair.name} from {base} to "
                + " and ".join(sorted({_STORE_TITLE[s] for s, _, _ in pushable}))
            ),
            changes=changes,
            warnings=[
                (
                    "This OVERWRITES live store listing copy. The text under 'before' is what "
                    "users see right now."
                ),
                (
                    "On Google Play a listing edit is submitted for review and CANCELS any "
                    "review currently in flight for the app."
                ),
            ],
            notes=[
                f"{unchanged} field(s) already match and will not be sent.",
                (
                    "App Store copy goes live only when the version is submitted and approved; "
                    "promotional text is the exception and goes live immediately."
                ),
            ],
            reversal="Re-run metadata_push after restoring the previous text from git.",
        )

    gate = require_confirmation(
        op, build_preview, confirm=confirm, confirmation_token=confirmation_token
    )
    if gate is not None:
        return gate

    results: list[str] = []
    with audit_execution(op) as record:
        for store_, locale, diffs in pushable:
            if store_ is Store.GOOGLE_PLAY:
                fields = {
                    _PLAY_LISTING_FIELDS[d.field]: d.local or "" for d in diffs
                }
                with publisher.PlayEdit(
                    pair.play_package or "", label=f"metadata_push {locale}"
                ) as edit:
                    edit.patch_listing(locale, fields)
                results.append(
                    f"[ok] Google Play {locale}: {', '.join(d.field for d in diffs)}"
                )
            elif apple_listing is not None:
                client = apple_gw.client()
                info_changes = {
                    _APPLE_FIELD_TO_ASC[d.field]: d.local or ""
                    for d in diffs
                    if d.field in _APPLE_INFO_FIELDS
                }
                version_changes = {
                    _APPLE_FIELD_TO_ASC[d.field]: d.local or ""
                    for d in diffs
                    if d.field not in _APPLE_INFO_FIELDS
                }
                if version_changes and locale in apple_listing.version_loc_ids:
                    resources.update_version_localization(
                        client, apple_listing.version_loc_ids[locale], version_changes
                    )
                if info_changes and locale in apple_listing.info_loc_ids:
                    resources.update_app_info_localization(
                        client, apple_listing.info_loc_ids[locale], info_changes
                    )
                results.append(f"[ok] App Store {locale}: {', '.join(d.field for d in diffs)}")
        record.set("fields", sum(len(d) for _, _, d in pushable))

    return append_warning(
        "\n".join(
            [
                f"[done] metadata_push — {pair.name}",
                "",
                *results,
                "",
                (
                    "Google Play listing changes are submitted for review. App Store copy is "
                    "saved on the editable version and ships when that version is approved."
                ),
                *(["", "Problems:", *(f"  ! {p}" for p in problems)] if problems else []),
            ]
        )
    )


# --- Registration ------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    """Attach every cross-store tool. Wire this from ``server.py``.

    Registered whenever *either* store is configured: the whole point of these
    tools is that they still answer with one store missing, and a user whose
    Apple credentials are not set up yet should still see their Play portfolio
    through the same table they will keep using afterwards.
    """

    @mcp.tool(name="portfolio_overview", annotations=READ_ONLY)
    def portfolio_overview_tool(
        month: MonthArg = "",
        days: Annotated[
            int,
            Field(
                description=(
                    "Android Vitals window in days; 28 (the default) matches Play Console. "
                    "Applies to Google Play only — Apple publishes no crash rate."
                )
            ),
        ] = 28,
    ) -> str:
        """EVERY app on BOTH stores in one table. Start here for any portfolio question.

        Answers "how is my portfolio doing?" in a single call: live version and
        track, staged-rollout share, rating, installs, revenue with its currency,
        and crash/ANR against Google's thresholds — for every Google Play app and
        every App Store app, joined by the pairing registry.

        Degrades rather than failing: one app's missing permission, or a store
        that is not configured at all, shrinks the table instead of emptying it,
        and every cell that could not be filled shows a reason code with a legend
        underneath. It never prints "ok" for something it did not measure — Apple
        publishes no crash rate, so App Store rows read "no-vitals" — and it never
        adds two currencies together.

        Apps existing on both stores are only joined when the pairing is written
        in ~/.storepilot/apps.toml, since no API states it; run suggest_app_pairs
        then pair_apps. Unpaired apps still appear, one row per store.
        """
        return _boundary(portfolio_overview, month, days)

    @mcp.tool(name="compare_reviews", annotations=READ_ONLY)
    def compare_reviews_tool(
        app: AppKey = "",
        days: Annotated[
            int,
            Field(
                description=(
                    "How far back to keep App Store reviews. It does NOT widen the Play "
                    "side: Google returns roughly the last 7 days whatever this says."
                )
            ),
        ] = 30,
        limit: Annotated[int, Field(description="Maximum reviews per store.")] = 50,
    ) -> str:
        """Reviews for one paired app from BOTH stores, grouped for comparison.

        Returns a rating-distribution table for the two stores side by side and
        then the review texts themselves, labelled by store and grouped by
        rating, so the differences in what users complain about per platform are
        readable directly. The retrieval and structuring happen here; reading the
        sentiment is left to you.

        The samples are NOT comparable as populations, and the output says so:
        Google Play only exposes production-track reviews that carry a comment,
        from roughly the last 7 days.
        """
        return _boundary(compare_reviews, app, days, limit)

    @mcp.tool(name="parity_check", annotations=READ_ONLY)
    def parity_check_tool(
        app: Annotated[
            str,
            Field(
                description=(
                    "Which registered app: apps.toml key, display name, Play package or "
                    "Apple ID. Empty checks every app paired across both stores."
                )
            ),
        ] = "",
        locale: Annotated[
            str,
            Field(
                description=(
                    "Listing locale to compare, spelled as GOOGLE PLAY spells it (e.g. "
                    "'en-US', 'vi', 'zh-TW'). The App Store equivalent is derived, since "
                    "the stores disagree on some codes. Empty uses the app's first "
                    "registered locale, otherwise en-US."
                )
            ),
        ] = "",
    ) -> str:
        """Find drift between a paired app's two store listings.

        Reports DIFFERENCES only: live version, rollout state, and the listing
        fields that exist on both stores (title/name, short description/subtitle,
        full description/description). Where a value would not fit if copied to
        the other store, it says by how much — Play allows 30/80/4000 for
        title/short/full, Apple 30/30/4000/100/170 for name/subtitle/description/
        keywords/promotional text, and a rejected Apple submission costs days.
        """
        return _boundary(parity_check, app, locale)

    @mcp.tool(name="release_both", annotations=DESTRUCTIVE)
    def release_both_tool(
        version_name: Annotated[str, Field(description="User-visible version being shipped, e.g. 3.2.1.")],
        aab_path: Annotated[str, Field(description="Absolute path to the .aab for the Play half. Empty skips the upload.")] = "",
        release_notes: Annotated[str, Field(description="Release notes used on both stores.")] = "",
        play_track: Annotated[str, Field(description="Play track for the Android half. production reaches real users.")] = "internal",
        app: Annotated[str, Field(description="Pair key from apps.toml. Empty uses the only pair, if there is one.")] = "",
        testflight_locale: Annotated[str, Field(description="Locale for the TestFlight What to Test notes.")] = "en-US",
        confirm: Annotated[bool, Field(description="Leave False to get a preview and a confirmation_token. Set True only on the second call, after a human has seen that preview.")] = False,
        confirmation_token: Annotated[str | None, Field(description="The confirmation_token from the preview, passed back unchanged. Bound to the exact arguments previewed and single-use. Never invent one.")] = None,
    ) -> str:
        """Ship one version to Google Play AND Apple TestFlight. TWO-STEP TOOL.

        Call it FIRST with confirm=False. Nothing is published: the Play half runs
        for real inside a throwaway edit that is validated and then discarded, so
        the preview shows the version code Google actually assigned and any error
        Google would raise, and the Apple half is checked against the build really
        sitting in TestFlight. Show that preview to the user, wait for approval,
        then call again with the SAME arguments plus confirm=True and the token.

        ONE token covers BOTH stores. Change either store's parameters and it
        stops working, because approving half a two-store release is not
        approving this release.

        Args:
            version_name: the marketing version both stores show, e.g. "3.2.1".
            aab_path: the .aab to upload to Play. Omit to re-release the build
                already on the track.
            release_notes: published to Play as release notes and to TestFlight
                as "What to Test". Testers and users see this text.
            play_track: defaults to "internal". "production" forces a staged
                rollout capped at 20% on the first step.
            app: registry key of a paired app.
            testflight_locale: locale for the TestFlight notes.

        Apple has NO API that accepts a binary, so the .ipa must already be in
        TestFlight (iTMSTransporter/altool/Xcode). If it is not there, the Apple
        half is reported as SKIPPED with the exact upload command — never as
        success. If Play succeeds and Apple fails, the result says exactly what
        landed where and refuses to call it a success; Play is NOT rolled back,
        because pulling a working build from users over another store's API error
        is the worse outcome.
        """
        return _boundary(
            release_both,
            version_name,
            aab_path,
            release_notes,
            play_track,
            app,
            testflight_locale,
            confirm,
            confirmation_token,
        )

    @mcp.tool(name="metadata_pull", annotations=LOCAL_WRITE)
    def metadata_pull_tool(
        app: AppKey = "",
        store: StoreArg = "both",
        locales: LocalesArg = "",
        metadata_dir: MetadataDirArg = "",
    ) -> str:
        """Download store listing copy into a fastlane-compatible local tree.

        Writes exactly fastlane's layout — metadata/android/<locale>/title.txt,
        short_description.txt, full_description.txt, changelogs/<versionCode>.txt
        for supply, and metadata/ios/<locale>/name.txt, subtitle.txt,
        description.txt, keywords.txt, release_notes.txt for deliver — so the
        same checkout keeps working with fastlane and you can migrate either
        direction. Files whose content already matches are left untouched, so
        git shows only real changes.
        """
        return _boundary(metadata_pull, app, store, locales, metadata_dir)

    @mcp.tool(name="metadata_push", annotations=WRITE)
    def metadata_push_tool(
        # "Empty pushes every registered app" was wrong: an empty `app` resolves
        # to the ONLY registered app and errors when there is more than one. A
        # model that believed the old text would have expected a portfolio-wide
        # publish and got a validation error instead.
        app: Annotated[
            str,
            Field(
                description=(
                    "Which registered app: apps.toml key, display name, Play package or "
                    "Apple ID. Empty works only when exactly one app is registered — this "
                    "tool never pushes to several apps at once."
                )
            ),
        ] = "",
        store: StoreArg = "both",
        locales: Annotated[str, Field(description="Comma-separated locales to push. Empty pushes every locale found on disk.")] = "",
        metadata_dir: MetadataDirArg = "",
        confirm: Annotated[bool, Field(description="Leave False to get a preview and a confirmation_token. Set True only on the second call, after a human has seen that preview.")] = False,
        confirmation_token: Annotated[str | None, Field(description="The confirmation_token from the preview, passed back unchanged. Bound to the exact arguments previewed and single-use. Never invent one.")] = None,
    ) -> str:
        """Publish the local fastlane metadata tree to one or both stores. TWO-STEP TOOL.

        Call with confirm=False first for a real before/after diff of every field
        that would change, show it to the user, then call again with the same
        arguments plus confirm=True and the confirmation_token from the preview.

        Args:
            app: registry key, name, package or Apple ID.
            store: "both" (default), "play" or "ios".
            locales: comma-separated locales; empty means every locale on disk.
            metadata_dir: source tree. Defaults to the registry's metadata_dir.
            confirm: leave False to preview.
            confirmation_token: the token from the preview. Do not construct one.

        Fields whose local content is byte-identical to what the store already
        serves are SKIPPED, compared by content digest rather than file
        timestamp: a git checkout or a formatter must never cause a store write.
        Anything over a store's length limit blocks the whole push before a
        single request goes out, because the store would reject it — and on Apple
        a rejection costs a full review cycle.
        """
        return _boundary(
            metadata_push, app, store, locales, metadata_dir, confirm, confirmation_token
        )

    @mcp.tool(name="list_app_pairs", annotations=READ_ONLY)
    def list_app_pairs_tool() -> str:
        """Show the Play <-> App Store pairing registry that cross-store tools read.

        No store API states that a Play package and an App Store app are the same
        product, so the mapping lives in ~/.storepilot/apps.toml (override with
        STOREPILOT_APPS_FILE). Apps on only one store are legitimate entries and
        still appear in portfolio_overview.
        """
        return _boundary(list_app_pairs)

    @mcp.tool(name="suggest_app_pairs", annotations=READ_ONLY)
    def suggest_app_pairs_tool() -> str:
        """Propose Play <-> App Store pairings from bundle-id and name evidence.

        Reads both stores' app lists and scores every combination, then proposes
        a one-to-one matching with the reasoning for each. NOTHING is applied:
        a proposal is inert until pair_apps writes it to the registry, because a
        wrong pair silently attributes one app's revenue, reviews and crash rate
        to another and nothing downstream would look wrong.

        Use this before hand-writing entries for a portfolio of any size, then
        accept the proposals you agree with.
        """
        return _boundary(suggest_app_pairs)

    @mcp.tool(name="pair_apps", annotations=LOCAL_WRITE)
    def pair_apps_tool(
        key: Annotated[
            str,
            Field(
                description=(
                    "Short registry key for this app, e.g. 'acme-todo'. Derived from the "
                    "name or package when omitted."
                )
            ),
        ] = "",
        play: Annotated[
            str, Field(description="Google Play package name, e.g. 'com.acme.todo'.")
        ] = "",
        appstore: Annotated[
            str,
            Field(
                description=(
                    "Numeric Apple ID, e.g. '1234567890'. NOT the bundle id — that goes in "
                    "bundle_id."
                )
            ),
        ] = "",
        name: Annotated[
            str, Field(description="Display name used in cross-store output.")
        ] = "",
        bundle_id: Annotated[
            str, Field(description="iOS bundle id, which improves future auto-pairing.")
        ] = "",
        metadata_dir: MetadataDirArg = "",
        locales: LocalesArg = "",
    ) -> str:
        """Write one app into the pairing registry, creating or extending its entry.

        Passing only one store id is valid and registers a single-store app.
        Calling this again for the same app extends the existing entry rather
        than creating a duplicate.
        """
        return _boundary(
            pair_apps, key, play, appstore, name, bundle_id, metadata_dir, locales
        )
