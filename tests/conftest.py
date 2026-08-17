"""Suite-wide isolation.

Two things in this project are dangerous to a test run:

* ``~/.storepilot/`` holds the user's guard HMAC key, the replay-nonce ledger,
  the audit log and cached revenue data. A test that writes there could clear a
  real audit trail or rotate a real guard key.
* ``storepilot.config.settings`` is a module-level singleton built at *import*
  time from the environment and a ``.env`` file, so redirecting it inside a test
  function is too late for anything already imported.

So the sandbox is installed at conftest import (before any test module imports
storepilot), re-applied per test against the live ``settings`` object, and the
real state directory is fingerprinted at session start and re-checked at the end.

Network access is blocked outright: every store adapter must be exercised
through ``httpx.MockTransport`` or a fake client object.
"""

from __future__ import annotations

import hashlib
import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# --- Import-time sandbox -----------------------------------------------------

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="storepilot-tests-"))

#: The user's real state. Nothing in this suite may touch it.
REAL_STATE_DIR = Path.home() / ".storepilot"

#: Anything that could accidentally supply real credentials to a test.
_CREDENTIAL_VARS = (
    "STOREPILOT_GOOGLE_CREDENTIALS",
    "STOREPILOT_GOOGLE_REPORTS_BUCKET",
    "STOREPILOT_ASC_KEY_PATH",
    "STOREPILOT_ASC_KEY_ID",
    "STOREPILOT_ASC_ISSUER_ID",
    "STOREPILOT_ASC_VENDOR_NUMBER",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "STOREPILOT_MAX_INITIAL_ROLLOUT",
)

for _var in _CREDENTIAL_VARS:
    os.environ.pop(_var, None)

os.environ["STOREPILOT_STATE_DIR"] = str(_SESSION_DIR / "state")
os.environ["STOREPILOT_AUDIT_LOG"] = str(_SESSION_DIR / "state" / "audit.log")
os.environ["STOREPILOT_APPS_FILE"] = str(_SESSION_DIR / "state" / "apps.toml")
os.environ["STOREPILOT_CACHE_DIR"] = str(_SESSION_DIR / "cache")


def _fingerprint(directory: Path) -> dict[str, str]:
    """Content hash of every file under ``directory`` (empty when it does not exist)."""
    if not directory.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            try:
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:  # pragma: no cover - unreadable file, still a change signal
                out[str(path)] = f"unreadable:{exc}"
    return out


@pytest.fixture(scope="session", autouse=True)
def real_state_untouched() -> Iterator[None]:
    """Fail the run if anything under ``~/.storepilot`` changed.

    A leaked test that rewrites ``guard.key`` or truncates ``audit.log`` is a
    serious failure, and it is silent unless something checks for it.
    """
    before = _fingerprint(REAL_STATE_DIR)
    yield
    after = _fingerprint(REAL_STATE_DIR)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        pytest.fail(
            "The test run modified the user's real state directory "
            f"{REAL_STATE_DIR}: added={added} removed={removed} changed={changed}"
        )


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Make a real socket connection impossible.

    Stubbing the transport in each test is the discipline; this is the backstop
    that turns a missed stub into a loud failure instead of a slow, flaky test
    that quietly talks to Google or Apple. Tests marked ``live`` opt out — they
    are excluded from the default run and exist only for manual verification
    against a real account.
    """
    if request.node.get_closest_marker("live"):
        yield
        return

    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "A test attempted a real network connection. Stub the transport "
            "(httpx.MockTransport for App Store, a fake client for Google)."
        )

    socket.socket.connect = blocked  # type: ignore[method-assign]
    socket.create_connection = blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.create_connection = real_create  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give every test its own state dir, cache dir, registry and audit log.

    Per test rather than per session because the guard's replay ledger and audit
    log are append-only: sharing them would let one test's spent nonce or logged
    line change another test's result, and would make a second run of the suite
    behave differently from the first.
    """
    from storepilot.config import settings
    from storepilot.core import guards

    state = tmp_path / "state"
    cache = tmp_path / "cache"
    monkeypatch.setenv("STOREPILOT_STATE_DIR", str(state))
    monkeypatch.setenv("STOREPILOT_AUDIT_LOG", str(state / "audit.log"))
    monkeypatch.setenv("STOREPILOT_APPS_FILE", str(state / "apps.toml"))
    monkeypatch.setenv("STOREPILOT_CACHE_DIR", str(cache))
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)

    # The settings singleton already exists, so the environment alone is not
    # enough — every credential-bearing field is blanked on the live object.
    monkeypatch.setattr(settings, "cache_dir", cache)
    monkeypatch.setattr(settings, "cache_enabled", True)
    monkeypatch.setattr(settings, "google_credentials", None)
    monkeypatch.setattr(settings, "google_reports_bucket", None)
    monkeypatch.setattr(settings, "asc_key_path", None)
    monkeypatch.setattr(settings, "asc_key_id", None)
    monkeypatch.setattr(settings, "asc_issuer_id", None)
    monkeypatch.setattr(settings, "asc_vendor_number", None)

    guards.reset_state_cache()
    guards.clear_warnings()
    _reset_singletons()
    try:
        yield state
    finally:
        guards.reset_state_cache()
        guards.clear_warnings()
        _reset_singletons()


def _reset_singletons() -> None:
    """Drop every process-wide cache the adapters keep between calls."""
    from storepilot.app_store import auth as asc_auth
    from storepilot.app_store import client as asc_client
    from storepilot.google_play import reporting

    asc_client.reset_client()
    asc_auth.reset_auth()
    reporting.reset_freshness_cache()
    # The Reporting API paces itself at 8 QPS; offline that is pure wall clock.
    reporting._throttle._min_interval = 0.0
    reporting._throttle._next_at = 0.0


@pytest.fixture
def fixture_dir() -> Path:
    """Directory holding the sample report files (real-shaped, UTF-16 CSVs)."""
    return Path(__file__).parent / "fixtures"
