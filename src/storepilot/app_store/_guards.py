"""The ONE place this adapter touches the shared write-guard framework.

``storepilot.core.guards`` owns the policy: HMAC-keyed confirmation tokens, the
replay ledger, the TTL, and the audit log. This module only adapts it to App
Store Connect — it fixes the store name, supplies the ``Operation`` shape the
tools build, and re-exports the few types they need. Nothing in ``tools.py``
imports ``core.guards`` directly, so a change to the shared interface is a change
to this file alone.

**Why the token is not just a hash of the content.** An earlier local fallback
here derived the token as ``sha256(tool|action|target|preview)``. That is
computable by the caller, and the caller is a language model: it could mint its
own token and confirm its own write without ever rendering the preview into the
chat where a human could object. The preview — not the token — is the safety
mechanism, and a self-computable token lets the model skip it. ``core.guards``
keys the HMAC with a per-install secret at ``~/.storepilot/guard.key`` (0600)
that never appears in tool output, so possessing a valid token *proves* a real
preview call happened. This module therefore never computes a token itself.

**Flow.** :func:`gate` returns the preview text when the operation is not yet
authorised — the tool returns that string verbatim and mutates nothing. It
returns ``None`` when the tool may proceed. A bad, expired, replayed or drifted
token raises ``ValidationError``, which the tool wrapper already renders.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from storepilot.core.guards import (
    UNTRUSTED_CONTENT_NOTE,
    AuditRecorder,
    Change,
    Operation,
    Preview,
    append_warning,
    audit,
    audit_execution,
    audit_log_path,
    audit_warning,
    require_confirmation,
    target_for,
    untrusted,
)
from storepilot.core.models import Store

#: Store label used in every ``Operation.target`` this adapter builds, so audit
#: entries and cross-store operations sort together.
STORE = Store.APP_STORE.value

__all__ = [
    "STORE",
    "UNTRUSTED_CONTENT_NOTE",
    "AuditRecorder",
    "Change",
    "Operation",
    "Preview",
    "audit_log_path",
    "audit_warning",
    "executing",
    "gate",
    "operation",
    "rejected",
    "untrusted",
    "with_warning",
]


def operation(
    tool: str,
    *,
    app_id: str,
    params: dict[str, Any],
    call_args: dict[str, Any],
) -> Operation:
    """Build the ``Operation`` that a confirmation token will be bound to.

    ``params`` carries everything *material* — including values the tool derived
    rather than received, such as the resolved version id or the exact text that
    will be published. That is what makes the token content-bound: if the model
    previews one reply and then confirms a different one, the fingerprint moves
    and the token stops verifying.

    ``call_args`` is the literal argument list, which the framework echoes back
    in the preview so the model knows exactly how to re-call the tool. Derived
    values must stay out of it — they are not valid keyword arguments.
    """
    return Operation(
        tool=tool,
        target=target_for(STORE, app_id),
        params=params,
        call_args=call_args,
    )


def gate(
    op: Operation,
    build_preview: Callable[[], Preview] | Preview,
    *,
    confirm: bool,
    confirmation_token: str | None,
    token_required: bool = True,
) -> str | None:
    """Return the preview text to show the user, or ``None`` to proceed.

    Every App Store write uses ``token_required=True``. The framework offers a
    lighter, token-free gate for low-blast-radius writes, but nothing this
    adapter does qualifies: a review reply is published publicly under the
    developer's name and Apple notifies the reviewer, and a submission starts a
    review cycle that costs days to unwind.
    """
    return require_confirmation(
        op,
        build_preview,
        confirm=confirm,
        confirmation_token=confirmation_token,
        token_required=token_required,
    )


def executing(op: Operation) -> AbstractContextManager[AuditRecorder]:
    """Wrap the real mutation so both success and failure reach the audit log.

    A failed write is the most important entry: it is the case where the user
    cannot tell whether anything landed.
    """
    return audit_execution(op)


def rejected(op: Operation, reason: str) -> None:
    """Record an operation refused by a local check, before any request went out."""
    audit(op, outcome="blocked", detail=reason)


def with_warning(text: str) -> str:
    """Append the framework's degraded-bookkeeping banner, when there is one."""
    return append_warning(text)
