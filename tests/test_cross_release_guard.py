"""The two-store release is ONE guarded operation.

A half-confirmed release — the user approved the Play half and the Apple half
went out too, or the reverse — is worse than no release at all. So
``release_both`` issues a single token covering both stores, and anything that
changes what *either* store would do has to invalidate it.
"""

from __future__ import annotations

import pytest

from storepilot.core.errors import ValidationError
from storepilot.core.guards import Preview, issue_token, require_confirmation, verify_token
from storepilot.cross.apps import AppPair
from storepilot.cross.tools import release_both_operation

PAIR = AppPair(
    key="acme-todo",
    name="Acme Todo",
    play_package="com.acme.todo",
    apple_id="1234567890",
    bundle_id="com.acme.todo",
)

BASE = {
    "pair": PAIR,
    "version_name": "4.2.0",
    "play_track": "production",
    "play_status": "inProgress",
    "play_fraction": 0.1,
    "aab_sha256": "a" * 64,
    "aab_size": 41_000_000,
    "aab_path": "/build/app-release.aab",
    "release_notes": "Bug fixes and performance improvements.",
    "apple_build_id": "b-1180",
    "apple_build_number": "1180",
    "testflight_locale": "en-US",
    "call_args": {"version_name": "4.2.0"},
}


def operation(**overrides: object):
    return release_both_operation(**{**BASE, **overrides})  # type: ignore[arg-type]


def test_one_operation_names_both_stores() -> None:
    op = operation()
    assert op.tool == "release_both"
    assert "google_play:com.acme.todo" in op.target
    assert "app_store:1234567890" in op.target


def test_one_token_covers_the_whole_two_store_release() -> None:
    op = operation()
    token = issue_token(op.fingerprint())
    verify_token(token, op.fingerprint(), tool="release_both")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version_name", "4.2.1"),
        ("play_track", "beta"),
        ("play_status", "draft"),
        ("play_fraction", 0.2),
        ("aab_sha256", "b" * 64),  # the binary was rebuilt between the two calls
        ("aab_size", 41_000_001),
        ("release_notes", "Bug fixes and a surprise."),
        ("apple_build_id", "b-1181"),
        ("apple_build_number", "1181"),
        ("testflight_locale", "vi"),
    ],
)
def test_changing_what_either_store_does_invalidates_the_token(
    field: str, value: object
) -> None:
    token = issue_token(operation().fingerprint())
    drifted = operation(**{field: value})

    with pytest.raises(ValidationError) as excinfo:
        verify_token(token, drifted.fingerprint(), tool="release_both")
    assert "does not match these arguments" in excinfo.value.message


def test_the_same_binary_at_a_different_path_is_the_same_release() -> None:
    """The digest is what identifies the artifact; a moved file is not a new build."""
    token = issue_token(operation().fingerprint())
    moved = operation(aab_path="/elsewhere/app-release.aab")
    with pytest.raises(ValidationError):
        verify_token(token, moved.fingerprint(), tool="release_both")


def test_pointing_at_the_other_app_invalidates_the_token() -> None:
    token = issue_token(operation().fingerprint())
    other = operation(
        pair=AppPair(key="other", name="Other", play_package="com.acme.other", apple_id="999")
    )
    with pytest.raises(ValidationError):
        verify_token(token, other.fingerprint(), tool="release_both")


def test_the_preview_is_what_the_user_approves_and_it_mutates_nothing() -> None:
    op = operation()
    preview = Preview(
        summary="Release 4.2.0 to Play production (10%) and TestFlight build 1180",
        warnings=["This publishes to BOTH stores."],
    )
    text = require_confirmation(op, preview, confirm=False)

    assert text is not None
    assert "nothing has been changed yet" in text
    assert "google_play:com.acme.todo" in text
    assert "app_store:1234567890" in text
    assert "BOTH stores" in text
