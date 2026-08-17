"""The local metadata mirror (a fastlane tree) and cross-store locale mapping.

Two stores, two directory layouts, two sets of length limits, and two spellings
of the same locale. Everything here is offline file handling, which makes it
cheap to test and expensive to get wrong: a mis-mapped locale publishes Vietnamese
copy to a Taiwanese storefront, and a missed length limit turns a push into a
store-side rejection halfway through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from storepilot.core.errors import ValidationError
from storepilot.core.metadata_mirror import (
    ABSENT,
    APPLE_FIELDS,
    CHANGED,
    NEW,
    PARITY_PAIRS,
    PLAY_FIELDS,
    UNCHANGED,
    MirrorState,
    changelog_path,
    counterpart_field,
    diff_fields,
    digest,
    load_state,
    locale_dir,
    locales_present,
    map_locale,
    metadata_root,
    over_limit,
    read_changelog,
    read_locale,
    record_state,
    save_state,
    spec_for,
    store_root,
    unmapped_locale_note,
    write_changelog,
    write_locale,
)
from storepilot.core.models import Store

PLAY = Store.GOOGLE_PLAY
APPLE = Store.APP_STORE


# --- Layout ------------------------------------------------------------------


def test_the_tree_is_a_fastlane_tree(tmp_path: Path) -> None:
    assert metadata_root(tmp_path) == tmp_path / "metadata"
    assert store_root(tmp_path, PLAY) == tmp_path / "metadata" / "android"
    assert store_root(tmp_path, APPLE) == tmp_path / "metadata" / "ios"
    assert locale_dir(tmp_path, PLAY, "en-US") == tmp_path / "metadata" / "android" / "en-US"
    assert (
        changelog_path(tmp_path, "en-US", 4501)
        == tmp_path / "metadata" / "android" / "en-US" / "changelogs" / "4501.txt"
    )


def test_underscored_locales_are_normalised() -> None:
    assert locale_dir("/base", PLAY, "en_US").name == "en-US"


@pytest.mark.parametrize("locale", ["../../etc", "/etc/passwd", "en US", "", "..", "e"])
def test_a_locale_that_is_a_path_fragment_is_refused(locale: str) -> None:
    """The locale becomes a directory name; escaping the tree must be impossible."""
    with pytest.raises(ValidationError) as excinfo:
        locale_dir("/base", PLAY, locale)
    assert "not a locale code" in excinfo.value.message


def test_changelogs_are_keyed_by_version_code_not_version_name() -> None:
    with pytest.raises(ValidationError) as excinfo:
        changelog_path("/base", "en-US", "3.2.1")
    assert "numeric version code" in excinfo.value.message


# --- Round trips -------------------------------------------------------------


def test_a_locale_round_trips_through_disk(tmp_path: Path) -> None:
    values = {
        "title": "Acme Todo",
        "short_description": "The todo app",
        "full_description": "Long copy.\n\nWith paragraphs.",
        "video_url": "https://youtu.be/abc",
    }
    writes = write_locale(tmp_path, PLAY, "en-US", values)
    assert all(w.status == "created" for w in writes)

    assert read_locale(tmp_path, PLAY, "en-US") == values


def test_writing_identical_content_twice_touches_nothing(tmp_path: Path) -> None:
    """A push that rewrites unchanged files is a real, user-visible store change."""
    write_locale(tmp_path, PLAY, "en-US", {"title": "Acme Todo"})
    second = write_locale(tmp_path, PLAY, "en-US", {"title": "Acme Todo"})
    assert [w.status for w in second] == ["unchanged"]
    assert second[0].changed is False

    third = write_locale(tmp_path, PLAY, "en-US", {"title": "Acme Todo Pro"})
    assert [w.status for w in third] == ["written"]


def test_a_none_value_is_not_written_as_an_empty_file(tmp_path: Path) -> None:
    """"The store did not return this field" is not "the store returned ''"."""
    write_locale(tmp_path, PLAY, "en-US", {"title": "Acme Todo", "full_description": None})
    assert read_locale(tmp_path, PLAY, "en-US") == {"title": "Acme Todo"}
    assert not (locale_dir(tmp_path, PLAY, "en-US") / "full_description.txt").exists()


def test_files_end_with_a_newline_so_git_diffs_stay_readable(tmp_path: Path) -> None:
    write_locale(tmp_path, PLAY, "en-US", {"title": "Acme Todo"})
    raw = (locale_dir(tmp_path, PLAY, "en-US") / "title.txt").read_bytes()
    assert raw == b"Acme Todo\n"


def test_changelogs_round_trip(tmp_path: Path) -> None:
    write_changelog(tmp_path, "en-US", 4501, "Bug fixes")
    assert read_changelog(tmp_path, "en-US", 4501) == "Bug fixes"
    assert read_changelog(tmp_path, "en-US", 9999) is None


def test_locales_present_lists_only_locale_directories(tmp_path: Path) -> None:
    write_locale(tmp_path, APPLE, "en-US", {"name": "Acme"})
    write_locale(tmp_path, APPLE, "vi", {"name": "Acme"})
    (store_root(tmp_path, APPLE) / "review_information").mkdir(parents=True, exist_ok=True)
    (store_root(tmp_path, APPLE) / ".hidden").mkdir(exist_ok=True)

    assert locales_present(tmp_path, APPLE) == ["en-US", "vi"]
    assert locales_present(tmp_path, PLAY) == []


# --- Length limits -----------------------------------------------------------


def test_each_store_has_its_own_limit_for_the_same_idea() -> None:
    """Play's 80-character short description does not fit Apple's 30-character subtitle."""
    text = "x" * 80
    assert over_limit(PLAY, "short_description", text) == 0
    assert over_limit(APPLE, "subtitle", text) == 50
    assert counterpart_field(PLAY, "short_description") == "subtitle"
    assert counterpart_field(APPLE, "subtitle") == "short_description"


def test_fields_without_a_limit_never_report_one() -> None:
    assert over_limit(PLAY, "video_url", "https://" + "x" * 500) == 0
    assert over_limit(PLAY, "not_a_field", "anything") == 0
    assert over_limit(APPLE, "keywords", None) == 0


def test_the_keyword_field_documents_the_comma_space_trap() -> None:
    spec = spec_for(APPLE, "keywords")
    assert spec is not None and spec.limit == 100
    assert "NO space after the comma" in (spec.note or "")
    assert spec_for(PLAY, "keywords") is None, "Play has no keyword field at all"


def test_parity_pairs_are_symmetric() -> None:
    play_fields = {s.field for s in PLAY_FIELDS}
    apple_fields = {s.field for s in APPLE_FIELDS}
    for play_field, apple_field in PARITY_PAIRS:
        assert play_field in play_fields
        assert apple_field in apple_fields
        assert counterpart_field(PLAY, play_field) == apple_field
        assert counterpart_field(APPLE, apple_field) == play_field


# --- Locale mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("play_locale", "apple_locale"),
    [
        ("en-US", "en-US"),
        ("vi", "vi"),
        ("zh-TW", "zh-Hant"),
        ("iw-IL", "he"),
        ("in-ID", "id"),
        ("es-419", "es-MX"),
        ("pt-BR", "pt-BR"),
        ("fr-CA", "fr-CA"),
    ],
)
def test_locales_round_trip_between_the_two_stores(play_locale: str, apple_locale: str) -> None:
    """Play 'zh-TW' is Apple 'zh-Hant'; publishing one to the other is a wrong storefront."""
    assert map_locale(play_locale, source=PLAY, target=APPLE) == apple_locale
    assert map_locale(apple_locale, source=APPLE, target=PLAY) == play_locale


def test_mapping_is_case_insensitive_on_the_script_subtag() -> None:
    assert map_locale("zh-hant", source=APPLE, target=PLAY) == "zh-TW"


def test_mapping_to_the_same_store_is_the_identity() -> None:
    assert map_locale("en-GB", source=PLAY, target=PLAY) == "en-GB"


@pytest.mark.parametrize("locale", ["en-IN", "fil", "am"])
def test_locales_one_store_simply_does_not_have_are_reported_not_guessed(locale: str) -> None:
    assert map_locale(locale, source=PLAY, target=APPLE) is None
    note = unmapped_locale_note(locale, source=PLAY, target=APPLE)
    assert locale in note
    assert "App Store Connect" in note or "Apple" in note


def test_an_unknown_locale_note_explains_the_general_problem() -> None:
    note = unmapped_locale_note("xx-YY", source=APPLE, target=PLAY)
    assert "Google Play" in note
    assert "zh-TW" in note and "zh-Hant" in note


# --- Diffing -----------------------------------------------------------------


def test_diff_classifies_every_outcome_a_push_can_have() -> None:
    diffs = {
        d.field: d
        for d in diff_fields(
            PLAY,
            local={
                "title": "New Title",
                "short_description": "Same",
                "full_description": "Creates this",
            },
            remote={
                "title": "Old Title",
                "short_description": "Same",
                "full_description": "",
                "video_url": "https://youtu.be/abc",
            },
        )
    }

    assert diffs["title"].status == CHANGED
    assert diffs["short_description"].status == UNCHANGED
    assert diffs["full_description"].status == NEW
    assert diffs["video_url"].status == ABSENT

    assert diffs["title"].will_push is True
    assert diffs["short_description"].will_push is False
    assert diffs["video_url"].will_push is False
    assert "left untouched" in diffs["video_url"].summarize()


def test_a_field_over_the_limit_is_blocked_rather_than_sent() -> None:
    [diff] = diff_fields(
        APPLE, local={"subtitle": "x" * 45}, remote={"subtitle": "old"}, fields=["subtitle"]
    )
    assert diff.status == CHANGED
    assert diff.over_by == 15
    assert diff.will_push is False
    assert "BLOCKED" in diff.summarize()


def test_whitespace_only_changes_do_not_count_as_edits() -> None:
    """A formatter or a git checkout must not make a push look necessary."""
    [diff] = diff_fields(
        PLAY, local={"title": "Acme Todo\n"}, remote={"title": "  Acme Todo  "}, fields=["title"]
    )
    assert diff.status == UNCHANGED
    assert digest("Acme Todo\n") == digest("  Acme Todo  ")


# --- The pull-time sidecar ---------------------------------------------------


def test_state_answers_did_i_edit_this_or_did_the_store(tmp_path: Path) -> None:
    state = MirrorState()
    writes = write_locale(tmp_path, PLAY, "en-US", {"title": "Acme Todo"})
    record_state(state, PLAY, "en-US", writes)
    save_state(tmp_path, state)

    reloaded = load_state(tmp_path)
    assert reloaded.pulled_at is not None
    assert reloaded.locally_edited(PLAY, "en-US", "title", "Acme Todo") is False
    assert reloaded.locally_edited(PLAY, "en-US", "title", "Acme Todo Pro") is True
    # A field never pulled cannot be called edited.
    assert reloaded.locally_edited(PLAY, "en-US", "full_description", "anything") is False


def test_a_corrupt_sidecar_degrades_to_no_state_known(tmp_path: Path) -> None:
    metadata_root(tmp_path).mkdir(parents=True)
    (metadata_root(tmp_path) / ".storepilot-metadata.json").write_text("{not json")
    state = load_state(tmp_path)
    assert state.entries == {}
    assert state.locally_edited(PLAY, "en-US", "title", "anything") is False
