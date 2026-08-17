"""The confirmation gate: what stands between a language model and a live release.

Every test here is a scenario an LLM actually produces — inventing a token,
re-using yesterday's, "helpfully" nudging a parameter between the preview and the
confirmation, retrying a call that already succeeded.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from storepilot.core.errors import ValidationError
from storepilot.core.guards import (
    Change,
    Operation,
    Preview,
    audit_log_path,
    issue_token,
    require_confirmation,
    verify_token,
)

TOOL = "play_promote_release"


def make_op(**overrides: object) -> Operation:
    params: dict[str, object] = {
        "package_name": "com.acme.todo",
        "from_track": "beta",
        "to_track": "production",
        "status": "inProgress",
        "user_fraction": 0.1,
    }
    params.update(overrides)
    return Operation(
        tool=TOOL,
        target="google_play:com.acme.todo",
        params=params,
        call_args=dict(params),
    )


def make_preview() -> Preview:
    return Preview(
        summary="Promote build 4501 from beta to production at 10%",
        changes=[Change("production release", before="4.1.0", after="4.2.0")],
        reversal="play_halt_rollout stops it immediately.",
    )


def token_from_preview(text: str) -> str:
    """Pull the token out of the rendered preview the way a caller would."""
    for line in text.splitlines():
        if "confirmation_token=" in line:
            return line.split('"')[1]
    raise AssertionError(f"no confirmation_token in preview:\n{text}")


# --- The preview leg ---------------------------------------------------------


def test_preview_returns_text_and_mutates_nothing() -> None:
    op = make_op()
    mutations: list[str] = []

    def build() -> Preview:
        return make_preview()

    out = require_confirmation(op, build, confirm=False)

    assert out is not None, "preview leg must return text, never authorise the call"
    assert "CONFIRMATION REQUIRED" in out
    assert "nothing has been changed yet" in out
    assert mutations == []
    # The preview must carry everything a human needs to say no.
    assert op.target in out
    assert "4.1.0" in out and "4.2.0" in out
    assert "play_halt_rollout" in out


def test_preview_expensive_build_is_only_paid_on_the_preview_leg() -> None:
    op = make_op()
    calls: list[int] = []

    def build() -> Preview:
        calls.append(1)
        return make_preview()

    require_confirmation(op, build, confirm=False)
    assert calls == [1]

    token = token_from_preview(require_confirmation(op, build, confirm=False) or "")
    calls.clear()
    assert require_confirmation(op, build, confirm=True, confirmation_token=token) is None
    assert calls == [], "the confirm leg must not re-run the dry run"


# --- Rejections --------------------------------------------------------------


def test_confirm_without_a_token_is_rejected() -> None:
    op = make_op()
    with pytest.raises(ValidationError) as excinfo:
        require_confirmation(op, make_preview(), confirm=True)
    assert "no confirmation_token" in excinfo.value.message
    assert "confirm=False first" in excinfo.value.remedy
    assert "Do not invent a token" in excinfo.value.remedy


def test_token_issued_for_a_different_operation_is_rejected() -> None:
    other = make_op(package_name="com.acme.OTHER")
    token = issue_token(other.fingerprint())

    with pytest.raises(ValidationError) as excinfo:
        verify_token(token, make_op().fingerprint(), tool=TOOL)
    assert "does not match these arguments" in excinfo.value.message


def test_token_is_rejected_when_a_parameter_drifted() -> None:
    previewed = make_op(user_fraction=0.1)
    token = issue_token(previewed.fingerprint())
    drifted = make_op(user_fraction=0.5)

    with pytest.raises(ValidationError) as excinfo:
        verify_token(token, drifted.fingerprint(), tool=TOOL)
    assert "an argument changed" in excinfo.value.remedy


def test_float_drift_below_representation_noise_is_not_drift() -> None:
    """0.1 and 0.10000000000000001 are the same rollout; 0.1 and 0.11 are not."""
    token = issue_token(make_op(user_fraction=0.1).fingerprint())
    verify_token(token, make_op(user_fraction=0.10000000000000001).fingerprint(), tool=TOOL)

    token = issue_token(make_op(user_fraction=0.1).fingerprint())
    with pytest.raises(ValidationError):
        verify_token(token, make_op(user_fraction=0.11).fingerprint(), tool=TOOL)


def test_expired_token_is_rejected() -> None:
    op = make_op()
    stale = issue_token(op.fingerprint(), ttl_seconds=-5)

    with pytest.raises(ValidationError) as excinfo:
        verify_token(stale, op.fingerprint(), tool=TOOL)
    assert "expired" in excinfo.value.message
    assert "stale approval cannot be replayed" in excinfo.value.remedy


def test_replaying_a_spent_token_is_rejected() -> None:
    op = make_op()
    token = issue_token(op.fingerprint())

    verify_token(token, op.fingerprint(), tool=TOOL)  # first use: fine
    with pytest.raises(ValidationError) as excinfo:
        verify_token(token, op.fingerprint(), tool=TOOL)
    assert "already been used" in excinfo.value.message


def test_a_model_computable_plain_hash_is_rejected() -> None:
    """The whole point of keying the MAC.

    An unkeyed digest over the same payload is something the model can compute
    from data it already has, which would let it skip the human entirely.
    """
    op = make_op()
    expiry = int(time.time() + 600)
    nonce = "0123456789abcdef"
    plain = hashlib.sha256(f"{op.fingerprint()}|{expiry}|{nonce}".encode()).hexdigest()[:32]
    forged = f"sp1.{expiry}.{nonce}.{plain}"

    with pytest.raises(ValidationError) as excinfo:
        verify_token(forged, op.fingerprint(), tool=TOOL)
    assert "does not match these arguments" in excinfo.value.message


@pytest.mark.parametrize(
    "token",
    [
        "yes",
        "sp1.deadbeef",
        "sp2.9999999999.abcd.0123456789abcdef0123456789abcdef",
        "sp1.notanumber.abcd.0123456789abcdef0123456789abcdef",
        "",
        "   ",
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(ValidationError):
        verify_token(token, make_op().fingerprint(), tool=TOOL)


def test_token_does_not_reveal_the_fingerprint() -> None:
    """A caller cannot edit a token to match a different operation."""
    op = make_op()
    token = issue_token(op.fingerprint())
    assert op.fingerprint() not in token
    assert op.params["package_name"] not in token


# --- The happy path ----------------------------------------------------------


def test_confirming_with_the_previewed_token_authorises_exactly_once() -> None:
    op = make_op()
    preview_text = require_confirmation(op, make_preview(), confirm=False)
    assert preview_text is not None
    token = token_from_preview(preview_text)

    assert require_confirmation(op, make_preview(), confirm=True, confirmation_token=token) is None

    with pytest.raises(ValidationError):
        require_confirmation(op, make_preview(), confirm=True, confirmation_token=token)


def test_token_not_required_gate_still_previews_and_still_audits() -> None:
    """The lighter gate used by low-blast-radius writes (a review reply)."""
    op = Operation(tool="play_reply_review", target="google_play:com.acme.todo", params={"id": "r1"})
    text = require_confirmation(op, make_preview(), confirm=False, token_required=False)
    assert text is not None
    assert "confirmation_token" not in text
    assert "confirm=True" in text

    assert (
        require_confirmation(op, make_preview(), confirm=True, token_required=False) is None
    )
    assert _audit_outcomes() == ["preview", "confirmed"]


# --- Audit trail -------------------------------------------------------------


def _audit_records() -> list[dict[str, object]]:
    path: Path = audit_log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _audit_outcomes() -> list[str]:
    return [str(record["outcome"]) for record in _audit_records()]


def test_audit_records_preview_rejected_and_confirmed() -> None:
    op = make_op()
    preview_text = require_confirmation(op, make_preview(), confirm=False)
    assert preview_text is not None

    with pytest.raises(ValidationError):
        require_confirmation(op, make_preview(), confirm=True, confirmation_token="sp1.bogus")

    token = token_from_preview(preview_text)
    require_confirmation(op, make_preview(), confirm=True, confirmation_token=token)

    assert _audit_outcomes() == ["preview", "rejected", "confirmed"]
    for record in _audit_records():
        assert record["tool"] == TOOL
        assert record["target"] == "google_play:com.acme.todo"
        assert record["fingerprint"] == op.fingerprint()[:16]


def test_audit_log_contains_no_secrets() -> None:
    secret_token = issue_token(make_op().fingerprint())
    op = Operation(
        tool="play_update_listing",
        target="google_play:com.acme.todo",
        params={
            "package_name": "com.acme.todo",
            "confirmation_token": secret_token,
            "asc_private_key": "-----BEGIN PRIVATE KEY-----MIGHAgEA-----END PRIVATE KEY-----",
            "api_key": "AIzaSyTOTALLYSECRET",
            "password": "hunter2",
            "issuer_id": "57246542-96fe-1a63-e053-0824d011072a",
            "title": "Acme Todo",
        },
    )
    require_confirmation(op, make_preview(), confirm=False)

    raw = audit_log_path().read_text(encoding="utf-8")
    for secret in (
        secret_token,
        "BEGIN PRIVATE KEY",
        "AIzaSyTOTALLYSECRET",
        "hunter2",
        "57246542-96fe-1a63-e053-0824d011072a",
    ):
        assert secret not in raw, f"{secret!r} leaked into the audit log"

    params = _audit_records()[0]["params"]
    assert isinstance(params, dict)
    assert params["title"] == "Acme Todo", "non-secret params must stay readable"
    for key in ("confirmation_token", "asc_private_key", "api_key", "password", "issuer_id"):
        assert params[key] == "<redacted>"


def test_audit_execution_records_success_and_failure() -> None:
    from storepilot.core.guards import audit_execution, unguarded

    op = make_op()
    with audit_execution(op) as recorder:
        recorder.note("promoted 4501")
        recorder.set("version_code", 4501)

    with pytest.raises(RuntimeError), audit_execution(op):
        raise RuntimeError("Google said no")

    records = _audit_records()
    assert [r["outcome"] for r in records] == ["executed", "failed"]
    assert records[0]["detail"] == "promoted 4501"
    assert records[0]["extra"]["version_code"] == 4501  # type: ignore[index]
    assert "Google said no" in str(records[1]["detail"])

    halt = Operation(tool="play_halt_rollout", target="google_play:com.acme.todo")
    with unguarded(halt, reason="halting a rollout makes things safer"):
        pass
    assert _audit_outcomes()[-2:] == ["immediate", "executed"]


def test_audit_survives_an_unwritable_log_and_says_so() -> None:
    """Losing the audit trail must not break a write, but must never be silent."""
    from storepilot.core import guards

    unwritable = Path("/dev/null/nope/audit.log")
    original = guards.audit_log_path
    guards.audit_log_path = lambda: unwritable  # type: ignore[assignment]
    try:
        guards.audit(make_op(), outcome="preview", detail="whatever")
    finally:
        guards.audit_log_path = original  # type: ignore[assignment]

    warning = guards.audit_warning()
    assert warning is not None
    assert "Guard bookkeeping degraded" in warning
    assert "audit log not writable" in warning
    assert guards.append_warning("body").startswith("body\n\n! Guard bookkeeping degraded")
