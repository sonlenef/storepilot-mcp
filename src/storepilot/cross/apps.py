"""The app-pairing registry: which Play package is which App Store app.

Nothing in either API answers this. Google Play knows ``com.acme.todo``; App
Store Connect knows Apple ID ``1234567890`` and bundle id ``com.acme.todo``; no
endpoint on either side says they are the same product. Every cross-store
question — one portfolio table, one review comparison, one release — depends on
that mapping existing somewhere, so it lives in a small file the user owns:

    ~/.storepilot/apps.toml          (override with STOREPILOT_APPS_FILE)

    [apps.acme-todo]
    name = "Acme Todo"
    play = "com.acme.todo"
    appstore = "1234567890"
    bundle_id = "com.acme.todo"
    metadata_dir = "~/code/acme-todo/fastlane"
    locales = ["en-US", "vi"]

Three design decisions, each of them load-bearing:

**Auto-pairing proposes, it never decides.** With thirty apps nobody hand-writes
thirty entries, so :func:`propose` scores every Play app against every App Store
app on bundle-id and name evidence. But a wrong pair silently attributes one
app's revenue, reviews and crash rate to another, and the user would have no way
to notice — so a proposal is inert until it is written to the file. The file is
the trust boundary; the heuristic only drafts it.

**Single-store apps are first-class.** A Play-only app is not an error and not a
half-pair: it is an app, and it appears in the portfolio with its App Store cells
marked "not on this store". A cross-store tool that hides everything it cannot
pair would hide most portfolios.

**A registry entry that no longer resolves is reported, not dropped.** If
``apps.toml`` names a package the credentials cannot see, that is either a typo
or a permissions gap, and both need saying out loud — quietly skipping the row
turns a broken config into a missing app.
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storepilot.core.errors import ValidationError
from storepilot.core.guards import state_dir
from storepilot.core.models import App, Store

__all__ = [
    "AppPair",
    "Proposal",
    "Registry",
    "StoreApp",
    "build_portfolio",
    "default_metadata_dir",
    "load",
    "propose",
    "registry_path",
    "render_registry_template",
    "save",
    "slugify",
    "upsert",
]

#: Confidence floor for showing a proposal at all. Below this the evidence is so
#: thin that reading the suggestion costs more than typing the entry by hand.
MIN_PROPOSAL_SCORE = 0.60

#: At or above this the two apps agree on both bundle id and name.
HIGH_CONFIDENCE = 0.90
LIKELY_CONFIDENCE = 0.75


def registry_path() -> Path:
    """``~/.storepilot/apps.toml``, or ``STOREPILOT_APPS_FILE`` when set.

    Falls in behind ``STOREPILOT_STATE_DIR`` so a sandboxed or per-project
    install keeps its registry, guard key and audit log together.
    """
    raw = os.environ.get("STOREPILOT_APPS_FILE")
    if raw:
        return Path(raw).expanduser()
    return state_dir() / "apps.toml"


# --- Models ------------------------------------------------------------------


@dataclass(frozen=True)
class StoreApp:
    """One app as a store reports it, plus the bundle id when the store has one.

    Google Play's package name *is* the bundle id; App Store Connect keeps them
    separate (numeric Apple ID for the API, bundle id for humans), which is
    precisely the asymmetry that makes pairing necessary.
    """

    store: Store
    app_id: str
    name: str
    bundle_id: str | None = None

    @classmethod
    def from_app(cls, app: App, bundle_id: str | None = None) -> StoreApp:
        return cls(
            store=app.store,
            app_id=app.app_id,
            name=app.name,
            bundle_id=bundle_id or (app.app_id if app.store is Store.GOOGLE_PLAY else None),
        )


@dataclass(frozen=True)
class AppPair:
    """One product, on one or both stores.

    ``source`` is ``registry`` when the pairing was written down, ``unpaired``
    when the app was discovered on exactly one store and nothing claims it.
    """

    key: str
    name: str
    play_package: str | None = None
    apple_id: str | None = None
    bundle_id: str | None = None
    metadata_dir: str | None = None
    locales: tuple[str, ...] = ()
    source: str = "registry"
    notes: tuple[str, ...] = ()

    @property
    def is_paired(self) -> bool:
        return bool(self.play_package and self.apple_id)

    @property
    def stores(self) -> tuple[Store, ...]:
        out: list[Store] = []
        if self.play_package:
            out.append(Store.GOOGLE_PLAY)
        if self.apple_id:
            out.append(Store.APP_STORE)
        return tuple(out)

    def app_id(self, store: Store) -> str | None:
        return self.play_package if store is Store.GOOGLE_PLAY else self.apple_id

    def has(self, store: Store) -> bool:
        return self.app_id(store) is not None

    def describe(self) -> str:
        parts = []
        if self.play_package:
            parts.append(f"play={self.play_package}")
        if self.apple_id:
            parts.append(f"appstore={self.apple_id}")
        return f"{self.name} [{', '.join(parts) or 'no store ids'}]"

    def matches(self, query: str) -> bool:
        """True when ``query`` names this app by key, name, package or Apple ID."""
        q = query.strip().lower()
        if not q:
            return False
        candidates = {
            self.key.lower(),
            self.name.lower(),
            (self.play_package or "").lower(),
            (self.apple_id or "").lower(),
            (self.bundle_id or "").lower(),
        }
        candidates.discard("")
        return q in candidates or any(c.startswith(q) for c in candidates)


@dataclass
class Registry:
    """Parsed ``apps.toml`` plus everything that was wrong with it."""

    path: Path
    pairs: list[AppPair] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exists: bool = False

    def get(self, key: str) -> AppPair | None:
        return next((p for p in self.pairs if p.key == key), None)

    def find(self, query: str) -> list[AppPair]:
        return [p for p in self.pairs if p.matches(query)]

    def claimed_play(self) -> set[str]:
        return {p.play_package for p in self.pairs if p.play_package}

    def claimed_apple(self) -> set[str]:
        return {p.apple_id for p in self.pairs if p.apple_id}


# --- Loading -----------------------------------------------------------------

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def slugify(value: str, *, fallback: str = "app") -> str:
    """A stable, hand-typeable registry key derived from a name or package."""
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return text[:48] or fallback


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_entry(key: str, raw: Any, warnings: list[str]) -> AppPair | None:
    if not isinstance(raw, dict):
        warnings.append(f"[apps.{key}] is not a table and was ignored.")
        return None
    if not _KEY_RE.match(key):
        warnings.append(
            f"[apps.{key}] has an unusable key; use lowercase letters, digits, '-' or '_'."
        )
        return None

    play = _as_str(raw.get("play") or raw.get("play_package") or raw.get("package"))
    apple = _as_str(raw.get("appstore") or raw.get("apple_id") or raw.get("app_store"))
    if apple and not apple.isdigit():
        warnings.append(
            f"[apps.{key}] appstore = {apple!r} is not a numeric Apple ID. App Store Connect "
            f"identifies apps by the numeric Apple ID (e.g. 1234567890); the bundle id belongs "
            f"in bundle_id."
        )
    if not play and not apple:
        warnings.append(f"[apps.{key}] names neither a play package nor an appstore id; ignored.")
        return None

    locales_raw = raw.get("locales")
    locales: tuple[str, ...] = ()
    if isinstance(locales_raw, list):
        locales = tuple(str(item).strip() for item in locales_raw if str(item).strip())
    elif locales_raw is not None:
        warnings.append(f"[apps.{key}] locales must be a list, e.g. locales = [\"en-US\"].")

    return AppPair(
        key=key,
        name=_as_str(raw.get("name")) or play or apple or key,
        play_package=play,
        apple_id=apple,
        bundle_id=_as_str(raw.get("bundle_id")),
        metadata_dir=_as_str(raw.get("metadata_dir")),
        locales=locales,
        source="registry",
    )


def load(path: Path | None = None) -> Registry:
    """Read the registry. A missing file is a normal, empty state — not an error."""
    target = path or registry_path()
    registry = Registry(path=target)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return registry
    except OSError as exc:
        registry.warnings.append(f"Cannot read {target}: {exc.strerror or exc}")
        return registry

    registry.exists = True
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(
            f"{target} is not valid TOML: {exc}",
            remedy=(
                "Fix the syntax, or delete the file and re-create it with pair_apps. Each app is "
                'a table: [apps.acme-todo] with play = "com.acme.todo" and '
                'appstore = "1234567890".'
            ),
        ) from exc

    apps = data.get("apps")
    if apps is None:
        # Tolerate a flat file of tables, since that is what a person writes by
        # hand before reading the docs.
        apps = {k: v for k, v in data.items() if isinstance(v, dict)}
        if apps:
            registry.warnings.append(
                "Entries were found at the top level; the documented form nests them under "
                "[apps.<key>]. Both are accepted."
            )
    if not isinstance(apps, dict):
        raise ValidationError(
            f"{target}: 'apps' must be a table of app entries.",
            remedy='Use [apps.acme-todo] sections, one per app.',
        )

    seen_play: dict[str, str] = {}
    seen_apple: dict[str, str] = {}
    for key, raw_entry in apps.items():
        pair = _parse_entry(str(key), raw_entry, registry.warnings)
        if pair is None:
            continue
        if pair.play_package and pair.play_package in seen_play:
            registry.warnings.append(
                f"{pair.play_package} is claimed by both [apps.{seen_play[pair.play_package]}] "
                f"and [apps.{pair.key}]; the first wins."
            )
            continue
        if pair.apple_id and pair.apple_id in seen_apple:
            registry.warnings.append(
                f"Apple ID {pair.apple_id} is claimed by both [apps.{seen_apple[pair.apple_id]}] "
                f"and [apps.{pair.key}]; the first wins."
            )
            continue
        if pair.play_package:
            seen_play[pair.play_package] = pair.key
        if pair.apple_id:
            seen_apple[pair.apple_id] = pair.key
        registry.pairs.append(pair)

    registry.pairs.sort(key=lambda p: p.name.lower())
    return registry


# --- Writing -----------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _toml_str(value: str) -> str:
    return '"' + "".join(_ESCAPES.get(ch, ch) for ch in value) + '"'


def _render_pair(pair: AppPair) -> list[str]:
    lines = [f"[apps.{pair.key}]", f"name = {_toml_str(pair.name)}"]
    if pair.play_package:
        lines.append(f"play = {_toml_str(pair.play_package)}")
    if pair.apple_id:
        lines.append(f"appstore = {_toml_str(pair.apple_id)}")
    if pair.bundle_id:
        lines.append(f"bundle_id = {_toml_str(pair.bundle_id)}")
    if pair.metadata_dir:
        lines.append(f"metadata_dir = {_toml_str(pair.metadata_dir)}")
    if pair.locales:
        joined = ", ".join(_toml_str(loc) for loc in pair.locales)
        lines.append(f"locales = [{joined}]")
    return lines


HEADER = """\
# StorePilot app registry.
#
# Neither store's API can tell you that a Play package and an App Store app are
# the same product, so the pairing lives here. Written by the pair_apps tool and
# safe to edit by hand.
#
#   [apps.acme-todo]
#   name         = "Acme Todo"       # shown in portfolio_overview
#   play         = "com.acme.todo"   # Play package name; omit for iOS-only apps
#   appstore     = "1234567890"      # numeric Apple ID; omit for Android-only apps
#   bundle_id    = "com.acme.todo"   # optional, helps auto-pairing
#   metadata_dir = "~/code/todo"     # optional; holds metadata/android + metadata/ios
#   locales      = ["en-US", "vi"]   # optional; defaults to every locale the store has
"""


def render_registry_template(pairs: list[AppPair]) -> str:
    """The full file text for a set of pairs, header included."""
    body: list[str] = [HEADER]
    for pair in sorted(pairs, key=lambda p: p.key):
        body.append("")
        body.extend(_render_pair(pair))
    return "\n".join(body).rstrip() + "\n"


def save(pairs: list[AppPair], path: Path | None = None) -> Path:
    """Rewrite the registry atomically. Returns the path written."""
    target = path or registry_path()
    text = render_registry_template(pairs)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise ValidationError(
            f"Cannot write the app registry at {target}: {exc.strerror or exc}",
            remedy=(
                "Check the directory is writable, or point STOREPILOT_APPS_FILE somewhere it is."
            ),
        ) from exc
    return target


def upsert(registry: Registry, pair: AppPair) -> tuple[list[AppPair], str]:
    """Merge ``pair`` into the registry's list. Returns the new list and what changed.

    Merging rather than appending is what makes ``pair_apps`` safe to call twice:
    adding the App Store side to an app already registered on Play must extend
    that entry, not create a second one claiming the same package.
    """
    pairs = list(registry.pairs)
    for index, existing in enumerate(pairs):
        same_key = existing.key == pair.key
        same_play = bool(pair.play_package) and existing.play_package == pair.play_package
        same_apple = bool(pair.apple_id) and existing.apple_id == pair.apple_id
        if not (same_key or same_play or same_apple):
            continue
        merged = AppPair(
            key=existing.key,
            name=pair.name or existing.name,
            play_package=pair.play_package or existing.play_package,
            apple_id=pair.apple_id or existing.apple_id,
            bundle_id=pair.bundle_id or existing.bundle_id,
            metadata_dir=pair.metadata_dir or existing.metadata_dir,
            locales=pair.locales or existing.locales,
        )
        pairs[index] = merged
        if merged == existing:
            return pairs, f"[apps.{merged.key}] already said exactly this; nothing changed."
        return pairs, f"updated [apps.{merged.key}] -> {merged.describe()}"

    pairs.append(pair)
    return pairs, f"added [apps.{pair.key}] -> {pair.describe()}"


# --- Portfolio assembly ------------------------------------------------------


def build_portfolio(
    play_apps: list[StoreApp],
    apple_apps: list[StoreApp],
    registry: Registry,
    *,
    unavailable: set[Store] | None = None,
) -> tuple[list[AppPair], list[str]]:
    """Every app worth a portfolio row, paired where the registry says so.

    Order of precedence, and the reason for each:

    1. **Registry entries win.** They are the only trustworthy statement that two
       app ids are one product.
    2. **Registry entries whose ids are not visible still appear**, carrying a
       note. Dropping them would turn a typo or a missing permission into an app
       that simply vanished from the portfolio.
    3. **Everything else is a single-store app**, included on its own row. This
       is the common case for a portfolio mid-migration and must not look like a
       failure.

    ``unavailable`` names stores that could not be reached at all. Their apps are
    still listed from the registry, but without the "this account cannot see it"
    note — an unconfigured store is not a missing permission, and saying so would
    send the user to fix the wrong thing.
    """
    down = unavailable or set()
    warnings: list[str] = []
    play_by_id = {a.app_id: a for a in play_apps}
    apple_by_id = {a.app_id: a for a in apple_apps}
    used_play: set[str] = set()
    used_apple: set[str] = set()
    out: list[AppPair] = []

    for pair in registry.pairs:
        notes: list[str] = []
        name = pair.name
        if pair.play_package:
            live = play_by_id.get(pair.play_package)
            if live is None:
                if Store.GOOGLE_PLAY not in down:
                    notes.append(
                        f"registry names Play package {pair.play_package}, which this Play "
                        f"account cannot see (wrong package name, no access granted, or no "
                        f"published release)"
                    )
            else:
                used_play.add(pair.play_package)
                if name in (pair.play_package, pair.key):
                    name = live.name
        if pair.apple_id:
            live_apple = apple_by_id.get(pair.apple_id)
            if live_apple is None:
                if Store.APP_STORE not in down:
                    notes.append(
                        f"registry names Apple ID {pair.apple_id}, which this App Store Connect "
                        f"key cannot see (wrong ID, or the key belongs to another team)"
                    )
            else:
                used_apple.add(pair.apple_id)
                if name in (pair.apple_id, pair.key):
                    name = live_apple.name
        out.append(
            AppPair(
                key=pair.key,
                name=name,
                play_package=pair.play_package,
                apple_id=pair.apple_id,
                bundle_id=pair.bundle_id,
                metadata_dir=pair.metadata_dir,
                locales=pair.locales,
                source="registry",
                notes=tuple(notes),
            )
        )
        warnings.extend(f"[apps.{pair.key}]: {note}" for note in notes)

    taken_keys = {p.key for p in out}

    def unique_key(base: str) -> str:
        key = base
        suffix = 2
        while key in taken_keys:
            key = f"{base}-{suffix}"
            suffix += 1
        taken_keys.add(key)
        return key

    for app in play_apps:
        if app.app_id in used_play:
            continue
        out.append(
            AppPair(
                key=unique_key(slugify(app.name) or slugify(app.app_id.rsplit(".", 1)[-1])),
                name=app.name,
                play_package=app.app_id,
                bundle_id=app.app_id,
                source="unpaired",
            )
        )
    for app in apple_apps:
        if app.app_id in used_apple:
            continue
        out.append(
            AppPair(
                key=unique_key(slugify(app.name) or f"apple-{app.app_id}"),
                name=app.name,
                apple_id=app.app_id,
                bundle_id=app.bundle_id,
                source="unpaired",
            )
        )

    out.sort(key=lambda p: (p.name.lower(), p.key))
    return out, warnings


# --- Auto-pairing ------------------------------------------------------------

_TRADEMARKS = str.maketrans({"™": "", "®": "", "©": "", "’": "'"})

#: Suffixes people append to one store's listing but not the other's. Removing
#: them before comparing is what lets "Acme Todo" match "Acme Todo: Task List".
_NAME_TAIL_RE = re.compile(r"\s*[:–—\-|]\s+.*$")
_PLATFORM_WORDS = re.compile(
    r"\b(for\s+(ios|iphone|ipad|android)|ios|android|mobile\s+app)\b", re.IGNORECASE
)

#: Bundle-id suffixes teams add on one platform only.
_ID_PLATFORM_SUFFIXES = ("ios", "android", "apple", "google", "app", "mobile")


def _normalize_name(name: str) -> str:
    text = name.translate(_TRADEMARKS)
    text = _NAME_TAIL_RE.sub("", text)
    text = _PLATFORM_WORDS.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _id_tokens(identifier: str) -> list[str]:
    return [t for t in re.split(r"[.\-_]+", identifier.strip().lower()) if t]


def _bundle_score(play_package: str, apple_bundle: str | None) -> tuple[float, str]:
    """How strongly two identifiers claim to be the same product."""
    if not apple_bundle:
        return 0.0, ""
    left, right = play_package.strip().lower(), apple_bundle.strip().lower()
    if left == right:
        return 1.0, "identical bundle id"

    lt, rt = _id_tokens(left), _id_tokens(right)
    # A platform suffix on exactly one side: com.acme.todo vs com.acme.todo.ios
    if lt and rt:
        lt_trim = lt[:-1] if lt[-1] in _ID_PLATFORM_SUFFIXES and len(lt) > 2 else lt
        rt_trim = rt[:-1] if rt[-1] in _ID_PLATFORM_SUFFIXES and len(rt) > 2 else rt
        if lt_trim == rt_trim:
            return 0.92, "bundle ids match apart from a platform suffix"
        if len(lt_trim) >= 2 and len(rt_trim) >= 2:
            if lt_trim[:2] == rt_trim[:2] and lt_trim[-1] == rt_trim[-1]:
                return 0.8, "same reverse-domain owner and same final segment"
            if lt_trim[-1] == rt_trim[-1] and len(lt_trim[-1]) > 3:
                return 0.65, f"same final bundle segment '{lt_trim[-1]}'"
            if lt_trim[:2] == rt_trim[:2]:
                return 0.55, "same reverse-domain owner"
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if ratio > 0.85:
        return round(ratio * 0.8, 3), f"bundle ids {ratio:.0%} similar"
    return 0.0, ""


def _name_score(play_name: str, apple_name: str) -> tuple[float, str]:
    left, right = _normalize_name(play_name), _normalize_name(apple_name)
    if not left or not right:
        return 0.0, ""
    if left == right:
        return 1.0, "identical name"
    if left.startswith(right) or right.startswith(left):
        shorter = min(len(left), len(right))
        longer = max(len(left), len(right))
        return round(0.6 + 0.3 * (shorter / longer), 3), "one name is a prefix of the other"
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if ratio >= 0.7:
        return round(ratio * 0.9, 3), f"names {ratio:.0%} similar"
    return 0.0, ""


@dataclass(frozen=True)
class Proposal:
    """A pairing the heuristic believes in, and exactly why.

    Inert until written to ``apps.toml``: the reasons exist so a human can reject
    it in one glance, which is the only check standing between a bad guess and a
    portfolio that mis-attributes revenue.
    """

    play: StoreApp
    apple: StoreApp
    score: float
    reasons: tuple[str, ...]
    key: str

    @property
    def confidence(self) -> str:
        if self.score >= HIGH_CONFIDENCE:
            return "high"
        if self.score >= LIKELY_CONFIDENCE:
            return "likely"
        return "possible"


def propose(
    play_apps: list[StoreApp],
    apple_apps: list[StoreApp],
    registry: Registry | None = None,
    *,
    minimum: float = MIN_PROPOSAL_SCORE,
) -> tuple[list[Proposal], list[StoreApp], list[StoreApp]]:
    """Score every Play app against every App Store app and pick the best matches.

    Returns ``(proposals, unmatched_play, unmatched_apple)``. Assignment is greedy
    one-to-one on descending score: one Play app cannot be proposed against two
    Apple apps, because the failure it would cause — two rows silently sharing one
    app's revenue — is worse than leaving a pair for the user to write by hand.
    """
    claimed_play = registry.claimed_play() if registry else set()
    claimed_apple = registry.claimed_apple() if registry else set()
    candidates_play = [a for a in play_apps if a.app_id not in claimed_play]
    candidates_apple = [a for a in apple_apps if a.app_id not in claimed_apple]

    scored: list[tuple[float, tuple[str, ...], StoreApp, StoreApp]] = []
    for play in candidates_play:
        for apple in candidates_apple:
            bundle, bundle_why = _bundle_score(play.app_id, apple.bundle_id)
            name, name_why = _name_score(play.name, apple.name)
            # Agreement between two independent signals is worth more than either
            # alone, but neither can be manufactured by the other.
            score = round(min(1.0, max(bundle, name) + 0.1 * min(bundle, name)), 3)
            if score < minimum:
                continue
            reasons = tuple(r for r in (bundle_why, name_why) if r)
            scored.append((score, reasons, play, apple))

    scored.sort(key=lambda item: (-item[0], item[2].name.lower()))
    used_play: set[str] = set()
    used_apple: set[str] = set()
    proposals: list[Proposal] = []
    keys: set[str] = {p.key for p in (registry.pairs if registry else [])}

    for score, reasons, play, apple in scored:
        if play.app_id in used_play or apple.app_id in used_apple:
            continue
        used_play.add(play.app_id)
        used_apple.add(apple.app_id)
        base = slugify(play.name) or slugify(play.app_id.rsplit(".", 1)[-1])
        key, suffix = base, 2
        while key in keys:
            key = f"{base}-{suffix}"
            suffix += 1
        keys.add(key)
        proposals.append(
            Proposal(play=play, apple=apple, score=score, reasons=reasons, key=key)
        )

    unmatched_play = [a for a in candidates_play if a.app_id not in used_play]
    unmatched_apple = [a for a in candidates_apple if a.app_id not in used_apple]
    return proposals, unmatched_play, unmatched_apple


# --- Metadata directory ------------------------------------------------------


def default_metadata_dir(pair: AppPair) -> Path:
    """Where this app's fastlane tree lives when the registry does not say.

    Not the working directory: an MCP server's cwd is whatever the client
    happened to launch it from, so defaulting there would scatter metadata trees
    across unrelated projects. ``metadata_dir`` in ``apps.toml`` is the way to
    point at a real repository checkout.
    """
    if pair.metadata_dir:
        return Path(pair.metadata_dir).expanduser()
    return state_dir() / "metadata" / pair.key
