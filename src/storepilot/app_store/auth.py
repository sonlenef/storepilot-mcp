"""App Store Connect authentication: ES256 JSON Web Tokens with auto-refresh.

Apple does not issue a long-lived access token. Every request carries a JWT that
the caller signs itself with the ``.p8`` private key downloaded from App Store
Connect -> Users and Access -> Integrations. Two properties of that scheme drive
this module:

1. **The token expires in 20 minutes, hard.** Apple rejects any token whose
   ``exp`` is more than 1200 seconds past ``iat``, and an MCP server session
   routinely outlives that. So a token is never minted once at startup — it is
   minted on demand and re-minted before it expires (see ``REFRESH_MARGIN``).
2. **The key material is a secret that must never surface.** The private key is
   held as bytes on a dataclass with ``repr=False``, never placed in an error
   message, never logged, and never written to the cache. Diagnostics report the
   key *id* (public information users must paste around anyway) and a redacted
   path.

Signing is ES256 (ECDSA over P-256 / secp256r1). RS256 keys, PKCS#1 EC keys, and
the ``AuthKey_*.p8`` files from the *other* Apple key programs (Apple Music, Push
Notifications, DeviceCheck) all fail here, and each gets a distinct remedy —
"invalid key" with no explanation is the single most common setup dead end.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storepilot.config import settings
from storepilot.core.errors import (
    DOCS_ASC_KEYS,
    CredentialsError,
    redact_path,
)

#: Apple's fixed audience claim. Any other value is rejected with 401.
ASC_AUDIENCE = "appstoreconnect-v1"

ASC_BASE_URL = "https://api.appstoreconnect.apple.com"

#: Apple's hard ceiling on ``exp - iat``. Tokens minted longer than this are
#: rejected outright (401 NOT_AUTHORIZED), not merely truncated.
MAX_TOKEN_LIFETIME = 1200

#: Lifetime we actually request. Equal to the ceiling: there is no benefit to a
#: shorter one, and the refresh margin below already keeps us clear of the edge.
TOKEN_LIFETIME = 1200

#: Re-mint once this many seconds remain. 300s means a token is used for at most
#: 15 minutes, leaving a wide margin for clock skew between us and Apple and for
#: a slow request that starts just before expiry.
REFRESH_MARGIN = 300

_ALGORITHM = "ES256"

#: Apple's marker for "this key is scoped to a single app and the token must
#: carry a ``bid`` (bundle id) claim". Surfaced as a distinct remedy because no
#: amount of retrying fixes it.
_BID_REQUIRED_MARKERS = (
    "bid",
    "bundle id",
    "bundleid",
    "individual",
)


@dataclass(frozen=True)
class AscCredentials:
    """Everything needed to sign a token, plus the safe-to-display identifiers.

    ``private_key_pem`` is excluded from ``repr`` so an accidental ``print``,
    f-string, or exception traceback holding this object cannot leak the key.
    """

    key_id: str
    issuer_id: str
    key_path: Path
    private_key_pem: bytes = field(repr=False)
    #: Set only for individual-scoped keys, which require a ``bid`` claim.
    bundle_id: str | None = None

    @property
    def display_path(self) -> str:
        return redact_path(self.key_path)

    def describe(self) -> str:
        """One-line summary safe to show a user or write to a diagnostic report."""
        scope = f", bundle {self.bundle_id}" if self.bundle_id else ""
        return f"key {self.key_id} (issuer {self.issuer_id}) from {self.display_path}{scope}"


# --- Loading ----------------------------------------------------------------


def _missing_config_error(missing: list[str]) -> CredentialsError:
    return CredentialsError(
        f"App Store Connect is not configured: {', '.join(missing)} unset.",
        remedy=(
            "In App Store Connect go to Users and Access -> Integrations -> App Store Connect "
            "API, create a team key with the Admin or App Manager role, and download the .p8 "
            "file (Apple lets you download it exactly once). Then set STOREPILOT_ASC_KEY_PATH "
            "to that file, STOREPILOT_ASC_KEY_ID to the Key ID column, and "
            "STOREPILOT_ASC_ISSUER_ID to the Issuer ID shown above the key table. Sales reports "
            "additionally need STOREPILOT_ASC_VENDOR_NUMBER from Payments and Financial Reports."
        ),
        doc_url=DOCS_ASC_KEYS,
        details={"missing": missing},
    )


def load_credentials(
    *,
    key_path: Path | None = None,
    key_id: str | None = None,
    issuer_id: str | None = None,
    bundle_id: str | None = None,
) -> AscCredentials:
    """Read and validate the ``.p8`` key and identifiers.

    Every failure mode gets its own remedy, because "could not load key" sends
    users in circles: a missing env var, a path typo, a wrong *kind* of Apple
    key, and a corrupted download all look identical otherwise.
    """
    key_path = key_path or settings.asc_key_path
    key_id = key_id or settings.asc_key_id
    issuer_id = issuer_id or settings.asc_issuer_id

    missing = [
        name
        for name, value in (
            ("STOREPILOT_ASC_KEY_PATH", key_path),
            ("STOREPILOT_ASC_KEY_ID", key_id),
            ("STOREPILOT_ASC_ISSUER_ID", issuer_id),
        )
        if not value
    ]
    if missing:
        raise _missing_config_error(missing)

    assert key_path is not None and key_id is not None and issuer_id is not None
    path = Path(key_path).expanduser()

    if not path.exists():
        raise CredentialsError(
            f"App Store Connect private key not found at {redact_path(path)}.",
            remedy=(
                "Check STOREPILOT_ASC_KEY_PATH points at the .p8 file you downloaded. Apple "
                "allows the download only once — if the file is lost, revoke the key in Users "
                "and Access -> Integrations and generate a new one."
            ),
            doc_url=DOCS_ASC_KEYS,
        )
    if path.is_dir():
        raise CredentialsError(
            f"STOREPILOT_ASC_KEY_PATH points at a directory, not a file: {redact_path(path)}.",
            remedy="Point it at the AuthKey_XXXXXXXXXX.p8 file itself.",
            doc_url=DOCS_ASC_KEYS,
        )

    try:
        pem = path.read_bytes()
    except OSError as exc:
        raise CredentialsError(
            f"Cannot read the App Store Connect key at {redact_path(path)}.",
            remedy=(
                "Check file permissions — the key must be readable by the user running the MCP "
                "server. `chmod 600` the file and confirm ownership."
            ),
            details={"os_error": str(exc)},
        ) from exc

    _validate_pem(pem, path)
    _validate_key_id(key_id, path)
    _validate_issuer_id(issuer_id)

    return AscCredentials(
        key_id=key_id.strip(),
        issuer_id=issuer_id.strip(),
        key_path=path,
        private_key_pem=pem,
        bundle_id=(bundle_id.strip() if bundle_id else None),
    )


def _validate_pem(pem: bytes, path: Path) -> None:
    """Confirm the file is a PKCS#8 EC P-256 private key, the only kind Apple issues here."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    head = pem[:200].lstrip()
    if not head.startswith(b"-----BEGIN"):
        raise CredentialsError(
            f"{redact_path(path)} does not look like a PEM private key.",
            remedy=(
                "The file must start with '-----BEGIN PRIVATE KEY-----'. A file that starts "
                "with '{' is a Google service account key (wrong store); an HTML file means the "
                "download was intercepted by a login page. Re-download the .p8 from App Store "
                "Connect -> Users and Access -> Integrations."
            ),
            doc_url=DOCS_ASC_KEYS,
        )
    if b"BEGIN RSA PRIVATE KEY" in head:
        raise CredentialsError(
            f"{redact_path(path)} is an RSA key; App Store Connect requires an EC P-256 key.",
            remedy=(
                "App Store Connect API keys are ES256/EC. An RSA key belongs to a different "
                "system (a certificate signing request or an SSH key). Download the correct "
                "AuthKey_<KEYID>.p8 from Users and Access -> Integrations."
            ),
            doc_url=DOCS_ASC_KEYS,
        )

    try:
        key = load_pem_private_key(pem, password=None)
    except TypeError as exc:
        raise CredentialsError(
            f"The private key at {redact_path(path)} is encrypted with a passphrase.",
            remedy=(
                "App Store Connect .p8 keys are never passphrase-protected, so this file has "
                "been re-encrypted locally. Use the original downloaded file, or decrypt it "
                "with: openssl pkcs8 -topk8 -nocrypt -in key.p8 -out key-plain.p8"
            ),
            doc_url=DOCS_ASC_KEYS,
            details={"key_error": type(exc).__name__},
        ) from exc
    except ValueError as exc:
        raise CredentialsError(
            f"The private key at {redact_path(path)} could not be parsed.",
            remedy=(
                "The file is truncated or was mangled by an editor (smart quotes, CRLF "
                "conversion, or a stray copy-paste). Re-download the .p8; if the one-time "
                "download is gone, revoke the key and generate a new one."
            ),
            doc_url=DOCS_ASC_KEYS,
            # str(exc) from cryptography describes the *format* failure only and
            # never echoes key bytes, so it is safe to surface.
            details={"key_error": str(exc)},
        ) from exc

    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise CredentialsError(
            f"{redact_path(path)} holds a {type(key).__name__}, not an EC key.",
            remedy=(
                "App Store Connect signs with ES256 over the P-256 curve. Confirm you "
                "downloaded the key from Users and Access -> Integrations -> App Store Connect "
                "API, and not from Keys (which issues Push Notification, MusicKit and "
                "DeviceCheck keys that look identical but do not work here)."
            ),
            doc_url=DOCS_ASC_KEYS,
        )
    if key.curve.name != "secp256r1":
        raise CredentialsError(
            f"{redact_path(path)} uses curve {key.curve.name}; ES256 requires secp256r1 (P-256).",
            remedy=(
                "Generate a fresh App Store Connect API key — Apple always issues P-256 for "
                "this API, so a different curve means the file is not an App Store Connect key."
            ),
            doc_url=DOCS_ASC_KEYS,
        )


def _validate_key_id(key_id: str, path: Path) -> None:
    """Catch the very common paste of a filename or a whole path into the key id."""
    cleaned = key_id.strip()
    if not cleaned:
        raise _missing_config_error(["STOREPILOT_ASC_KEY_ID"])
    if "/" in cleaned or cleaned.endswith(".p8"):
        raise CredentialsError(
            f"STOREPILOT_ASC_KEY_ID looks like a filename, not a key id: {cleaned!r}.",
            remedy=(
                "Use only the 10-character Key ID from the Key ID column, e.g. 'ABCD1234EF'. "
                f"It is also the XXXXXXXXXX in the filename AuthKey_XXXXXXXXXX.p8 — for your "
                f"file that is '{path.stem.removeprefix('AuthKey_')}'."
            ),
            doc_url=DOCS_ASC_KEYS,
        )


def _validate_issuer_id(issuer_id: str) -> None:
    """The issuer id is a UUID; a key id pasted here is the classic swap."""
    cleaned = issuer_id.strip()
    try:
        uuid.UUID(cleaned)
    except ValueError:
        raise CredentialsError(
            f"STOREPILOT_ASC_ISSUER_ID is not a UUID: {cleaned!r}.",
            remedy=(
                "The Issuer ID is a UUID like '57246542-96fe-1a63-e053-0824d011072a', shown "
                "once above the key table in Users and Access -> Integrations. It is easy to "
                "swap with the 10-character Key ID — check they are not reversed. The issuer "
                "id is per-team, so every key on your team shares it."
            ),
            doc_url=DOCS_ASC_KEYS,
        ) from None


# --- Token minting ----------------------------------------------------------


class TokenManager:
    """Mints and caches one ES256 token, re-minting it before Apple expires it.

    Thread-safe: MCP tool calls can overlap and a torn read of the cached token
    would send a half-rotated credential. ``lifetime`` and ``margin`` are
    injectable so tests can prove the refresh fires without waiting 15 minutes.
    """

    def __init__(
        self,
        credentials: AscCredentials,
        *,
        lifetime: int = TOKEN_LIFETIME,
        margin: int = REFRESH_MARGIN,
    ) -> None:
        if lifetime > MAX_TOKEN_LIFETIME:
            raise ValueError(
                f"App Store Connect rejects tokens longer than {MAX_TOKEN_LIFETIME}s; "
                f"got {lifetime}s."
            )
        if margin >= lifetime:
            raise ValueError("refresh margin must be shorter than the token lifetime")
        self.credentials = credentials
        self.lifetime = lifetime
        self.margin = margin
        self._lock = threading.Lock()
        self._token: str | None = None
        self._issued_at: int = 0
        self._expires_at: int = 0
        #: Count of tokens minted. Purely diagnostic; tests assert on it.
        self.mint_count = 0

    @property
    def expires_at(self) -> int:
        return self._expires_at

    def seconds_remaining(self, *, now: float | None = None) -> int:
        return int(self._expires_at - (now if now is not None else time.time()))

    def is_valid(self, *, now: float | None = None) -> bool:
        """Valid means "still usable", i.e. more than ``margin`` seconds left."""
        if self._token is None:
            return False
        current = now if now is not None else time.time()
        return current < (self._expires_at - self.margin)

    def token(self, *, now: float | None = None) -> str:
        """Return a currently-valid token, minting a fresh one if needed."""
        with self._lock:
            if not self.is_valid(now=now):
                self._mint(now=now)
            assert self._token is not None
            return self._token

    def auth_header(self, *, now: float | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(now=now)}"}

    def invalidate(self) -> None:
        """Drop the cached token so the next call mints a new one.

        Called after a 401: if the clock drifted or the key was revoked and
        re-added, a stale cached token would keep failing for up to 15 minutes.
        """
        with self._lock:
            self._token = None
            self._expires_at = 0

    def _mint(self, *, now: float | None = None) -> str:
        import jwt

        issued = int(now if now is not None else time.time())
        expires = issued + self.lifetime
        payload: dict[str, Any] = {
            "iss": self.credentials.issuer_id,
            "iat": issued,
            "exp": expires,
            "aud": ASC_AUDIENCE,
        }
        if self.credentials.bundle_id:
            # Individual-scoped (per-app) keys require the bundle id; team keys
            # must NOT carry it, so it is only added when explicitly configured.
            payload["bid"] = self.credentials.bundle_id

        headers = {"alg": _ALGORITHM, "kid": self.credentials.key_id, "typ": "JWT"}
        try:
            token = jwt.encode(
                payload,
                self.credentials.private_key_pem,
                algorithm=_ALGORITHM,
                headers=headers,
            )
        except Exception as exc:
            raise CredentialsError(
                "Failed to sign an App Store Connect token with the configured key.",
                remedy=(
                    "The .p8 file parsed but could not sign. Re-download the key from App "
                    "Store Connect -> Users and Access -> Integrations, and confirm PyJWT's "
                    "cryptography extra is installed (`pip install 'pyjwt[crypto]'`)."
                ),
                doc_url=DOCS_ASC_KEYS,
                details={"sign_error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        self._token = token
        self._issued_at = issued
        self._expires_at = expires
        self.mint_count += 1
        return token


def token_claims(token: str) -> dict[str, Any]:
    """Decode a token's claims WITHOUT verifying, for diagnostics only.

    Never used to make a trust decision — Apple verifies the signature. This
    exists so ``check_setup`` can show a user the exact ``iss``/``aud``/``exp``
    that was sent when Apple rejects it.
    """
    import jwt

    return jwt.decode(token, options={"verify_signature": False}, algorithms=[_ALGORITHM])


def token_header(token: str) -> dict[str, Any]:
    """Decode a token's JOSE header (alg/kid/typ) without verifying."""
    import jwt

    return jwt.get_unverified_header(token)


def rejected_token_error(detail: str, *, context: str) -> CredentialsError:
    """Build the error for a 401 from Apple, split by the reason Apple hints at.

    A rejected token is never worth retrying — the same key will produce the same
    rejection — so this always terminates the request rather than backing off.
    """
    lowered = detail.lower()
    if any(marker in lowered for marker in _BID_REQUIRED_MARKERS):
        return CredentialsError(
            f"App Store Connect rejected the token while {context}: it requires a bundle-id "
            f"('bid') claim.",
            remedy=(
                "The key is individual-scoped (limited to one app) rather than a team key. "
                "Either generate a Team Key in Users and Access -> Integrations -> App Store "
                "Connect API -> Team Keys, or configure the bundle id this key is scoped to so "
                "the token can carry the 'bid' claim."
            ),
            doc_url=DOCS_ASC_KEYS,
            details={"apple_detail": detail},
        )
    return CredentialsError(
        f"App Store Connect rejected the token while {context}.",
        remedy=(
            "Check, in order: (1) the Key ID matches the .p8 file, (2) the Issuer ID is the "
            "team UUID and has not been swapped with the Key ID, (3) the key is still active "
            "in Users and Access -> Integrations (revoked keys fail exactly like this), and "
            "(4) this machine's clock is accurate — tokens are valid for only 20 minutes and "
            "a clock skewed by more than that makes every token look expired. Run setup_doctor "
            "for a step-by-step check."
        ),
        doc_url=DOCS_ASC_KEYS,
        details={"apple_detail": detail} if detail else None,
    )


# --- Process-wide singleton -------------------------------------------------

_lock = threading.Lock()
_manager: TokenManager | None = None


def token_manager() -> TokenManager:
    """The shared token manager, built on first use.

    Shared deliberately: one token serves every tool call, and Apple's per-key
    rate limit is per *key* anyway, so minting extra tokens buys nothing.
    """
    global _manager
    with _lock:
        if _manager is None:
            _manager = TokenManager(load_credentials())
        return _manager


def reset_auth() -> None:
    """Drop the cached manager. Call after config changes or in tests."""
    global _manager
    with _lock:
        _manager = None
