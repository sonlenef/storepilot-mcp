"""The Android Publisher edits lifecycle, wrapped so it cannot half-apply.

Every Play write happens inside a transaction:

    edits.insert  ->  mutate  ->  edits.validate  ->  edits.commit

Nothing is live until ``commit``. An edit that is never committed expires on its
own after roughly seven days and changes nothing, which is what makes a truthful
preview possible: :class:`PlayEdit` in ``dry_run`` mode runs the whole sequence
including ``validate`` — a real server-side check with real version codes and
real rejections — and then deliberately ``delete``s the edit instead of
committing. The preview a user approves is therefore the operation Google
already agreed to perform, not our guess at it.

Two rules this module never breaks:

* ``validate`` always runs before ``commit``.
* On any exception the edit is deleted, and the original exception propagates
  untouched. A swallowed error here would leave the user believing a release
  shipped when it did not.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from storepilot.core.errors import (
    DOCS_PLAY_API_ACCESS,
    NotFoundError,
    StorePilotError,
    ValidationError,
    redact_path,
)
from storepilot.google_play.auth import classify_google_error, publisher_client

__all__ = [
    "TRACK_ORDER",
    "AabFile",
    "BundleInfo",
    "PlayEdit",
    "current_release",
    "inspect_aab",
    "list_track_releases",
    "read_listing",
    "read_track",
    "replace_release",
]

#: Play's built-in tracks ordered from least to most exposed. Used only to warn
#: about promotions that skip stages or go backwards; custom closed-testing
#: tracks are not in this list and simply carry no ordering.
TRACK_ORDER = ("internal", "alpha", "beta", "production")

_UPLOAD_CHUNK = 8 * 1024 * 1024


def _track_rank(track: str) -> int | None:
    name = track.strip().lower()
    return TRACK_ORDER.index(name) if name in TRACK_ORDER else None


# --- AAB inspection ----------------------------------------------------------


@dataclass(frozen=True)
class AabFile:
    """A local Android App Bundle that has passed structural checks."""

    path: Path
    size_bytes: int
    sha256: str

    @property
    def display_path(self) -> str:
        return redact_path(self.path)

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 1)


def inspect_aab(aab_path: str | Path) -> AabFile:
    """Verify a file really is an AAB before anything is uploaded anywhere.

    Uploading the wrong file is a slow, expensive way to find out it was the
    wrong file, and an APK named ``.aab`` fails with a Google error that does not
    say so. The cheap local checks are worth their milliseconds.
    """
    path = Path(aab_path).expanduser()
    if not path.exists():
        raise NotFoundError(
            f"No file at {redact_path(path)}.",
            remedy=(
                "Pass the absolute path to the .aab produced by your build "
                "(Flutter: build/app/outputs/bundle/release/app-release.aab; "
                "Gradle: app/build/outputs/bundle/release/app-release.aab)."
            ),
        )
    if path.is_dir():
        raise ValidationError(
            f"{redact_path(path)} is a directory, not an app bundle.",
            remedy="Pass the path to the .aab file itself.",
        )
    size = path.stat().st_size
    if size == 0:
        raise ValidationError(
            f"{redact_path(path)} is empty (0 bytes).",
            remedy="The build most likely failed or was interrupted. Rebuild and retry.",
        )
    if path.suffix.lower() == ".apk":
        raise ValidationError(
            f"{redact_path(path)} is an APK, not an Android App Bundle.",
            remedy=(
                "Google Play requires an .aab for new releases. Build one with "
                "'flutter build appbundle' or './gradlew bundleRelease'."
            ),
            doc_url=DOCS_PLAY_API_ACCESS,
        )
    if not zipfile.is_zipfile(path):
        raise ValidationError(
            f"{redact_path(path)} is not a valid app bundle (not a zip archive).",
            remedy="The file is corrupt or truncated. Rebuild it and check the download/copy.",
        )
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError(
            f"Cannot read {redact_path(path)} as an app bundle.",
            remedy="The archive is damaged. Rebuild it.",
            details={"zip_error": str(exc)},
        ) from exc

    if "BundleConfig.pb" not in names:
        hint = (
            "it looks like a signed APK"
            if "AndroidManifest.xml" in names
            else "it is a zip file of some other kind"
        )
        raise ValidationError(
            f"{redact_path(path)} does not contain BundleConfig.pb, so it is not an AAB — {hint}.",
            remedy="Build an app bundle ('flutter build appbundle' / './gradlew bundleRelease').",
        )

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return AabFile(path=path, size_bytes=size, sha256=digest.hexdigest())


@dataclass(frozen=True)
class BundleInfo:
    """What Google says about a bundle after it was accepted into an edit."""

    version_code: int
    sha256: str | None = None
    sha1: str | None = None

    def __str__(self) -> str:
        return f"version code {self.version_code}"


# --- Edit-free reads ---------------------------------------------------------


def _execute(request: Any, *, context: str, package_name: str | None = None) -> Any:
    try:
        return request.execute()
    except Exception as exc:
        raise classify_google_error(exc, context=context, package_name=package_name) from exc


def list_track_releases(package_name: str, track: str) -> list[dict[str, Any]]:
    """Release summaries for a track, read WITHOUT opening an edit.

    Previews use this for "what is live right now"; opening an edit just to look
    would be a mutation-shaped operation for a read.
    """
    client = publisher_client()
    parent = f"applications/{package_name}/tracks/{track}"
    response = _execute(
        client.applications().tracks().releases().list(parent=parent),
        context=f"listing releases on track '{track}'",
        package_name=package_name,
    )
    return response.get("releases", []) or []


def read_track(package_name: str, track: str) -> dict[str, Any]:
    """The full track resource (releases with versionCodes/status/userFraction).

    ``edits.tracks.get`` needs an edit, so this opens a throwaway one and deletes
    it. That is harmless — an uncommitted edit changes nothing — and it is the
    only way to see ``userFraction``, which the edit-free summary omits.
    """
    with PlayEdit(package_name, dry_run=True, label=f"read track {track}") as edit:
        return edit.get_track(track)


def read_listing(package_name: str, locale: str) -> dict[str, Any]:
    """The current store listing for one locale, via a throwaway edit."""
    with PlayEdit(package_name, dry_run=True, label=f"read listing {locale}") as edit:
        return edit.get_listing(locale)


def current_release(track_body: dict[str, Any]) -> dict[str, Any] | None:
    """The release a user would consider "the live one" on this track.

    Play returns every active release on a track. The one that matters is the
    one being served: ``inProgress`` first (a staged rollout in flight), then
    ``completed``, then ``halted``, then ``draft``.
    """
    releases = track_body.get("releases", []) or []
    order = {"inProgress": 0, "completed": 1, "halted": 2, "draft": 3}
    ranked = sorted(releases, key=lambda r: order.get(str(r.get("status")), 9))
    return ranked[0] if ranked else None


# --- The transaction ---------------------------------------------------------


class PlayEdit:
    """Context manager around one Android Publisher edit.

    ::

        with PlayEdit("com.acme.app") as edit:            # edits.insert
            info = edit.upload_bundle(path)
            edit.set_release("internal", [info.version_code], status="completed")
        # __exit__: edits.validate then edits.commit

        with PlayEdit("com.acme.app", dry_run=True) as edit:
            ...                                            # same calls
        # __exit__: edits.validate then edits.DELETE — nothing is published

    On any exception the edit is deleted and the exception propagates unchanged.
    """

    def __init__(
        self,
        package_name: str,
        *,
        dry_run: bool = False,
        changes_not_sent_for_review: bool = False,
        label: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.package_name = package_name
        self.dry_run = dry_run
        self.changes_not_sent_for_review = changes_not_sent_for_review
        self.label = label or ("dry run" if dry_run else "write")
        self._client = client
        self.id: str | None = None
        self.expiry_epoch: int | None = None
        self.operations: list[str] = []
        self.validated = False
        self.committed = False
        self.commit_result: dict[str, Any] | None = None
        self.discarded = False
        self.cleanup_error: str | None = None
        self._closed = False

    # -- plumbing --

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = publisher_client()
        return self._client

    @property
    def edits(self) -> Any:
        return self.client.edits()

    def _require_open(self) -> str:
        if self.id is None:
            raise ValidationError(
                "PlayEdit used outside its 'with' block — no edit is open.",
                remedy="Perform every mutation inside 'with PlayEdit(package) as edit:'.",
            )
        return self.id

    def _run(self, request: Any, *, context: str) -> Any:
        return _execute(request, context=context, package_name=self.package_name)

    # -- lifecycle --

    def __enter__(self) -> Self:
        body = self._run(
            self.edits.insert(packageName=self.package_name, body={}),
            context=f"opening an edit ({self.label})",
        )
        self.id = body.get("id")
        if not self.id:
            raise ValidationError(
                "Google accepted edits.insert but returned no edit id.",
                remedy="Retry once; if it persists the Android Publisher API is misbehaving.",
                details={"response": str(body)[:400]},
            )
        raw_expiry = body.get("expiryTimeSeconds")
        self.expiry_epoch = int(raw_expiry) if raw_expiry else None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if self._closed:
            return False
        self._closed = True
        if self.id is None:
            return False

        if exc_type is not None:
            # Something failed mid-transaction. Discard so nothing partial can
            # ever be committed, and let the original error through untouched.
            self._safe_delete()
            return False

        try:
            self.validate()
            if self.dry_run:
                self._safe_delete()
            else:
                self.commit_result = self.commit()
                self.committed = True
        except BaseException:
            self._safe_delete()
            raise
        return False

    def validate(self) -> dict[str, Any]:
        """``edits.validate`` — Google checks the whole edit before anything ships."""
        edit_id = self._require_open()
        result = self._run(
            self.edits.validate(packageName=self.package_name, editId=edit_id),
            context=f"validating edit {edit_id}",
        )
        self.validated = True
        return result

    def commit(self) -> dict[str, Any]:
        """``edits.commit`` — the point of no return.

        ``changes_not_sent_for_review=True`` publishes the edit but leaves the
        changes queued rather than submitting them for review. Some changes (a
        store listing edit on an app under review, anything touching a declaration
        form) must be sent for review by hand from the Play Console; committing
        them normally would cancel a review already in flight.
        """
        edit_id = self._require_open()
        kwargs: dict[str, Any] = {"packageName": self.package_name, "editId": edit_id}
        if self.changes_not_sent_for_review:
            kwargs["changesNotSentForReview"] = True
        return self._run(
            self.edits.commit(**kwargs),
            context=f"committing edit {edit_id}",
        )

    def delete(self) -> None:
        """``edits.delete`` — abandon the transaction; nothing was published."""
        edit_id = self._require_open()
        self._run(
            self.edits.delete(packageName=self.package_name, editId=edit_id),
            context=f"discarding edit {edit_id}",
        )
        self.discarded = True

    def discard(self) -> None:
        """Abandon the edit now and make ``__exit__`` a no-op.

        For the "there is nothing to do after all" branch: bailing out without
        this would still run ``validate``, which can surface an unrelated
        pre-existing problem and turn a clean "nothing to halt" answer into an
        error during an incident.
        """
        if self._closed or self.id is None:
            return
        self._closed = True
        self._safe_delete()

    def _safe_delete(self) -> None:
        """Best-effort discard. Never masks the error that got us here."""
        try:
            self.delete()
        except StorePilotError as err:
            self.cleanup_error = err.message
        except Exception as err:  # noqa: BLE001 - cleanup must not raise
            self.cleanup_error = f"{type(err).__name__}: {err}"

    # -- mutations --

    def upload_bundle(self, aab: AabFile | str | Path) -> BundleInfo:
        """Upload an AAB into this edit and return the version code Google assigned.

        The artifact belongs to the edit: if the edit is discarded, so is the
        upload, and the version code is not consumed. That is what lets a preview
        upload for real and still change nothing.
        """
        from googleapiclient.http import MediaFileUpload

        edit_id = self._require_open()
        bundle = aab if isinstance(aab, AabFile) else inspect_aab(aab)
        media = MediaFileUpload(
            str(bundle.path),
            mimetype="application/octet-stream",
            chunksize=_UPLOAD_CHUNK,
            resumable=True,
        )
        request = self.edits.bundles().upload(
            packageName=self.package_name,
            editId=edit_id,
            media_body=media,
        )
        context = f"uploading {bundle.display_path} ({bundle.size_mb} MB) to edit {edit_id}"
        try:
            response = None
            while response is None:
                _status, response = request.next_chunk()
        except Exception as exc:
            raise classify_google_error(
                exc, context=context, package_name=self.package_name
            ) from exc

        version_code = response.get("versionCode")
        if version_code is None:
            raise ValidationError(
                "Google accepted the bundle but returned no version code.",
                remedy="Retry the upload. If it repeats, upload once via the Play Console UI.",
                details={"response": str(response)[:400]},
            )
        info = BundleInfo(
            version_code=int(version_code),
            sha256=response.get("sha256"),
            sha1=response.get("sha1"),
        )
        self.operations.append(f"uploaded {bundle.display_path} as version code {info.version_code}")
        return info

    def upload_apk(self, apk_path: str | Path) -> BundleInfo:
        """Upload a legacy APK. Only for apps predating the AAB requirement."""
        from googleapiclient.http import MediaFileUpload

        edit_id = self._require_open()
        path = Path(apk_path).expanduser()
        if not path.exists():
            raise NotFoundError(
                f"No file at {redact_path(path)}.",
                remedy="Pass the absolute path to the signed .apk.",
            )
        media = MediaFileUpload(
            str(path),
            mimetype="application/vnd.android.package-archive",
            chunksize=_UPLOAD_CHUNK,
            resumable=True,
        )
        request = self.edits.apks().upload(
            packageName=self.package_name, editId=edit_id, media_body=media
        )
        try:
            response = None
            while response is None:
                _status, response = request.next_chunk()
        except Exception as exc:
            raise classify_google_error(
                exc,
                context=f"uploading {redact_path(path)} to edit {edit_id}",
                package_name=self.package_name,
            ) from exc
        self.operations.append(f"uploaded APK {redact_path(path)}")
        return BundleInfo(version_code=int(response["versionCode"]))

    def get_track(self, track: str) -> dict[str, Any]:
        edit_id = self._require_open()
        return self._run(
            self.edits.tracks().get(
                packageName=self.package_name, editId=edit_id, track=track
            ),
            context=f"reading track '{track}'",
        )

    def update_track(self, track: str, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace the track's release list. ``releases`` is the desired end state."""
        edit_id = self._require_open()
        result = self._run(
            self.edits.tracks().update(
                packageName=self.package_name,
                editId=edit_id,
                track=track,
                body={"track": track, "releases": releases},
            ),
            context=f"updating track '{track}'",
        )
        self.operations.append(f"set track '{track}' to {len(releases)} release(s)")
        return result

    def set_release(
        self,
        track: str,
        version_codes: list[str] | list[int],
        *,
        status: str = "completed",
        user_fraction: float | None = None,
        release_notes: dict[str, str] | None = None,
        name: str | None = None,
        in_app_update_priority: int | None = None,
    ) -> dict[str, Any]:
        """Make ``version_codes`` the single active release on ``track``.

        ``userFraction`` is only legal alongside ``inProgress``/``halted``; the
        API rejects the combination rather than ignoring it, so it is dropped
        here for the other statuses instead of being passed through.
        """
        release = build_release(
            version_codes,
            status=status,
            user_fraction=user_fraction,
            release_notes=release_notes,
            name=name,
            in_app_update_priority=in_app_update_priority,
        )
        return self.update_track(track, [release])

    def get_listing(self, locale: str) -> dict[str, Any]:
        edit_id = self._require_open()
        try:
            return self._run(
                self.edits.listings().get(
                    packageName=self.package_name, editId=edit_id, language=locale
                ),
                context=f"reading the '{locale}' store listing",
            )
        except NotFoundError:
            # A locale with no listing yet is a legitimate state, not an error:
            # the caller is about to create it.
            return {"language": locale}

    def patch_listing(self, locale: str, fields: dict[str, Any]) -> dict[str, Any]:
        """PATCH, never PUT.

        ``listings.update`` replaces the whole listing, so omitting a field wipes
        it. Callers here only ever supply the fields they mean to change, so a
        partial update is the only correct verb.
        """
        edit_id = self._require_open()
        body = {"language": locale, **fields}
        result = self._run(
            self.edits.listings().patch(
                packageName=self.package_name, editId=edit_id, language=locale, body=body
            ),
            context=f"updating the '{locale}' store listing",
        )
        self.operations.append(f"patched '{locale}' listing: {', '.join(sorted(fields))}")
        return result


def replace_release(
    track_body: dict[str, Any],
    target: dict[str, Any],
    replacement: dict[str, Any],
) -> list[dict[str, Any]]:
    """The track's full release list with ``target`` swapped for ``replacement``.

    ``edits.tracks.update`` takes the desired END STATE of the track: any release
    missing from the list is removed from the track. That matters because a
    production track in mid-rollout holds TWO releases — the new build
    ``inProgress`` at some fraction, and the previous build ``completed``,
    serving everyone else. Writing back only the release being changed silently
    unassigns the build that most users are actually on.

    So an operation that modifies ONE release (halt it, widen it) must send the
    others back untouched. An operation that genuinely replaces what the track
    serves — create, promote — uses :meth:`PlayEdit.set_release` instead, and its
    preview says so.

    Matching is by version codes, which is what identifies a release on a track;
    if the target is not found the replacement is appended rather than dropped.
    """
    def codes_of(release: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(c) for c in release.get("versionCodes", []) or [])

    wanted = codes_of(target)
    out: list[dict[str, Any]] = []
    swapped = False
    for release in track_body.get("releases", []) or []:
        if not swapped and codes_of(release) == wanted:
            out.append(replacement)
            swapped = True
        else:
            out.append(release)
    if not swapped:
        out.append(replacement)
    return out


def build_release(
    version_codes: list[str] | list[int],
    *,
    status: str = "completed",
    user_fraction: float | None = None,
    release_notes: dict[str, str] | None = None,
    name: str | None = None,
    in_app_update_priority: int | None = None,
) -> dict[str, Any]:
    """Build a ``TrackRelease`` body with the field combinations Play accepts."""
    if not version_codes:
        raise ValidationError(
            "A release needs at least one version code.",
            remedy=(
                "Upload a bundle first with play_upload_bundle, or pass the version codes of "
                "builds already uploaded to this app."
            ),
        )
    release: dict[str, Any] = {
        "versionCodes": [str(code) for code in version_codes],
        "status": status,
    }
    if status in {"inProgress", "halted"} and user_fraction is not None:
        release["userFraction"] = float(user_fraction)
    if name:
        release["name"] = name
    if in_app_update_priority is not None:
        release["inAppUpdatePriority"] = int(in_app_update_priority)
    if release_notes:
        release["releaseNotes"] = [
            {"language": locale, "text": text} for locale, text in sorted(release_notes.items())
        ]
    return release


# --- Rendering helpers shared by the write tools -----------------------------


@dataclass
class TrackSnapshot:
    """A human-readable freeze-frame of a track, for before/after previews."""

    track: str
    releases: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_track(cls, body: dict[str, Any]) -> TrackSnapshot:
        return cls(track=str(body.get("track", "?")), releases=body.get("releases", []) or [])

    def describe(self) -> str:
        if not self.releases:
            return "no active release"
        return "; ".join(describe_release(r) for r in self.releases)


def describe_release(release: dict[str, Any]) -> str:
    """One line a human can judge: build, status, and how many users get it."""
    codes = ", ".join(str(c) for c in release.get("versionCodes", []) or []) or "no builds"
    status = str(release.get("status", "unknown"))
    parts = [f"version code(s) {codes}", status]
    fraction = release.get("userFraction")
    if fraction is not None:
        parts.append(f"{float(fraction) * 100:g}% of users")
    elif status == "completed":
        parts.append("100% of users")
    if release.get("name"):
        parts.insert(0, f"'{release['name']}'")
    return " — ".join(parts)


def promotion_warning(from_track: str, to_track: str) -> str | None:
    """Flag promotions that skip testing stages or move backwards."""
    src, dst = _track_rank(from_track), _track_rank(to_track)
    if src is None or dst is None:
        return None
    if dst < src:
        return (
            f"This moves the build BACKWARDS, from '{from_track}' to the less exposed "
            f"'{to_track}'. That is unusual — confirm it is what you meant."
        )
    if dst - src > 1:
        skipped = ", ".join(TRACK_ORDER[src + 1 : dst])
        return (
            f"This skips the {skipped} track(s): the build goes straight from '{from_track}' "
            f"to '{to_track}' without the intermediate testing stage."
        )
    return None
