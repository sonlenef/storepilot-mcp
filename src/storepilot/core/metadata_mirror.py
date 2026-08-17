"""Fastlane-compatible local metadata mirror.

Store listing copy lives in two places that constantly drift: the stores, and
whatever the team keeps in git. This module owns the local half, and it uses
**fastlane's own directory layout** rather than inventing one, so adopting
StorePilot never means abandoning fastlane — the same tree feeds ``supply`` and
``deliver``, and a team can migrate in either direction at any time::

    <base>/metadata/android/<locale>/title.txt            (fastlane supply)
                                     short_description.txt
                                     full_description.txt
                                     video.txt
                                     changelogs/<versionCode>.txt
                                     images/...
    <base>/metadata/ios/<locale>/name.txt                 (fastlane deliver)
                                 subtitle.txt
                                 description.txt
                                 keywords.txt
                                 promotional_text.txt
                                 release_notes.txt
                                 marketing_url.txt / support_url.txt / privacy_url.txt
    <base>/metadata/ios/review_information/first_name.txt ...

One deliberate deviation, called out because it is the only one: fastlane
``deliver`` defaults to ``fastlane/metadata/<locale>`` with no platform segment,
because it only ever handled Apple. Putting Apple under ``metadata/ios`` is what
lets both stores share one tree; existing fastlane users keep working with a
one-line change, ``deliver(metadata_path: "metadata/ios")``. Filenames inside the
locale directory are byte-for-byte fastlane's.

Three rules this module enforces, each of them a bug someone has shipped:

1. **Skip unchanged content.** A push compares the digest of the local file with
   the digest of what the store currently serves and sends nothing when they
   match. This is fastlane's ``sync_image_upload`` lesson generalised: re-uploading
   identical text is not free — on Play it can push an app back into review, and
   on Apple it dirties a version that was ready to submit.
2. **Length limits belong to the target store, not to the field.** The same
   sentence is a legal Play short description (80) and an illegal Apple subtitle
   (30). Copying across stores without checking is how a sync tool produces a
   rejected submission.
3. **Locale codes disagree between the stores** and the disagreements are not
   guessable (Play still ships Hebrew as the pre-1989 ``iw``; Apple wants script
   subtags for Chinese where Play wants regions). The mapping is a table, and an
   unmapped locale is reported rather than silently approximated.

Everything here is pure filesystem plus hashing: no network, no store adapter
imports, so it is testable offline and reusable by any tool.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from storepilot.core.errors import ValidationError
from storepilot.core.models import Store

__all__ = [
    "APPLE_FIELDS",
    "CHANGELOGS_DIRNAME",
    "IMAGES_DIRNAME",
    "METADATA_DIRNAME",
    "PARITY_PAIRS",
    "PLAY_FIELDS",
    "REVIEW_INFO_FIELDS",
    "STATE_FILENAME",
    "FieldDiff",
    "FieldSpec",
    "FileWrite",
    "changelog_path",
    "counterpart_field",
    "diff_fields",
    "digest",
    "field_specs",
    "images_dir",
    "load_state",
    "locale_dir",
    "locales_present",
    "map_locale",
    "metadata_root",
    "over_limit",
    "read_changelog",
    "read_locale",
    "read_review_information",
    "record_state",
    "review_information_dir",
    "save_state",
    "spec_for",
    "store_root",
    "unmapped_locale_note",
    "write_changelog",
    "write_locale",
]

METADATA_DIRNAME = "metadata"
CHANGELOGS_DIRNAME = "changelogs"
IMAGES_DIRNAME = "images"
REVIEW_INFO_DIRNAME = "review_information"

#: Sidecar written next to the metadata tree recording the digest of every file
#: at the moment it was pulled. It is what makes "you edited this since the last
#: pull" answerable without a network call; it is never authoritative for a push,
#: where the store's current text is the only thing worth comparing against.
STATE_FILENAME = ".storepilot-metadata.json"

#: fastlane's platform directory names.
STORE_DIRNAME: dict[Store, str] = {
    Store.GOOGLE_PLAY: "android",
    Store.APP_STORE: "ios",
}


@dataclass(frozen=True)
class FieldSpec:
    """One listing field: where it lives on disk and what the store allows.

    ``limit`` is the store's own server-side cap. ``counterpart`` names the field
    that carries the same meaning on the other store, which is what makes a
    cross-store diff (and a cross-store length check) possible at all.
    """

    field: str
    filename: str
    label: str
    limit: int | None = None
    counterpart: str | None = None
    note: str | None = None

    def path(self, base: Path, store: Store, locale: str) -> Path:
        return locale_dir(base, store, locale) / self.filename


# --- Google Play (fastlane supply) ------------------------------------------

PLAY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("title", "title.txt", "Title", 30, counterpart="name"),
    FieldSpec(
        "short_description",
        "short_description.txt",
        "Short description",
        80,
        counterpart="subtitle",
        note="Play allows 80 characters here; Apple's subtitle, the same idea, allows 30.",
    ),
    FieldSpec(
        "full_description",
        "full_description.txt",
        "Full description",
        4000,
        counterpart="description",
    ),
    FieldSpec("video_url", "video.txt", "Promo video URL", None, counterpart="marketing_url"),
)

# --- App Store Connect (fastlane deliver) -----------------------------------

APPLE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "name.txt", "App name", 30, counterpart="title"),
    FieldSpec(
        "subtitle",
        "subtitle.txt",
        "Subtitle",
        30,
        counterpart="short_description",
        note="Apple caps the subtitle at 30 characters — Play's short description allows 80.",
    ),
    FieldSpec(
        "description", "description.txt", "Description", 4000, counterpart="full_description"
    ),
    FieldSpec(
        "keywords",
        "keywords.txt",
        "Keywords",
        100,
        note=(
            "Apple only. Comma-separated with NO space after the comma — the space counts "
            "against the 100-character budget. Google Play has no keyword field at all; "
            "discovery there comes from the title and descriptions."
        ),
    ),
    FieldSpec(
        "promotional_text",
        "promotional_text.txt",
        "Promotional text",
        170,
        note="Apple only. The single field that can change without submitting a new version.",
    ),
    FieldSpec(
        "whats_new",
        "release_notes.txt",
        "What's New",
        4000,
        note="Play's equivalent is changelogs/<versionCode>.txt, keyed by build.",
    ),
    FieldSpec("marketing_url", "marketing_url.txt", "Marketing URL", None, counterpart="video_url"),
    FieldSpec("support_url", "support_url.txt", "Support URL", None),
    FieldSpec("privacy_url", "privacy_url.txt", "Privacy policy URL", None),
)

#: fastlane deliver's App Review contact sheet. Written and read here so the tree
#: stays a complete fastlane tree; StorePilot does not yet push these to Apple
#: (``appStoreReviewDetail`` is not wired into the App Store adapter), and the
#: tools say so rather than pretending the files are synced.
REVIEW_INFO_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("first_name", "first_name.txt", "Review contact first name"),
    FieldSpec("last_name", "last_name.txt", "Review contact last name"),
    FieldSpec("phone_number", "phone_number.txt", "Review contact phone"),
    FieldSpec("email_address", "email_address.txt", "Review contact email"),
    FieldSpec("demo_user", "demo_user.txt", "Demo account username"),
    FieldSpec("demo_password", "demo_password.txt", "Demo account password"),
    FieldSpec("notes", "notes.txt", "Notes for the reviewer"),
)

#: Play field <-> Apple field, for parity checks and cross-store copies.
PARITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("title", "name"),
    ("short_description", "subtitle"),
    ("full_description", "description"),
    ("video_url", "marketing_url"),
)

_SPECS: dict[Store, tuple[FieldSpec, ...]] = {
    Store.GOOGLE_PLAY: PLAY_FIELDS,
    Store.APP_STORE: APPLE_FIELDS,
}


def field_specs(store: Store) -> tuple[FieldSpec, ...]:
    return _SPECS[store]


def spec_for(store: Store, field_name: str) -> FieldSpec | None:
    return next((s for s in _SPECS[store] if s.field == field_name), None)


def counterpart_field(store: Store, field_name: str) -> str | None:
    """The field on the *other* store carrying the same meaning, if any."""
    spec = spec_for(store, field_name)
    return spec.counterpart if spec else None


def over_limit(store: Store, field_name: str, value: str | None) -> int:
    """Characters by which ``value`` exceeds the store's cap for the field (0 if fine)."""
    spec = spec_for(store, field_name)
    if spec is None or spec.limit is None or value is None:
        return 0
    return max(0, len(value) - spec.limit)


# --- Locale mapping ----------------------------------------------------------
#
# Both stores mostly speak BCP-47, and the overlap is large enough that a naive
# pass-through works for perhaps 80% of locales — which is exactly what makes the
# remaining 20% dangerous, because a wrong locale does not error, it publishes
# text in a language nobody asked for. Every entry below is a real disagreement.

#: Play locale -> Apple locale, for the cases where they differ.
_PLAY_TO_APPLE: dict[str, str] = {
    "zh-CN": "zh-Hans",
    "zh-TW": "zh-Hant",
    "zh-HK": "zh-Hant",
    "es-419": "es-MX",
    "es-ES": "es-ES",
    "es-US": "es-MX",
    "iw-IL": "he",  # Play still ships Hebrew under the pre-1989 ISO code.
    "in-ID": "id",  # ...and Indonesian under the pre-1989 code too.
    "ar": "ar-SA",
    "cs-CZ": "cs",
    "da-DK": "da",
    "el-GR": "el",
    "fi-FI": "fi",
    "hi-IN": "hi",
    "hr": "hr",
    "hu-HU": "hu",
    "it-IT": "it",
    "ja-JP": "ja",
    "ko-KR": "ko",
    "ms": "ms",
    "no-NO": "no",
    "pl-PL": "pl",
    "ro": "ro",
    "ru-RU": "ru",
    "sk": "sk",
    "sv-SE": "sv",
    "th": "th",
    "tr-TR": "tr",
    "uk": "uk",
    "vi": "vi",
    "ca": "ca",
    "de-DE": "de-DE",
    "fr-FR": "fr-FR",
    "fr-CA": "fr-CA",
    "nl-NL": "nl-NL",
    "pt-BR": "pt-BR",
    "pt-PT": "pt-PT",
    "en-US": "en-US",
    "en-GB": "en-GB",
    "en-AU": "en-AU",
    "en-CA": "en-CA",
}

#: Apple locale -> Play locale. Built from the table above, then corrected where
#: the relation is genuinely not one-to-one (Play splits zh-TW/zh-HK where Apple
#: has one zh-Hant; Apple has no en-IN where Play does).
_APPLE_TO_PLAY: dict[str, str] = {apple: play for play, apple in _PLAY_TO_APPLE.items()}
_APPLE_TO_PLAY.update(
    {
        "zh-Hant": "zh-TW",
        "es-MX": "es-419",
        "he": "iw-IL",
        "id": "in-ID",
    }
)

#: Locales one store offers and the other simply does not. Reported, never mapped.
_NO_COUNTERPART: dict[Store, dict[str, str]] = {
    Store.GOOGLE_PLAY: {
        "en-IN": "Apple has no en-IN storefront locale; en-GB or en-US is the usual target.",
        "fil": "Apple does not offer Filipino as an App Store localization.",
        "am": "Apple does not offer Amharic as an App Store localization.",
    },
    Store.APP_STORE: {
        "ar-SA": "",  # mapped; present for completeness of the lookup path
    },
}


def map_locale(locale: str, *, source: Store, target: Store) -> str | None:
    """Translate a locale code between stores. ``None`` when there is no counterpart.

    Falls back to the exact code when both stores use it, then to the bare
    language subtag — but only when the target store plausibly accepts it, which
    is why the explicit table exists rather than a generic normalizer.
    """
    if source is target:
        return locale
    code = locale.strip().replace("_", "-")
    table = _PLAY_TO_APPLE if source is Store.GOOGLE_PLAY else _APPLE_TO_PLAY
    if code in table:
        return table[code] or None
    # Case-insensitive retry: "zh-hant" is the same locale as "zh-Hant".
    lowered = {k.lower(): v for k, v in table.items()}
    if code.lower() in lowered:
        return lowered[code.lower()] or None
    if code in _NO_COUNTERPART.get(source, {}):
        return None
    reverse = _APPLE_TO_PLAY if source is Store.GOOGLE_PLAY else _PLAY_TO_APPLE
    if code in reverse.values():
        return code  # both stores spell it the same way
    language = code.split("-")[0]
    if language and language != code:
        for candidate in table.values():
            if candidate.split("-")[0] == language:
                return candidate
    return None


def unmapped_locale_note(locale: str, *, source: Store, target: Store) -> str:
    """Why a locale could not be translated, phrased as something to act on."""
    reason = _NO_COUNTERPART.get(source, {}).get(locale.strip())
    other = "App Store Connect" if target is Store.APP_STORE else "Google Play"
    if reason:
        return f"{locale}: {reason}"
    return (
        f"{locale}: no known {other} equivalent. The two stores do not use identical locale "
        f"codes (Play 'zh-TW' is Apple 'zh-Hant', Play 'iw-IL' is Apple 'he'). Add the locale "
        f"in {other} first, then name it explicitly."
    )


# --- Paths -------------------------------------------------------------------


def metadata_root(base: str | Path) -> Path:
    return Path(base).expanduser() / METADATA_DIRNAME


def store_root(base: str | Path, store: Store) -> Path:
    return metadata_root(base) / STORE_DIRNAME[store]


_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def _safe_locale(locale: str) -> str:
    code = locale.strip().replace("_", "-")
    if not code or not _LOCALE_RE.match(code):
        raise ValidationError(
            f"{locale!r} is not a locale code.",
            remedy=(
                "Pass a BCP-47 code as the store spells it: Play uses 'en-US', 'vi', 'zh-TW'; "
                "Apple uses 'en-US', 'vi', 'zh-Hant'. It becomes a directory name, so a path "
                "fragment is rejected here rather than escaping the metadata tree."
            ),
        )
    return code


def locale_dir(base: str | Path, store: Store, locale: str) -> Path:
    return store_root(base, store) / _safe_locale(locale)


def changelog_path(base: str | Path, locale: str, version_code: str | int) -> Path:
    """``metadata/android/<locale>/changelogs/<versionCode>.txt`` — supply's layout."""
    code = str(version_code).strip()
    if not code.isdigit():
        raise ValidationError(
            f"Play changelogs are keyed by numeric version code, got {version_code!r}.",
            remedy=(
                "Use the integer versionCode of the build (e.g. 4501), not the version name "
                "'3.2.1'. fastlane supply reads changelogs/<versionCode>.txt."
            ),
        )
    return locale_dir(base, Store.GOOGLE_PLAY, locale) / CHANGELOGS_DIRNAME / f"{code}.txt"


def images_dir(base: str | Path, locale: str) -> Path:
    """Play image directory. StorePilot does not sync images (no adapter uploads
    them yet); the path exists so a fastlane tree round-trips unchanged."""
    return locale_dir(base, Store.GOOGLE_PLAY, locale) / IMAGES_DIRNAME


def review_information_dir(base: str | Path) -> Path:
    return store_root(base, Store.APP_STORE) / REVIEW_INFO_DIRNAME


def locales_present(base: str | Path, store: Store) -> list[str]:
    """Locale directories already on disk for a store, sorted."""
    root = store_root(base, store)
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and p.name != REVIEW_INFO_DIRNAME and not p.name.startswith(".")
    )


# --- Reading / writing -------------------------------------------------------


def digest(text: str | None) -> str:
    """Content digest used for every skip-unchanged decision.

    Trailing whitespace is stripped first: a text editor that adds a final
    newline must not make a file look modified, or every push would rewrite
    every field forever.
    """
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValidationError(
            f"Cannot read {path.name} in {path.parent.name}: {exc.strerror or exc}.",
            remedy="Check the file's permissions, then re-run.",
        ) from exc


def read_locale(base: str | Path, store: Store, locale: str) -> dict[str, str]:
    """Every field present on disk for one locale. Absent files are simply absent.

    Absent is deliberately not the same as empty: a missing ``keywords.txt``
    means "not managed here", while an empty one means "publish nothing", and
    conflating them would silently wipe a field on the next push.
    """
    out: dict[str, str] = {}
    for spec in field_specs(store):
        text = _read_text(spec.path(Path(base), store, locale))
        if text is not None:
            out[spec.field] = text
    return out


def read_changelog(base: str | Path, locale: str, version_code: str | int) -> str | None:
    return _read_text(changelog_path(base, locale, version_code))


def read_review_information(base: str | Path) -> dict[str, str]:
    directory = review_information_dir(base)
    out: dict[str, str] = {}
    for spec in REVIEW_INFO_FIELDS:
        text = _read_text(directory / spec.filename)
        if text is not None:
            out[spec.field] = text
    return out


@dataclass
class FileWrite:
    """One file the mirror touched, or deliberately did not."""

    path: Path
    field: str
    status: str  # "written" | "unchanged" | "created"
    digest: str
    chars: int = 0

    @property
    def changed(self) -> bool:
        return self.status != "unchanged"


def _write_file(path: Path, text: str, field_name: str) -> FileWrite:
    existing = _read_text(path)
    new_digest = digest(text)
    if existing is not None and digest(existing) == new_digest:
        return FileWrite(path, field_name, "unchanged", new_digest, len(text))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Trailing newline: fastlane writes one, and git diffs are unreadable
        # without it.
        path.write_text(text.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise ValidationError(
            f"Cannot write {path}: {exc.strerror or exc}.",
            remedy=(
                "Check the directory is writable. Pass a different metadata directory, or set "
                "metadata_dir on the app in ~/.storepilot/apps.toml."
            ),
        ) from exc
    return FileWrite(
        path, field_name, "created" if existing is None else "written", new_digest, len(text)
    )


def write_locale(
    base: str | Path,
    store: Store,
    locale: str,
    values: dict[str, str | None],
) -> list[FileWrite]:
    """Write the fields of one locale, skipping files whose content is identical.

    ``None`` values are skipped entirely rather than written as an empty file:
    the store not returning a field is not the same as the store returning "".
    """
    writes: list[FileWrite] = []
    for spec in field_specs(store):
        value = values.get(spec.field)
        if value is None:
            continue
        writes.append(_write_file(spec.path(Path(base), store, locale), str(value), spec.field))
    return writes


def write_changelog(
    base: str | Path, locale: str, version_code: str | int, text: str
) -> FileWrite:
    return _write_file(changelog_path(base, locale, version_code), text, "changelog")


# --- Diffing -----------------------------------------------------------------

#: A push decides per field, and these are the only outcomes.
CHANGED = "changed"
UNCHANGED = "unchanged"
NEW = "new"  # store has nothing; the file creates it
ABSENT = "absent"  # nothing on disk; the store's value is left alone


@dataclass
class FieldDiff:
    """One field's local-vs-store comparison, and whether it is publishable."""

    field: str
    label: str
    local: str | None
    remote: str | None
    status: str
    limit: int | None = None
    over_by: int = 0
    note: str | None = None

    @property
    def will_push(self) -> bool:
        return self.status in (CHANGED, NEW) and self.over_by == 0

    def summarize(self) -> str:
        if self.status == UNCHANGED:
            return f"{self.label}: unchanged ({len(self.local or '')} chars) — not sent"
        if self.status == ABSENT:
            return f"{self.label}: no local file — store value left untouched"
        if self.over_by:
            return (
                f"{self.label}: {len(self.local or '')} chars, {self.over_by} over the "
                f"{self.limit}-character limit — BLOCKED"
            )
        verb = "creates" if self.status == NEW else "replaces"
        return f"{self.label}: {verb} the store value ({len(self.local or '')} chars)"


def diff_fields(
    store: Store,
    local: dict[str, str],
    remote: dict[str, str | None],
    *,
    fields: list[str] | None = None,
) -> list[FieldDiff]:
    """Compare local files against what the store currently serves.

    The comparison is on content digests, not timestamps: a file touched by a
    formatter, a checkout, or a ``git clone`` has a new mtime and identical
    bytes, and re-pushing it would be a real, user-visible change for no reason.
    """
    out: list[FieldDiff] = []
    for spec in field_specs(store):
        if fields is not None and spec.field not in fields:
            continue
        local_value = local.get(spec.field)
        remote_value = remote.get(spec.field)
        if local_value is None:
            status = ABSENT
        elif remote_value is None or not str(remote_value).strip():
            status = NEW
        elif digest(local_value) == digest(str(remote_value)):
            status = UNCHANGED
        else:
            status = CHANGED
        out.append(
            FieldDiff(
                field=spec.field,
                label=spec.label,
                local=local_value,
                remote=str(remote_value) if remote_value is not None else None,
                status=status,
                limit=spec.limit,
                over_by=over_limit(store, spec.field, local_value),
                note=spec.note,
            )
        )
    return out


# --- Pull-time state ---------------------------------------------------------


@dataclass
class MirrorState:
    """Digests recorded at the last pull, per store/locale/field."""

    pulled_at: str | None = None
    entries: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def key(store: Store, locale: str, field_name: str) -> str:
        return f"{STORE_DIRNAME[store]}/{locale}/{field_name}"

    def locally_edited(self, store: Store, locale: str, field_name: str, text: str | None) -> bool:
        """True when the file differs from what the last pull wrote.

        Answers "did I change this, or did the store?" without a network call —
        the question a push preview has to answer before overwriting a store.
        """
        recorded = self.entries.get(self.key(store, locale, field_name))
        return recorded is not None and recorded != digest(text)


def _state_path(base: str | Path) -> Path:
    return metadata_root(base) / STATE_FILENAME


def load_state(base: str | Path) -> MirrorState:
    """Read the sidecar. A missing or corrupt file degrades to "no state known"."""
    try:
        raw = json.loads(_state_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MirrorState()
    if not isinstance(raw, dict):
        return MirrorState()
    entries = raw.get("entries")
    return MirrorState(
        pulled_at=raw.get("pulled_at"),
        entries={str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {},
    )


def record_state(state: MirrorState, store: Store, locale: str, writes: list[FileWrite]) -> None:
    for write in writes:
        state.entries[MirrorState.key(store, locale, write.field)] = write.digest


def save_state(base: str | Path, state: MirrorState) -> None:
    """Persist the sidecar. Never fatal — losing it only costs the edit detection."""
    state.pulled_at = datetime.now(UTC).isoformat(timespec="seconds")
    path = _state_path(base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pulled_at": state.pulled_at, "entries": state.entries}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return
