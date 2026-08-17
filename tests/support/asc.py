"""Throwaway App Store Connect credentials and canned HTTP responses.

The EC P-256 key is generated per test, so the suite signs and verifies real
ES256 tokens without any Apple key existing on the machine. Responses come from
``httpx.MockTransport``; nothing here opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

ISSUER_ID = "57246542-96fe-1a63-e053-0824d011072a"
KEY_ID = "ABCD1234EF"


def generate_ec_key(path: Path, *, curve: str = "secp256r1") -> bytes:
    """Write a PKCS#8 PEM EC private key and return its bytes."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    curves = {"secp256r1": ec.SECP256R1(), "secp384r1": ec.SECP384R1()}
    key = ec.generate_private_key(curves[curve])
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    return pem


def public_key_pem(private_pem: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_pem, password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def make_credentials(tmp_path: Path, *, bundle_id: str | None = None) -> Any:
    """Real ``AscCredentials`` over a freshly generated key."""
    from storepilot.app_store.auth import load_credentials

    key_path = tmp_path / f"AuthKey_{KEY_ID}.p8"
    generate_ec_key(key_path)
    return load_credentials(
        key_path=key_path, key_id=KEY_ID, issuer_id=ISSUER_ID, bundle_id=bundle_id
    )


# --- Response helpers --------------------------------------------------------


def json_response(
    payload: dict[str, Any],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers={"content-type": "application/json", **(headers or {})},
    )


def apple_error(
    status: int,
    *,
    code: str = "ENTITY_ERROR",
    title: str = "An error occurred",
    detail: str = "",
    pointer: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    error: dict[str, Any] = {"status": str(status), "code": code, "title": title}
    if detail:
        error["detail"] = detail
    if pointer:
        error["source"] = {"pointer": pointer}
    return json_response({"errors": [error]}, status=status, headers=headers)


def resource(resource_type: str, resource_id: str, **attributes: Any) -> dict[str, Any]:
    return {"type": resource_type, "id": resource_id, "attributes": attributes}


def sequence_transport(
    responses: Sequence[httpx.Response],
    *,
    log: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Answer requests from a fixed list, in order. The last one repeats."""
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return httpx.MockTransport(handler)


def routed_transport(
    routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]],
    *,
    log: list[httpx.Request] | None = None,
    default: httpx.Response | None = None,
) -> httpx.MockTransport:
    """Answer by URL path (first matching prefix wins)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request)
        for prefix, response in routes.items():
            if request.url.path.startswith(prefix):
                return response(request) if callable(response) else response
        if default is not None:
            return default
        return apple_error(404, code="NOT_FOUND", detail=f"no route for {request.url.path}")

    return httpx.MockTransport(handler)


def tsv(rows: Sequence[Sequence[str]]) -> bytes:
    """Apple's sales reports are TSV, not JSON."""
    return ("\n".join("\t".join(cell for cell in row) for row in rows) + "\n").encode("utf-8")


def gzipped(data: bytes) -> bytes:
    import gzip

    return gzip.compress(data)


def dump(payload: Any) -> str:
    return json.dumps(payload)
