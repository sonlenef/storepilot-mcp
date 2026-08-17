"""The app-pairing registry and the auto-pairing heuristic.

No API on either store says that a Play package and an App Store app are the same
product, so this mapping is the join key for every cross-store answer. A wrong
pair silently attributes one app's revenue, reviews and crash rate to another and
the user has no way to notice — which is why the heuristic only ever *proposes*,
and why one-to-one assignment matters more than recall.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from storepilot.core.errors import ValidationError
from storepilot.core.models import Store
from storepilot.cross.apps import (
    HIGH_CONFIDENCE,
    MIN_PROPOSAL_SCORE,
    AppPair,
    Registry,
    StoreApp,
    build_portfolio,
    default_metadata_dir,
    load,
    propose,
    registry_path,
    save,
    slugify,
    upsert,
)


def play(app_id: str, name: str) -> StoreApp:
    return StoreApp(Store.GOOGLE_PLAY, app_id, name, app_id)


def apple(app_id: str, name: str, bundle_id: str | None = None) -> StoreApp:
    return StoreApp(Store.APP_STORE, app_id, name, bundle_id)


# --- The file ----------------------------------------------------------------


def test_registry_path_follows_the_state_dir_and_the_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STOREPILOT_APPS_FILE", raising=False)
    monkeypatch.setenv("STOREPILOT_STATE_DIR", str(tmp_path / "state"))
    assert registry_path() == tmp_path / "state" / "apps.toml"

    monkeypatch.setenv("STOREPILOT_APPS_FILE", str(tmp_path / "elsewhere.toml"))
    assert registry_path() == tmp_path / "elsewhere.toml"


def test_a_missing_registry_is_an_empty_state_not_an_error(tmp_path: Path) -> None:
    registry = load(tmp_path / "nothing-here.toml")
    assert registry.pairs == []
    assert registry.exists is False
    assert registry.warnings == []


def test_a_registry_round_trips_through_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "apps.toml"
    pairs = [
        AppPair(
            key="acme-todo",
            name='Acme "Todo"',
            play_package="com.acme.todo",
            apple_id="1234567890",
            bundle_id="com.acme.todo",
            metadata_dir="~/code/todo",
            locales=("en-US", "vi"),
        ),
        AppPair(key="ios-only", name="Notes", apple_id="1999888777"),
    ]
    save(pairs, path)
    loaded = load(path)

    assert loaded.exists is True
    assert loaded.warnings == []
    assert {p.key for p in loaded.pairs} == {"acme-todo", "ios-only"}
    todo = loaded.get("acme-todo")
    assert todo is not None
    assert todo.name == 'Acme "Todo"', "quotes in a name must survive the TOML round trip"
    assert todo.locales == ("en-US", "vi")
    assert todo.metadata_dir == "~/code/todo"
    assert loaded.get("ios-only").play_package is None  # type: ignore[union-attr]


def test_broken_toml_is_refused_with_a_way_out(tmp_path: Path) -> None:
    path = tmp_path / "apps.toml"
    path.write_text('[apps.acme-todo]\nplay = "unterminated\n')
    with pytest.raises(ValidationError) as excinfo:
        load(path)
    assert "not valid TOML" in excinfo.value.message
    assert "pair_apps" in excinfo.value.remedy


def test_a_hand_written_flat_file_is_tolerated_and_flagged(tmp_path: Path) -> None:
    path = tmp_path / "apps.toml"
    path.write_text('[acme-todo]\nplay = "com.acme.todo"\nappstore = "1234567890"\n')
    registry = load(path)
    assert [p.key for p in registry.pairs] == ["acme-todo"]
    assert any("top level" in w for w in registry.warnings)


def test_a_bundle_id_in_the_appstore_field_is_flagged(tmp_path: Path) -> None:
    """The Apple ID is numeric; pasting the bundle id here silently breaks lookups."""
    path = tmp_path / "apps.toml"
    path.write_text('[apps.todo]\nplay = "com.acme.todo"\nappstore = "com.acme.todo"\n')
    registry = load(path)
    assert any("not a numeric Apple ID" in w for w in registry.warnings)
    assert registry.pairs, "the entry is still loaded — the warning is the point, not a rejection"


def test_two_entries_claiming_one_app_are_reported_and_the_first_wins(tmp_path: Path) -> None:
    path = tmp_path / "apps.toml"
    path.write_text(
        '[apps.first]\nplay = "com.acme.todo"\n\n[apps.second]\nplay = "com.acme.todo"\n'
    )
    registry = load(path)
    assert [p.key for p in registry.pairs] == ["first"]
    assert any("claimed by both" in w for w in registry.warnings)


def test_unusable_entries_are_skipped_with_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "apps.toml"
    path.write_text(
        '[apps.no-ids]\nname = "Nothing"\n\n'
        '[apps."Bad Key"]\nplay = "com.acme.x"\n\n'
        '[apps.bad-locales]\nplay = "com.acme.y"\nlocales = "en-US"\n'
    )
    registry = load(path)
    assert {p.key for p in registry.pairs} == {"bad-locales"}
    assert any("names neither" in w for w in registry.warnings)
    assert any("unusable key" in w for w in registry.warnings)
    assert any("locales must be a list" in w for w in registry.warnings)


def test_an_unwritable_registry_says_where_to_point_it_instead(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(ValidationError) as excinfo:
            save([AppPair(key="x", name="X", play_package="com.x")], locked / "apps.toml")
    finally:
        locked.chmod(0o700)
    assert "STOREPILOT_APPS_FILE" in excinfo.value.remedy


# --- Lookup ------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["acme-todo", "Acme Todo", "acme todo".replace(" ", "-"), "com.acme.todo", "1234567890", "com.acme"],
)
def test_an_app_can_be_named_by_key_name_package_or_apple_id(query: str) -> None:
    pair = AppPair(
        key="acme-todo",
        name="Acme Todo",
        play_package="com.acme.todo",
        apple_id="1234567890",
        bundle_id="com.acme.todo",
    )
    assert pair.matches(query) is True
    assert pair.matches("something-else") is False
    assert pair.matches("") is False


def test_pair_properties() -> None:
    both = AppPair(key="k", name="N", play_package="com.acme.todo", apple_id="1")
    assert both.is_paired is True
    assert both.stores == (Store.GOOGLE_PLAY, Store.APP_STORE)
    assert both.app_id(Store.APP_STORE) == "1"

    play_only = AppPair(key="k", name="N", play_package="com.acme.todo")
    assert play_only.is_paired is False
    assert play_only.has(Store.APP_STORE) is False


def test_upsert_merges_rather_than_duplicating() -> None:
    registry = Registry(
        path=Path("/tmp/x.toml"),
        pairs=[AppPair(key="acme-todo", name="Acme Todo", play_package="com.acme.todo")],
    )
    pairs, message = upsert(
        registry, AppPair(key="whatever", name="Acme Todo", play_package="com.acme.todo",
                          apple_id="1234567890")
    )
    assert len(pairs) == 1, "adding the Apple side must extend the entry, not create a rival"
    assert pairs[0].key == "acme-todo"
    assert pairs[0].apple_id == "1234567890"
    assert "updated" in message

    pairs, message = upsert(
        Registry(path=registry.path, pairs=pairs),
        AppPair(key="acme-todo", name="Acme Todo", play_package="com.acme.todo",
                apple_id="1234567890"),
    )
    assert "nothing changed" in message

    pairs, message = upsert(
        Registry(path=registry.path, pairs=pairs),
        AppPair(key="other", name="Other", play_package="com.other"),
    )
    assert len(pairs) == 2
    assert "added" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Acme Todo", "acme-todo"), ("Acme Todo: Task List", "acme-todo-task-list"),
     ("  ™Acme  ", "acme"), ("!!!", "app")],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_metadata_dir_defaults_under_the_state_dir_not_the_cwd(tmp_path: Path) -> None:
    """An MCP server's cwd is wherever the client launched it — never a good default."""
    pair = AppPair(key="acme-todo", name="Acme Todo", play_package="com.acme.todo")
    resolved = default_metadata_dir(pair)
    assert resolved == Path(os.environ["STOREPILOT_STATE_DIR"]) / "metadata" / "acme-todo"
    assert Path.cwd() not in resolved.parents

    explicit = AppPair(key="k", name="N", metadata_dir=str(tmp_path / "repo"))
    assert default_metadata_dir(explicit) == tmp_path / "repo"


# --- The heuristic -----------------------------------------------------------


def test_an_identical_bundle_id_is_a_high_confidence_pair() -> None:
    proposals, _, _ = propose(
        [play("com.acme.todo", "Acme Todo")],
        [apple("1234567890", "Acme Todo", "com.acme.todo")],
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.score >= HIGH_CONFIDENCE
    assert proposal.confidence == "high"
    assert "identical bundle id" in proposal.reasons
    assert "identical name" in proposal.reasons
    assert proposal.key == "acme-todo"


def test_a_platform_suffix_on_one_side_still_pairs() -> None:
    proposals, _, _ = propose(
        [play("com.acme.photo", "Acme Photo Editor")],
        [apple("1555000111", "Acme Photo Editor", "com.acme.photo.ios")],
    )
    assert proposals[0].confidence == "high"
    assert any("platform suffix" in reason for reason in proposals[0].reasons)


def test_a_renamed_listing_pairs_on_the_bundle_id_alone() -> None:
    proposals, _, _ = propose(
        [play("com.acme.todo", "Acme Todo")],
        [apple("1234567890", "Completely Different Marketing Name", "com.acme.todo")],
    )
    assert len(proposals) == 1
    assert proposals[0].reasons == ("identical bundle id",)


def test_a_shared_reverse_domain_alone_is_not_enough() -> None:
    """Two unrelated apps from one company must not be paired."""
    proposals, unmatched_play, unmatched_apple = propose(
        [play("com.acme.widgets", "Acme Widgets")],
        [apple("1999888777", "Acme Notes", "com.acme.notes")],
    )
    assert proposals == []
    assert len(unmatched_play) == 1 and len(unmatched_apple) == 1


def test_assignment_is_one_to_one_even_when_several_apps_look_alike() -> None:
    """Two rows silently sharing one app's revenue is worse than an unpaired row."""
    proposals, unmatched_play, unmatched_apple = propose(
        [play("com.acme.todo", "Acme Todo"), play("com.acme.todo2", "Acme Todo")],
        [
            apple("1234567890", "Acme Todo", "com.acme.todo"),
            apple("1111111111", "Acme Todo", "com.acme.todo.pro"),
        ],
    )
    used_play = {p.play.app_id for p in proposals}
    used_apple = {p.apple.app_id for p in proposals}
    assert len(used_play) == len(proposals)
    assert len(used_apple) == len(proposals)
    assert proposals[0].play.app_id == "com.acme.todo"
    assert proposals[0].apple.app_id == "1234567890", "the strongest evidence wins first"
    assert len(unmatched_play) + len(unmatched_apple) == 4 - 2 * len(proposals)


def test_apps_already_claimed_by_the_registry_are_not_re_proposed() -> None:
    registry = Registry(
        path=Path("/tmp/x.toml"),
        pairs=[AppPair(key="acme-todo", name="Acme Todo", play_package="com.acme.todo",
                       apple_id="1234567890")],
    )
    proposals, unmatched_play, unmatched_apple = propose(
        [play("com.acme.todo", "Acme Todo")],
        [apple("1234567890", "Acme Todo", "com.acme.todo")],
        registry,
    )
    assert proposals == []
    assert unmatched_play == [] and unmatched_apple == []


def test_proposal_keys_never_collide_with_registry_keys() -> None:
    registry = Registry(
        path=Path("/tmp/x.toml"),
        pairs=[AppPair(key="acme-todo", name="Something Else", play_package="com.other.thing")],
    )
    proposals, _, _ = propose(
        [play("com.acme.todo", "Acme Todo")],
        [apple("1234567890", "Acme Todo", "com.acme.todo")],
        registry,
    )
    assert proposals[0].key == "acme-todo-2"


def test_the_confidence_floor_is_respected() -> None:
    weak, _, _ = propose(
        [play("com.acme.todo", "Acme Todo")],
        [apple("1", "Acme Todo", "com.acme.todo")],
        minimum=1.1,
    )
    assert weak == []
    assert MIN_PROPOSAL_SCORE == 0.60


# --- Portfolio assembly ------------------------------------------------------


def test_single_store_apps_are_first_class_rows() -> None:
    pairs, warnings = build_portfolio(
        [play("com.acme.widgets", "Acme Widgets")],
        [apple("1999888777", "Acme Notes", "com.acme.notes")],
        Registry(path=Path("/tmp/x.toml")),
    )
    assert {p.name for p in pairs} == {"Acme Widgets", "Acme Notes"}
    assert all(p.source == "unpaired" for p in pairs)
    assert warnings == []


def test_a_registry_entry_the_account_cannot_see_is_kept_and_explained() -> None:
    registry = Registry(
        path=Path("/tmp/x.toml"),
        pairs=[AppPair(key="ghost", name="Ghost", play_package="com.acme.ghost",
                       apple_id="1000000000")],
    )
    pairs, warnings = build_portfolio([], [], registry)

    assert [p.key for p in pairs] == ["ghost"]
    assert len(warnings) == 2
    assert any("this Play account cannot see" in w for w in warnings)
    assert any("key cannot see" in w for w in warnings)


def test_an_unconfigured_store_does_not_produce_a_permissions_warning() -> None:
    """"Not set up" and "not permitted" send the user to entirely different places."""
    registry = Registry(
        path=Path("/tmp/x.toml"),
        pairs=[AppPair(key="ghost", name="Ghost", play_package="com.acme.ghost",
                       apple_id="1000000000")],
    )
    pairs, warnings = build_portfolio([], [], registry, unavailable={Store.APP_STORE})

    assert [p.key for p in pairs] == ["ghost"]
    assert len(warnings) == 1
    assert "Play account cannot see" in warnings[0]


def test_the_live_store_name_replaces_a_placeholder_registry_name() -> None:
    registry = Registry(
        path=Path("/tmp/x.toml"),
        pairs=[AppPair(key="todo", name="com.acme.todo", play_package="com.acme.todo")],
    )
    pairs, _ = build_portfolio([play("com.acme.todo", "Acme Todo")], [], registry)
    assert pairs[0].name == "Acme Todo"


def test_unpaired_keys_are_deduplicated() -> None:
    pairs, _ = build_portfolio(
        [play("com.acme.todo", "Todo"), play("com.other.todo", "Todo")],
        [apple("1", "Todo", "com.third.todo")],
        Registry(path=Path("/tmp/x.toml")),
    )
    keys = [p.key for p in pairs]
    assert len(set(keys)) == len(keys)
