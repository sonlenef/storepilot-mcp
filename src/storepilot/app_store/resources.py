"""App Store Connect endpoints, normalized into ``core.models``.

Everything the tools need to *read or change* a resource lives here; ``tools.py``
only formats and guards. Keeping the mapping in one layer matters because a
cross-store agent will join these objects with the Google Play adapter's — an
``App`` or ``Release`` produced here has to be indistinguishable in shape from
one produced there, or ``portfolio_overview`` ends up special-casing per store.

Apple-specific facts encoded below, since they are the ones that silently
produce wrong answers rather than errors:

* ``/v1/apps/{id}`` takes the **numeric Apple ID**, never the bundle id. The
  bundle id is a filter (``filter[bundleId]``). Passing a bundle id as the path
  id yields a 404 that reads exactly like "app does not exist".
* App **name and subtitle** live on ``appInfoLocalizations``, while description,
  keywords, promotional text and release notes live on
  ``appStoreVersionLocalizations``. Writing a name to the version localization
  fails, and the error does not say why.
* Review **territories are ISO 3166-1 alpha-3** ("USA", "GBR"), not the
  two-letter codes every other store uses.
* Apple's phased release is a fixed 7-day percentage ladder; the API exposes only
  the day number, so the fraction has to be derived from it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from storepilot.app_store.client import (
    DOCS_ASC_API,
    AscClient,
    PagedResult,
    attrs,
    resolve_related,
    resource_body,
)
from storepilot.core.errors import NotFoundError, ValidationError
from storepilot.core.guards import untrusted
from storepilot.core.models import (
    App,
    ListingText,
    Release,
    ReleaseStatus,
    Review,
    Store,
)

TRACK_APPSTORE = "appstore"
TRACK_TESTFLIGHT = "testflight"

#: Apple's fixed phased-release ladder. The API reports only ``currentDayNumber``,
#: so the audience share has to be looked up rather than read.
#: UNVERIFIED against a live phased release: these percentages are Apple's
#: published ladder. They are shown to users as "N% of users" in asc_list_versions
#: and portfolio_overview, so a change to the ladder would silently misstate the
#: audience of every in-flight iOS rollout.
PHASED_RELEASE_FRACTIONS: dict[int, float] = {
    1: 0.01,
    2: 0.02,
    3: 0.05,
    4: 0.10,
    5: 0.20,
    6: 0.50,
    7: 1.00,
}

#: Metadata length caps Apple enforces server-side. Violating one returns a 409
#: whose detail names the field but not the limit, so they are checked locally
#: first — a rejected submission costs a review cycle.
METADATA_LIMITS: dict[str, int] = {
    "name": 30,
    "subtitle": 30,
    "keywords": 100,
    "promotional_text": 170,
    "description": 4000,
    "whats_new": 4000,
}

#: Maximum length of a public developer response to a review.
REVIEW_RESPONSE_LIMIT = 5970

#: Which resource each writable metadata field actually lives on.
_APP_INFO_FIELDS = {"name", "subtitle", "privacy_policy_url"}
_VERSION_FIELDS = {
    "description",
    "keywords",
    "promotional_text",
    "whats_new",
    "marketing_url",
    "support_url",
}

_FIELD_TO_APPLE = {
    "name": "name",
    "subtitle": "subtitle",
    "privacy_policy_url": "privacyPolicyUrl",
    "description": "description",
    "keywords": "keywords",
    "promotional_text": "promotionalText",
    "whats_new": "whatsNew",
    "marketing_url": "marketingUrl",
    "support_url": "supportUrl",
}

#: App Store version states that mean "this version is editable".
EDITABLE_STATES = frozenset(
    {
        "PREPARE_FOR_SUBMISSION",
        "DEVELOPER_REJECTED",
        "REJECTED",
        "METADATA_REJECTED",
        "INVALID_BINARY",
    }
)

_STATE_TO_STATUS: dict[str, ReleaseStatus] = {
    "PREPARE_FOR_SUBMISSION": ReleaseStatus.DRAFT,
    "READY_FOR_DISTRIBUTION": ReleaseStatus.COMPLETED,
    "READY_FOR_SALE": ReleaseStatus.COMPLETED,
    "PREORDER_READY_FOR_SALE": ReleaseStatus.COMPLETED,
    "WAITING_FOR_REVIEW": ReleaseStatus.IN_PROGRESS,
    "IN_REVIEW": ReleaseStatus.IN_PROGRESS,
    "PENDING_APPLE_RELEASE": ReleaseStatus.IN_PROGRESS,
    "PENDING_DEVELOPER_RELEASE": ReleaseStatus.IN_PROGRESS,
    "PROCESSING_FOR_APP_STORE": ReleaseStatus.IN_PROGRESS,
    "PROCESSING_FOR_DISTRIBUTION": ReleaseStatus.IN_PROGRESS,
    "WAITING_FOR_EXPORT_COMPLIANCE": ReleaseStatus.IN_PROGRESS,
    "PENDING_CONTRACT": ReleaseStatus.HALTED,
    "REJECTED": ReleaseStatus.HALTED,
    "METADATA_REJECTED": ReleaseStatus.HALTED,
    "DEVELOPER_REJECTED": ReleaseStatus.HALTED,
    "INVALID_BINARY": ReleaseStatus.HALTED,
    "REMOVED_FROM_SALE": ReleaseStatus.HALTED,
    "DEVELOPER_REMOVED_FROM_SALE": ReleaseStatus.HALTED,
    "REPLACED_WITH_NEW_VERSION": ReleaseStatus.COMPLETED,
}


def parse_datetime(value: Any) -> datetime | None:
    """Parse Apple's ISO-8601 timestamps, which carry a literal ``Z``."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def version_state(attributes: Mapping[str, Any]) -> str:
    """Read the version state across Apple's rename.

    ``appStoreState`` was superseded by ``appVersionState``; both still appear
    depending on the account and API version, so neither can be assumed.
    """
    return str(
        attributes.get("appVersionState") or attributes.get("appStoreState") or "UNKNOWN"
    ).upper()


# --- Apps -------------------------------------------------------------------

_APP_FIELDS = "name,bundleId,sku,primaryLocale"


def to_app(resource: Mapping[str, Any]) -> App:
    a = attrs(resource)
    return App(
        store=Store.APP_STORE,
        app_id=str(resource.get("id", "")),
        name=str(a.get("name") or a.get("bundleId") or resource.get("id") or "unknown"),
    )


def list_apps(
    client: AscClient,
    *,
    bundle_id: str | None = None,
    limit: int | None = None,
) -> PagedResult:
    """List apps the API key's team can see. Raw resources, so callers keep bundleId."""
    params: dict[str, Any] = {"fields[apps]": _APP_FIELDS}
    if bundle_id:
        params["filter[bundleId]"] = bundle_id
    return client.get_all(
        "/v1/apps",
        params=params,
        limit=limit,
        context="listing apps",
    )


_NUMERIC = re.compile(r"^\d+$")


def resolve_app_id(client: AscClient, identifier: str) -> str:
    """Accept either a numeric Apple ID or a bundle id and return the Apple ID.

    Users and LLMs reach for the bundle id because it is the identifier they see
    everywhere else (Xcode, Play's package name, the cross-store tools). Silently
    accepting it removes the single most common 404 in this adapter.
    """
    identifier = identifier.strip()
    if not identifier:
        raise ValidationError(
            "No app identifier given.",
            remedy="Pass the numeric Apple ID (e.g. 1234567890) or the bundle id "
            "(e.g. com.example.app). Run asc_list_apps to see both.",
        )
    if _NUMERIC.match(identifier):
        return identifier

    found = list_apps(client, bundle_id=identifier, limit=2)
    if not found.data:
        raise NotFoundError(
            f"No app with bundle id {identifier!r} is visible to this API key.",
            remedy=(
                "Run asc_list_apps to see the exact bundle ids and Apple IDs this key can "
                "reach. If the app is missing entirely, the key belongs to a different team, "
                "or its role does not include the app. Note the app must already exist in App "
                "Store Connect — the API cannot create app records."
            ),
            doc_url=DOCS_ASC_API,
        )
    return str(found.data[0].get("id", ""))


# --- Builds / TestFlight ----------------------------------------------------

_BUILD_INCLUDE = "preReleaseVersion,buildBetaDetail,betaAppReviewSubmission"


def list_builds(
    client: AscClient,
    app_id: str,
    *,
    version: str | None = None,
    processing_state: str | None = None,
    limit: int | None = 25,
) -> PagedResult:
    """Recent TestFlight builds for an app, newest first.

    ``version`` filters on the *marketing* version ("3.2.1"), which lives on the
    related ``preReleaseVersion``, not on the build itself — the build's own
    ``version`` attribute is the build number ("142").
    """
    params: dict[str, Any] = {
        "filter[app]": app_id,
        "include": _BUILD_INCLUDE,
        "sort": "-uploadedDate",
    }
    if version:
        params["filter[preReleaseVersion.version]"] = version
    if processing_state:
        params["filter[processingState]"] = processing_state.upper()
    return client.get_all(
        "/v1/builds",
        params=params,
        limit=limit,
        context=f"listing builds for app {app_id}",
    )


def build_summary(result: PagedResult, resource: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a build plus its sideloaded TestFlight state into one row."""
    a = attrs(resource)
    pre_release = attrs(result.related_one(resource, "preReleaseVersion"))
    beta_detail = attrs(result.related_one(resource, "buildBetaDetail"))
    review = attrs(result.related_one(resource, "betaAppReviewSubmission"))
    return {
        "build_id": resource.get("id"),
        "build_number": a.get("version"),
        "version": pre_release.get("version"),
        "platform": pre_release.get("platform"),
        "processing_state": a.get("processingState"),
        "internal_state": beta_detail.get("internalBuildState"),
        "external_state": beta_detail.get("externalBuildState"),
        "beta_review_state": review.get("betaReviewState"),
        "uploaded_at": parse_datetime(a.get("uploadedDate")),
        "expires_at": parse_datetime(a.get("expirationDate")),
        "expired": bool(a.get("expired")),
        "min_os": a.get("minOsVersion"),
        "uses_non_exempt_encryption": a.get("usesNonExemptEncryption"),
    }


def to_testflight_release(app_id: str, summary: Mapping[str, Any]) -> Release:
    """Normalize a build into the shared ``Release`` shape on the testflight track."""
    state = str(summary.get("processing_state") or "").upper()
    status = {
        "VALID": ReleaseStatus.COMPLETED,
        "PROCESSING": ReleaseStatus.IN_PROGRESS,
        "FAILED": ReleaseStatus.HALTED,
        "INVALID": ReleaseStatus.HALTED,
    }.get(state, ReleaseStatus.UNKNOWN)
    return Release(
        store=Store.APP_STORE,
        app_id=app_id,
        track=TRACK_TESTFLIGHT,
        version_name=summary.get("version"),
        version_codes=[str(summary["build_number"])] if summary.get("build_number") else [],
        status=status,
        released_at=summary.get("uploaded_at"),
    )


# --- App Store versions -----------------------------------------------------

_VERSION_INCLUDE = "appStoreVersionPhasedRelease,build"


def list_versions(
    client: AscClient,
    app_id: str,
    *,
    platform: str = "IOS",
    state: str | None = None,
    limit: int | None = 10,
) -> PagedResult:
    """App Store versions for an app, newest first.

    The relationship endpoint rejects ``sort`` outright (PARAMETER_ERROR.ILLEGAL),
    so ordering is applied client-side after the fetch.
    """
    params: dict[str, Any] = {
        "filter[platform]": platform.upper(),
        "include": _VERSION_INCLUDE,
    }
    if state:
        params["filter[appStoreState]"] = state.upper()
    result = client.get_all(
        f"/v1/apps/{app_id}/appStoreVersions",
        params=params,
        limit=limit,
        context=f"listing App Store versions for app {app_id}",
    )
    result.data.sort(key=lambda v: _version_sort_key(attrs(v).get("versionString")), reverse=True)
    return result


def _version_sort_key(version: Any) -> tuple[int, ...]:
    """Order '1.10.0' after '1.9.0', which a plain string sort gets wrong."""
    parts = str(version or "").split(".")
    key: list[int] = []
    for part in parts:
        digits = "".join(c for c in part if c.isdigit())
        key.append(int(digits) if digits else 0)
    return tuple(key)


def phased_release_fraction(phased: Mapping[str, Any] | None) -> float | None:
    """Audience share of an in-progress phased release, or None when not phased.

    Returns None for a COMPLETE or INACTIVE phased release too: at that point
    everyone has it, and reporting a fraction would make ``is_staged_rollout``
    claim a rollout is still running.
    """
    a = attrs(phased)
    if not a:
        return None
    state = str(a.get("phasedReleaseState") or "").upper()
    if state in ("COMPLETE", "INACTIVE", ""):
        return None
    day = a.get("currentDayNumber")
    try:
        day_number = int(day)
    except (TypeError, ValueError):
        return None
    return PHASED_RELEASE_FRACTIONS.get(max(1, min(7, day_number)))


def to_release(result: PagedResult, app_id: str, resource: Mapping[str, Any]) -> Release:
    a = attrs(resource)
    state = version_state(a)
    phased = result.related_one(resource, "appStoreVersionPhasedRelease")
    fraction = phased_release_fraction(phased)
    status = _STATE_TO_STATUS.get(state, ReleaseStatus.UNKNOWN)
    if fraction is not None and status is ReleaseStatus.COMPLETED:
        # Live, but only to a slice of users — the cross-store view must not
        # show this as a finished rollout next to a Play staged release.
        status = ReleaseStatus.IN_PROGRESS
    # `or ""` rather than a default: Apple sends the key with a null value on a
    # phased release that has not started, and `None.upper()` would take down the
    # whole version listing.
    if str(attrs(phased).get("phasedReleaseState") or "").upper() == "PAUSED":
        status = ReleaseStatus.HALTED

    build = result.related_one(resource, "build")
    build_number = attrs(build).get("version")

    return Release(
        store=Store.APP_STORE,
        app_id=app_id,
        track=TRACK_APPSTORE,
        version_name=a.get("versionString"),
        version_codes=[str(build_number)] if build_number else [],
        status=status,
        user_fraction=fraction,
        released_at=parse_datetime(a.get("createdDate")),
    )


def editable_version(client: AscClient, app_id: str, *, platform: str = "IOS") -> dict[str, Any]:
    """The version that can still be edited, or an error explaining why none can.

    Every metadata write targets this version. Fetching it first turns Apple's
    opaque 409 ("resource not in a valid state") into a message that names the
    actual state and what to do about it.
    """
    versions = list_versions(client, app_id, platform=platform, limit=10)
    for resource in versions.data:
        if version_state(attrs(resource)) in EDITABLE_STATES:
            return dict(resource)

    states = [
        f"{attrs(v).get('versionString', '?')} ({version_state(attrs(v))})" for v in versions.data
    ]
    raise ValidationError(
        f"App {app_id} has no editable {platform} version.",
        remedy=(
            "Metadata can only be changed on a version in PREPARE_FOR_SUBMISSION (or one Apple "
            "rejected). Every current version is already submitted or live. Create the next "
            "version in App Store Connect first — the API can add versions, but a live version "
            "is frozen and editing it is not possible on any path, including the web UI."
        ),
        doc_url=DOCS_ASC_API,
        details={"versions_seen": states or ["none"]},
    )


# --- Localizations / listing text -------------------------------------------


def version_localizations(client: AscClient, version_id: str) -> PagedResult:
    return client.get_all(
        f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
        limit=100,
        context=f"reading localizations for version {version_id}",
    )


def app_info_localizations(client: AscClient, app_id: str) -> tuple[str | None, PagedResult | None]:
    """Name/subtitle localizations, via the app's editable ``appInfo``.

    Returns the appInfo id alongside, because writing a name requires it and
    re-deriving it would cost another request.
    """
    infos = client.get_all(
        f"/v1/apps/{app_id}/appInfos",
        params={"include": "appInfoLocalizations"},
        limit=10,
        context=f"reading app info for app {app_id}",
    )
    for resource in infos.data:
        state = str(
            attrs(resource).get("appStoreState") or attrs(resource).get("state") or ""
        ).upper()
        # The editable appInfo is the one not yet locked to a live version.
        if state in EDITABLE_STATES or not state:
            info_id = str(resource.get("id"))
            return info_id, client.get_all(
                f"/v1/appInfos/{info_id}/appInfoLocalizations",
                limit=100,
                context=f"reading name/subtitle localizations for app {app_id}",
            )
    return None, None


def to_listing_text(
    app_id: str,
    version_localization: Mapping[str, Any],
    info_localization: Mapping[str, Any] | None = None,
) -> ListingText:
    """Merge Apple's two localization resources into the shared listing shape."""
    v = attrs(version_localization)
    i = attrs(info_localization)
    return ListingText(
        store=Store.APP_STORE,
        app_id=app_id,
        locale=str(v.get("locale") or i.get("locale") or "unknown"),
        title=i.get("name"),
        short_description=i.get("subtitle"),
        full_description=v.get("description"),
        keywords=v.get("keywords"),
        video_url=v.get("marketingUrl"),
    )


def validate_metadata(fields: Mapping[str, Any]) -> list[str]:
    """Check listing copy against Apple's caps. Returns human-readable problems.

    Run before every write and before submission. Apple rejects an over-long
    field at submission time, and on a real submission that costs a full review
    cycle — measured in days, not seconds.
    """
    problems: list[str] = []
    for field_name, value in fields.items():
        if value is None:
            continue
        text = str(value)
        limit = METADATA_LIMITS.get(field_name)
        if limit is not None and len(text) > limit:
            problems.append(
                f"{field_name}: {len(text)} characters, Apple's limit is {limit} "
                f"(over by {len(text) - limit})"
            )
        if field_name == "keywords" and ", " in text:
            problems.append(
                "keywords: contains ', ' — Apple counts the space against the 100-character "
                "budget. Use commas with no spaces: 'photo,editor,filter'"
            )
    return problems


def field_to_apple(field_name: str) -> str:
    """StorePilot's field name -> Apple's attribute name (``whats_new`` -> ``whatsNew``)."""
    return _FIELD_TO_APPLE.get(field_name, field_name)


def unknown_metadata_fields(fields: Mapping[str, Any]) -> list[str]:
    known = _APP_INFO_FIELDS | _VERSION_FIELDS
    return [name for name in fields if name not in known]


def split_metadata_fields(
    fields: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split requested changes by the Apple resource that actually owns each one."""
    info: dict[str, Any] = {}
    version: dict[str, Any] = {}
    for name, value in fields.items():
        if value is None:
            continue
        apple_name = _FIELD_TO_APPLE.get(name)
        if apple_name is None:
            continue
        if name in _APP_INFO_FIELDS:
            info[apple_name] = value
        else:
            version[apple_name] = value
    return info, version


def update_version_localization(
    client: AscClient, localization_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    return client.patch(
        f"/v1/appStoreVersionLocalizations/{localization_id}",
        resource_body(
            "appStoreVersionLocalizations", attributes=changes, resource_id=localization_id
        ),
        context=f"updating version localization {localization_id}",
    )


def update_app_info_localization(
    client: AscClient, localization_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    return client.patch(
        f"/v1/appInfoLocalizations/{localization_id}",
        resource_body("appInfoLocalizations", attributes=changes, resource_id=localization_id),
        context=f"updating app info localization {localization_id}",
    )


def find_localization(result: PagedResult, locale: str) -> dict[str, Any] | None:
    wanted = locale.replace("_", "-").lower()
    for resource in result.data:
        if str(attrs(resource).get("locale", "")).replace("_", "-").lower() == wanted:
            return dict(resource)
    return None


def available_locales(result: PagedResult | None) -> list[str]:
    if result is None:
        return []
    return sorted({str(attrs(r).get("locale")) for r in result.data if attrs(r).get("locale")})


# --- Reviews ----------------------------------------------------------------

_REVIEW_INCLUDE = "response"


def list_reviews(
    client: AscClient,
    app_id: str,
    *,
    min_rating: int = 1,
    max_rating: int = 5,
    territory: str | None = None,
    limit: int | None = 25,
    sort: str = "-createdDate",
) -> PagedResult:
    """Customer reviews for an app.

    Apple has no min/max rating filter — only ``filter[rating]`` with an explicit
    set of values — so a range is expanded into that set here.
    """
    if not 1 <= min_rating <= 5 or not 1 <= max_rating <= 5:
        raise ValidationError(
            f"Rating filter out of range: min={min_rating}, max={max_rating}.",
            remedy="Ratings are 1-5. For one-star reviews only, pass min_rating=1, max_rating=1.",
        )
    if min_rating > max_rating:
        raise ValidationError(
            f"min_rating ({min_rating}) is greater than max_rating ({max_rating}).",
            remedy="Swap the arguments, or drop them both to see every rating.",
        )

    params: dict[str, Any] = {"include": _REVIEW_INCLUDE, "sort": sort}
    if (min_rating, max_rating) != (1, 5):
        params["filter[rating]"] = ",".join(str(r) for r in range(min_rating, max_rating + 1))
    if territory:
        params["filter[territory]"] = normalize_territory(territory)

    return client.get_all(
        f"/v1/apps/{app_id}/customerReviews",
        params=params,
        limit=limit,
        context=f"listing reviews for app {app_id}",
    )


#: Two-letter codes callers reach for, mapped to the alpha-3 Apple requires.
#: Not exhaustive by design — anything already three letters passes through, and
#: an unknown two-letter code raises rather than guessing wrong.
_TERRITORY_ALIASES = {
    "US": "USA", "GB": "GBR", "UK": "GBR", "CA": "CAN", "AU": "AUS", "DE": "DEU",
    "FR": "FRA", "JP": "JPN", "CN": "CHN", "KR": "KOR", "IN": "IND", "BR": "BRA",
    "MX": "MEX", "ES": "ESP", "IT": "ITA", "NL": "NLD", "SE": "SWE", "NO": "NOR",
    "DK": "DNK", "FI": "FIN", "PL": "POL", "RU": "RUS", "TR": "TUR", "ID": "IDN",
    "TH": "THA", "VN": "VNM", "PH": "PHL", "MY": "MYS", "SG": "SGP", "TW": "TWN",
    "HK": "HKG", "NZ": "NZL", "IE": "IRL", "CH": "CHE", "AT": "AUT", "BE": "BEL",
    "PT": "PRT", "GR": "GRC", "CZ": "CZE", "ZA": "ZAF", "AE": "ARE", "SA": "SAU",
    "IL": "ISR", "AR": "ARG", "CL": "CHL", "CO": "COL", "PE": "PER", "UA": "UKR",
}


def normalize_territory(territory: str) -> str:
    """Convert a territory code to the alpha-3 form App Store Connect expects."""
    code = territory.strip().upper()
    if len(code) == 3:
        return code
    mapped = _TERRITORY_ALIASES.get(code)
    if mapped:
        return mapped
    raise ValidationError(
        f"Unrecognised territory code {territory!r}.",
        remedy=(
            "App Store Connect uses ISO 3166-1 alpha-3 codes — 'USA' not 'US', 'GBR' not 'GB', "
            "'JPN' not 'JP'. Pass the three-letter code directly if it is not in the alias list."
        ),
    )


def to_review(result: PagedResult, app_id: str, resource: Mapping[str, Any]) -> Review:
    a = attrs(resource)
    response = result.related_one(resource, "response")
    # Title, body and nickname are written by the reviewer. Flatten them here,
    # at the one place a Review is built from Apple's payload, so every consumer
    # (asc_list_reviews, compare_reviews, the reply preview) renders text that
    # cannot begin a line and therefore cannot imitate StorePilot's own output.
    body = untrusted(a.get("body")) or None
    title = untrusted(a.get("title")) or None
    text = f"{title} — {body}" if title and body else (body or title)
    return Review(
        store=Store.APP_STORE,
        app_id=app_id,
        review_id=str(resource.get("id", "")),
        rating=int(a.get("rating") or 0),
        text=text,
        author=untrusted(a.get("reviewerNickname"), limit=60) or None,
        # createdDate, not a modification date: Apple exposes no "edited at" on a
        # customerReview. The shared model calls the field updated_at because the
        # Play side really is a last-modified timestamp, so a cross-store review
        # comparison is comparing "written" against "last edited". Close enough to
        # sort by, not close enough to reason about an edit from.
        updated_at=parse_datetime(a.get("createdDate")),
        has_developer_reply=response is not None,
    )


def review_territory(resource: Mapping[str, Any]) -> str | None:
    return attrs(resource).get("territory")


def get_review(client: AscClient, review_id: str) -> dict[str, Any]:
    """Fetch one review with its existing response, for reply previews."""
    payload = client.get_json(
        f"/v1/customerReviews/{review_id}",
        params={"include": _REVIEW_INCLUDE},
        context=f"reading review {review_id}",
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise NotFoundError(
            f"Review {review_id} not found.",
            remedy=(
                "Review ids come from asc_list_reviews and are opaque strings, not numbers. "
                "Apple also drops reviews when a user deletes them, so an id from an earlier "
                "session may no longer exist — re-run asc_list_reviews."
            ),
        )
    from storepilot.app_store.client import index_included

    included = index_included(payload)
    existing = resolve_related(data, "response", included)
    return {"review": data, "existing_response": existing[0] if existing else None}


def create_review_response(client: AscClient, review_id: str, body: str) -> dict[str, Any]:
    """Post a public developer response to a review."""
    return client.post(
        "/v1/customerReviewResponses",
        resource_body(
            "customerReviewResponses",
            attributes={"responseBody": body},
            relationships={"review": ("customerReviews", review_id)},
        ),
        context=f"replying to review {review_id}",
    )


def update_review_response(client: AscClient, response_id: str, body: str) -> dict[str, Any]:
    """Edit an existing response.

    Apple models an edit as a fresh POST rather than a PATCH in some API
    versions; the caller deletes and recreates when this path is unavailable.
    """
    return client.patch(
        f"/v1/customerReviewResponses/{response_id}",
        resource_body(
            "customerReviewResponses",
            attributes={"responseBody": body},
            resource_id=response_id,
        ),
        context=f"updating review response {response_id}",
    )


def delete_review_response(client: AscClient, response_id: str) -> None:
    """Withdraw a public response."""
    client.delete(
        f"/v1/customerReviewResponses/{response_id}",
        context=f"withdrawing review response {response_id}",
    )


# --- Submission -------------------------------------------------------------


def get_phased_release(client: AscClient, version_id: str) -> dict[str, Any] | None:
    """The version's phased release, or None when it has none.

    A version with no phased release is a normal state, not a failure. Apple is
    inconsistent about how it says so — ``200`` with ``data: null`` on some
    accounts, ``404`` on others — so both are folded into None. Letting the 404
    escape would abort a submission at the phased-release step, after the caller
    already confirmed it.
    """
    try:
        payload = client.get_json(
            f"/v1/appStoreVersions/{version_id}/appStoreVersionPhasedRelease",
            context=f"reading phased release for version {version_id}",
        )
    except NotFoundError:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def enable_phased_release(client: AscClient, version_id: str) -> dict[str, Any]:
    """Turn on Apple's 7-day phased rollout for a version.

    Enabled by default before submission: an unphased release puts a bad build
    in front of the entire install base at once, with no equivalent of Play's
    staged rollout to fall back on.

    UNVERIFIED: creating the resource with ``phasedReleaseState: INACTIVE`` and
    relying on Apple to move it to ACTIVE when the version goes live is
    documented behaviour that has not been confirmed against a live account. If
    a real submission ships to 100% at once despite this call succeeding, this
    is the assumption that broke, and the fix is to PATCH the phased release to
    ACTIVE after approval rather than at creation.
    """
    return client.post(
        "/v1/appStoreVersionPhasedReleases",
        resource_body(
            "appStoreVersionPhasedReleases",
            attributes={"phasedReleaseState": "INACTIVE"},
            relationships={"appStoreVersion": ("appStoreVersions", version_id)},
        ),
        context=f"enabling phased release for version {version_id}",
    )


def set_phased_release_state(client: AscClient, phased_id: str, state: str) -> dict[str, Any]:
    """Pause, resume, or complete a phased rollout.

    ``PAUSED`` is the fire escape: it stops new users receiving the version
    without pulling it from users who already have it.
    """
    return client.patch(
        f"/v1/appStoreVersionPhasedReleases/{phased_id}",
        resource_body(
            "appStoreVersionPhasedReleases",
            attributes={"phasedReleaseState": state.upper()},
            resource_id=phased_id,
        ),
        context=f"setting phased release {phased_id} to {state}",
    )


def create_version_submission(client: AscClient, version_id: str) -> dict[str, Any]:
    """Submit a version for App Review.

    Uses the legacy ``appStoreVersionSubmissions`` resource. Apple's newer
    ``reviewSubmissions`` flow (create a submission, attach items, then flip
    ``submitted``) supersedes it for accounts that have migrated; when this
    returns a 409 naming reviewSubmissions, that is the account this needs.
    """
    return client.post(
        "/v1/appStoreVersionSubmissions",
        resource_body(
            "appStoreVersionSubmissions",
            relationships={"appStoreVersion": ("appStoreVersions", version_id)},
        ),
        context=f"submitting version {version_id} for review",
    )


def version_build(client: AscClient, version_id: str) -> dict[str, Any] | None:
    payload = client.get_json(
        f"/v1/appStoreVersions/{version_id}/build",
        context=f"reading the build attached to version {version_id}",
    )
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def submission_precheck(
    client: AscClient,
    app_id: str,
    version: Mapping[str, Any],
) -> list[str]:
    """Everything that would get this version rejected, found before submitting.

    In the spirit of fastlane's ``precheck``: a rejection costs days, and every
    problem listed here is one Apple would have found for us at that price.
    Returns a list of problems; empty means the version looks submittable.
    """
    problems: list[str] = []
    version_id = str(version.get("id", ""))
    a = attrs(version)
    state = version_state(a)

    if state not in EDITABLE_STATES:
        problems.append(
            f"version {a.get('versionString', '?')} is in state {state}, which cannot be "
            f"submitted (only PREPARE_FOR_SUBMISSION or a rejected version can be)"
        )

    try:
        build = version_build(client, version_id)
    except NotFoundError:
        build = None
    if build is None:
        problems.append(
            "no build is attached to this version — upload one with Transporter or "
            "`xcrun altool` and attach it before submitting"
        )
    else:
        build_state = str(attrs(build).get("processingState") or "").upper()
        if build_state and build_state != "VALID":
            problems.append(
                f"the attached build is in processingState {build_state}; Apple only accepts "
                f"VALID builds (PROCESSING resolves on its own, FAILED/INVALID does not)"
            )

    localizations = version_localizations(client, version_id)
    if not localizations.data:
        problems.append("the version has no localizations — at least one locale is required")

    for resource in localizations.data:
        loc = attrs(resource)
        locale = loc.get("locale", "?")
        if not (loc.get("description") or "").strip():
            problems.append(f"{locale}: description is empty (required)")
        if not (loc.get("whatsNew") or "").strip() and a.get("versionString") not in ("1.0",):
            problems.append(
                f"{locale}: 'What's New' is empty — required for every update after the first "
                f"release"
            )
        for problem in validate_metadata(
            {
                "description": loc.get("description"),
                "keywords": loc.get("keywords"),
                "promotional_text": loc.get("promotionalText"),
                "whats_new": loc.get("whatsNew"),
            }
        ):
            problems.append(f"{locale}: {problem}")

    _, info_locs = app_info_localizations(client, app_id)
    if info_locs is not None:
        for resource in info_locs.data:
            info = attrs(resource)
            locale = info.get("locale", "?")
            for problem in validate_metadata(
                {"name": info.get("name"), "subtitle": info.get("subtitle")}
            ):
                problems.append(f"{locale}: {problem}")
            if not (info.get("privacyPolicyUrl") or "").strip():
                problems.append(
                    f"{locale}: privacy policy URL is empty — Apple requires one for every app"
                )

    return problems


def format_problems(problems: Sequence[str]) -> str:
    return "\n".join(f"  - {p}" for p in problems)
