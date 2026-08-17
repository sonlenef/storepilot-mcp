"""The Android Publisher edits lifecycle.

Nothing on Play is live until ``edits.commit``, which is what makes an honest
preview possible — but only if the sequence is exactly right. Two rules must
never break, and both are invisible in any single call:

* ``validate`` always runs before ``commit``;
* on any failure the edit is deleted and the original exception propagates.

So these tests assert on the *ordering* of the calls, not on their return values.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from storepilot.core.errors import NotFoundError, StorePilotError, ValidationError
from storepilot.google_play import publisher
from storepilot.google_play.publisher import (
    PlayEdit,
    build_release,
    current_release,
    describe_release,
    inspect_aab,
    promotion_warning,
)
from tests.support.fake_google import FakeClient

TRACK_BODY = {
    "track": "production",
    "releases": [
        {
            "versionCodes": ["4400"],
            "status": "inProgress",
            "userFraction": 0.1,
            "name": "4.1.0",
        }
    ],
}


# --- Call ordering -----------------------------------------------------------


def test_real_edit_runs_insert_change_validate_commit() -> None:
    log: list[str] = []
    with PlayEdit("com.acme.todo", client=FakeClient(log, TRACK_BODY)) as edit:
        edit.set_release("internal", [4501], status="completed")

    assert log == ["insert", "tracks.update", "validate", "commit"]
    assert edit.committed is True
    assert edit.discarded is False


def test_dry_run_runs_insert_change_validate_delete_and_never_commits() -> None:
    """The preview is a real server-side validation, then a deliberate discard."""
    log: list[str] = []
    with PlayEdit("com.acme.todo", dry_run=True, client=FakeClient(log, TRACK_BODY)) as edit:
        edit.set_release("internal", [4501], status="completed")

    assert log == ["insert", "tracks.update", "validate", "delete"]
    assert "commit" not in log
    assert edit.committed is False
    assert edit.discarded is True


def test_exception_mid_edit_deletes_and_propagates_the_original_error() -> None:
    log: list[str] = []
    sentinel = KeyError("something went wrong mid-edit")

    with (
        pytest.raises(KeyError) as excinfo,
        PlayEdit("com.acme.todo", client=FakeClient(log, TRACK_BODY)) as edit,
    ):
        edit.set_release("internal", [4501], status="completed")
        raise sentinel

    assert excinfo.value is sentinel, "the original exception must arrive untouched"
    assert log == ["insert", "tracks.update", "delete"]
    assert "commit" not in log
    assert "validate" not in log, "a failed edit is not validated, only discarded"


def test_a_failing_validate_never_commits() -> None:
    log: list[str] = []
    client = FakeClient(log, TRACK_BODY, fail_on="validate")

    with pytest.raises(StorePilotError), PlayEdit("com.acme.todo", client=client) as edit:
        edit.set_release("internal", [4501], status="completed")

    assert "commit" not in log
    assert log == ["insert", "tracks.update", "validate", "delete"]


def test_a_failing_commit_still_discards_the_edit() -> None:
    log: list[str] = []
    client = FakeClient(log, TRACK_BODY, fail_on="commit")

    with pytest.raises(StorePilotError), PlayEdit("com.acme.todo", client=client) as edit:
        edit.set_release("internal", [4501], status="completed")

    assert log == ["insert", "tracks.update", "validate", "commit", "delete"]


def test_cleanup_failure_never_masks_the_real_error() -> None:
    """If delete also fails, the caller must still see what actually went wrong."""
    log: list[str] = []
    client = FakeClient(log, TRACK_BODY, fail_on="delete")

    with (
        pytest.raises(RuntimeError, match="the real problem"),
        PlayEdit("com.acme.todo", client=client) as edit,
    ):
        edit.set_release("internal", [4501], status="completed")
        raise RuntimeError("the real problem")

    assert edit.cleanup_error is not None


def test_discard_skips_validate_entirely() -> None:
    """The "nothing to do after all" branch, which must not surface unrelated errors."""
    log: list[str] = []
    with PlayEdit("com.acme.todo", client=FakeClient(log, TRACK_BODY)) as edit:
        edit.get_track("production")
        edit.discard()

    assert log == ["insert", "tracks.get", "delete"]
    assert "validate" not in log


def test_reads_open_a_throwaway_edit_and_delete_it(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    monkeypatch.setattr(publisher, "publisher_client", lambda: FakeClient(log, TRACK_BODY))

    body = publisher.read_track("com.acme.todo", "production")
    assert body == TRACK_BODY
    assert log == ["insert", "tracks.get", "validate", "delete"]

    log.clear()
    releases = publisher.list_track_releases("com.acme.todo", "production")
    assert releases == TRACK_BODY["releases"]
    assert log == ["applications.tracks.releases.list"], "an edit-free read opens no edit"


def test_using_an_edit_outside_its_with_block_is_refused() -> None:
    edit = PlayEdit("com.acme.todo", client=FakeClient())
    with pytest.raises(ValidationError, match="no edit is open"):
        edit.get_track("production")


def test_missing_edit_id_from_google_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient([], TRACK_BODY)
    monkeypatch.setattr(
        client.edits(), "insert", lambda **kw: _StaticRequest({"expiryTimeSeconds": "1"})
    )
    with (
        pytest.raises(ValidationError, match="returned no edit id"),
        PlayEdit("com.acme.todo", client=client),
    ):
        pass


class _StaticRequest:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


# --- Listings ----------------------------------------------------------------


def test_listing_updates_patch_rather_than_replace() -> None:
    """``listings.update`` is a PUT: omitting a field would wipe it."""
    log: list[str] = []
    client = FakeClient(log, TRACK_BODY)
    with PlayEdit("com.acme.todo", client=client) as edit:
        edit.patch_listing("en-US", {"title": "New Title"})

    assert "listings.patch" in log
    assert "listings.update" not in log
    _name, kwargs = next(c for c in client.edits().calls if c[0] == "listings.patch")
    assert kwargs["body"] == {"language": "en-US", "title": "New Title"}


def test_a_locale_with_no_listing_yet_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    client = FakeClient(log, TRACK_BODY)
    edits = client.edits()
    edits.fail_on = "listings.get"
    edits.error = NotFoundError("no listing", remedy="create it")

    with PlayEdit("com.acme.todo", client=client) as edit:
        assert edit.get_listing("vi") == {"language": "vi"}


# --- Release bodies ----------------------------------------------------------


def test_user_fraction_is_dropped_for_statuses_play_rejects_it_with() -> None:
    assert "userFraction" not in build_release([1], status="completed", user_fraction=0.1)
    assert "userFraction" not in build_release([1], status="draft", user_fraction=0.1)
    assert build_release([1], status="inProgress", user_fraction=0.1)["userFraction"] == 0.1
    assert build_release([1], status="halted", user_fraction=0.1)["userFraction"] == 0.1


def test_version_codes_are_stringified_and_release_notes_sorted() -> None:
    body = build_release(
        [4501, "4502"],
        status="completed",
        release_notes={"vi": "Sửa lỗi", "en-US": "Bug fixes"},
        name="4.2.0",
        in_app_update_priority=3,
    )
    assert body["versionCodes"] == ["4501", "4502"]
    assert [note["language"] for note in body["releaseNotes"]] == ["en-US", "vi"]
    assert body["inAppUpdatePriority"] == 3


def test_a_release_with_no_version_codes_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one version code"):
        build_release([])


# --- Reading the live release ------------------------------------------------


def test_current_release_prefers_the_rollout_actually_being_served() -> None:
    body = {
        "releases": [
            {"status": "draft", "versionCodes": ["4600"]},
            {"status": "completed", "versionCodes": ["4400"]},
            {"status": "inProgress", "versionCodes": ["4500"], "userFraction": 0.1},
            {"status": "halted", "versionCodes": ["4300"]},
        ]
    }
    assert current_release(body)["versionCodes"] == ["4500"]  # type: ignore[index]
    assert current_release({"releases": []}) is None


def test_describe_release_states_the_audience() -> None:
    assert "10% of users" in describe_release(
        {"versionCodes": ["4500"], "status": "inProgress", "userFraction": 0.1}
    )
    assert "100% of users" in describe_release({"versionCodes": ["4400"], "status": "completed"})
    assert "no builds" in describe_release({"status": "draft"})


@pytest.mark.parametrize(
    ("source", "destination", "expected"),
    [
        ("internal", "alpha", None),
        ("beta", "production", None),
        ("internal", "production", "skips"),
        ("production", "beta", "BACKWARDS"),
        ("qa-team", "production", None),  # custom tracks carry no ordering
    ],
)
def test_promotion_warning(source: str, destination: str, expected: str | None) -> None:
    warning = promotion_warning(source, destination)
    if expected is None:
        assert warning is None
    else:
        assert warning is not None and expected in warning


# --- Local AAB checks (these run before anything is uploaded) ----------------


def make_aab(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("BundleConfig.pb", b"\x00fake")
        archive.writestr("base/manifest/AndroidManifest.xml", b"\x00fake")
    return path


def test_inspect_aab_accepts_a_real_bundle(tmp_path: Path) -> None:
    info = inspect_aab(make_aab(tmp_path / "app-release.aab"))
    assert info.size_bytes > 0
    assert len(info.sha256) == 64
    assert str(tmp_path) not in info.display_path, "the preview must not echo the home directory"


def test_inspect_aab_rejects_an_apk(tmp_path: Path) -> None:
    apk = tmp_path / "app-release.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"\x00fake")
    with pytest.raises(ValidationError, match="is an APK, not an Android App Bundle"):
        inspect_aab(apk)


def test_inspect_aab_rejects_a_zip_that_is_not_a_bundle(tmp_path: Path) -> None:
    other = tmp_path / "build.aab"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"\x00fake")
    with pytest.raises(ValidationError, match="does not contain BundleConfig.pb"):
        inspect_aab(other)


def test_inspect_aab_rejects_missing_empty_and_non_zip_files(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        inspect_aab(tmp_path / "nope.aab")

    empty = tmp_path / "empty.aab"
    empty.write_bytes(b"")
    with pytest.raises(ValidationError, match="empty"):
        inspect_aab(empty)

    truncated = tmp_path / "truncated.aab"
    truncated.write_bytes(b"PK\x03\x04not really a zip")
    with pytest.raises(ValidationError, match="not a valid app bundle"):
        inspect_aab(truncated)

    with pytest.raises(ValidationError, match="is a directory"):
        inspect_aab(tmp_path)
