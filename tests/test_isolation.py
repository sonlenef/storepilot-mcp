"""Self-tests for the suite's own safety rails.

If these fail, no other result in this suite can be trusted: the tests would be
reading and writing the operator's real guard key, replay ledger, audit log and
cached revenue data.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from storepilot.config import settings
from storepilot.core.cache import FileCache
from storepilot.core.guards import Operation, audit, audit_log_path, state_dir
from storepilot.cross.apps import registry_path
from tests.conftest import REAL_STATE_DIR


def test_state_dir_is_redirected_away_from_the_users_home(tmp_path: Path) -> None:
    for path in (state_dir(), audit_log_path(), registry_path()):
        assert REAL_STATE_DIR not in path.parents, f"{path} is inside the real state directory"
        assert str(tmp_path) in str(path), f"{path} is not inside this test's tmp_path"


def test_the_cache_is_redirected_too() -> None:
    resolved = settings.resolved_cache_dir
    assert REAL_STATE_DIR not in resolved.parents
    cache = FileCache("probe", root=resolved)
    cache.set("k", b"v")
    assert cache.path_for("k").exists()
    assert REAL_STATE_DIR not in cache.path_for("k").parents


def test_no_credentials_are_visible_to_a_test() -> None:
    """Tests must pass on a machine with a fully configured StorePilot, too."""
    assert settings.google_play_enabled is False
    assert settings.app_store_enabled is False
    assert settings.asc_vendor_number is None
    for var in ("STOREPILOT_GOOGLE_CREDENTIALS", "STOREPILOT_ASC_KEY_PATH"):
        assert var not in os.environ


def test_writing_to_the_guard_state_lands_in_the_sandbox() -> None:
    audit(Operation(tool="probe", target="none"), outcome="preview")
    written = audit_log_path()
    assert written.exists()
    assert REAL_STATE_DIR not in written.parents


def test_the_real_state_directory_is_not_written_to() -> None:
    """Belt and braces: the session fixture checks content hashes at the end too."""
    before = sorted(p.name for p in REAL_STATE_DIR.glob("*")) if REAL_STATE_DIR.exists() else []
    audit(Operation(tool="probe", target="none"), outcome="preview")
    after = sorted(p.name for p in REAL_STATE_DIR.glob("*")) if REAL_STATE_DIR.exists() else []
    assert before == after


def test_a_real_network_call_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="real network connection"):
        socket.create_connection(("api.appstoreconnect.apple.com", 443), timeout=0.1)

    with pytest.raises(RuntimeError, match="real network connection"):
        socket.socket().connect(("play.googleapis.com", 443))


def test_state_is_not_shared_between_tests(tmp_path: Path) -> None:
    """Each test gets a fresh ledger, which is what makes the suite re-runnable."""
    assert not audit_log_path().exists() or audit_log_path().read_text() == ""
