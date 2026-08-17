"""App Store Connect JWTs.

Apple gives out no access token: every request carries a token this process signs
itself, valid for at most 20 minutes. Getting any claim wrong produces a 401 that
says nothing useful, so each property is asserted directly — including verifying
the signature against a throwaway P-256 key generated here in the fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from storepilot.app_store.auth import (
    ASC_AUDIENCE,
    MAX_TOKEN_LIFETIME,
    REFRESH_MARGIN,
    TOKEN_LIFETIME,
    TokenManager,
    load_credentials,
    rejected_token_error,
    token_claims,
    token_header,
)
from storepilot.core.errors import CredentialsError
from tests.support.asc import (
    ISSUER_ID,
    KEY_ID,
    generate_ec_key,
    make_credentials,
    public_key_pem,
)


@pytest.fixture
def credentials(tmp_path: Path):
    return make_credentials(tmp_path)


# --- Claims and signature ----------------------------------------------------


def test_token_carries_the_claims_apple_requires(credentials) -> None:
    token = TokenManager(credentials).token(now=1_800_000_000)

    header = token_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == KEY_ID
    assert header["typ"] == "JWT"

    claims = token_claims(token)
    assert claims["iss"] == ISSUER_ID
    assert claims["aud"] == ASC_AUDIENCE == "appstoreconnect-v1"
    assert claims["iat"] == 1_800_000_000
    assert claims["exp"] - claims["iat"] <= MAX_TOKEN_LIFETIME
    assert claims["exp"] - claims["iat"] == TOKEN_LIFETIME


def test_token_signature_verifies_against_the_public_key(credentials, tmp_path: Path) -> None:
    import jwt

    token = TokenManager(credentials).token()
    decoded = jwt.decode(
        token,
        public_key_pem(credentials.private_key_pem),
        algorithms=["ES256"],
        audience=ASC_AUDIENCE,
    )
    assert decoded["iss"] == ISSUER_ID

    # A different P-256 key must not verify it — otherwise the signature proves nothing.
    stranger = public_key_pem(generate_ec_key(tmp_path / "stranger.p8"))
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, stranger, algorithms=["ES256"], audience=ASC_AUDIENCE)


def test_team_keys_carry_no_bid_claim_and_individual_keys_do(tmp_path: Path) -> None:
    team = TokenManager(make_credentials(tmp_path / "team")).token()
    assert "bid" not in token_claims(team)

    scoped = make_credentials(tmp_path / "scoped", bundle_id="com.acme.todo")
    assert token_claims(TokenManager(scoped).token())["bid"] == "com.acme.todo"


# --- Refresh -----------------------------------------------------------------


def test_token_is_reused_until_the_refresh_margin_then_reminted(credentials) -> None:
    manager = TokenManager(credentials, lifetime=1200, margin=300)
    start = 1_800_000_000.0

    first = manager.token(now=start)
    assert manager.mint_count == 1

    # Still comfortably valid: the same token comes back.
    assert manager.token(now=start + 600) == first
    assert manager.mint_count == 1

    # One second before the margin opens, still the same token.
    assert manager.token(now=start + 1200 - 301) == first
    assert manager.mint_count == 1

    # Inside the margin — re-minted BEFORE Apple would reject it.
    second = manager.token(now=start + 1200 - 299)
    assert manager.mint_count == 2
    assert second != first
    assert manager.seconds_remaining(now=start + 1200 - 299) > 0


def test_expiry_is_never_beyond_apples_ceiling(credentials) -> None:
    with pytest.raises(ValueError, match="rejects tokens longer than"):
        TokenManager(credentials, lifetime=MAX_TOKEN_LIFETIME + 1)
    with pytest.raises(ValueError, match="margin must be shorter"):
        TokenManager(credentials, lifetime=600, margin=600)
    assert REFRESH_MARGIN < TOKEN_LIFETIME


def test_invalidate_forces_a_fresh_mint(credentials) -> None:
    manager = TokenManager(credentials)
    manager.token(now=1_800_000_000)
    manager.invalidate()
    assert manager.is_valid(now=1_800_000_000) is False
    manager.token(now=1_800_000_000)
    assert manager.mint_count == 2


def test_auth_header_shape(credentials) -> None:
    header = TokenManager(credentials).auth_header()
    assert set(header) == {"Authorization"}
    assert header["Authorization"].startswith("Bearer ey")


# --- The key material must never surface -------------------------------------


def test_private_key_is_not_in_the_credentials_repr(credentials) -> None:
    text = repr(credentials)
    assert "PRIVATE KEY" not in text
    assert credentials.private_key_pem.decode() not in text
    assert KEY_ID in text, "the key id is public and useful in diagnostics"


def test_describe_is_safe_to_show_a_user(credentials) -> None:
    described = credentials.describe()
    assert KEY_ID in described and ISSUER_ID in described
    assert "PRIVATE KEY" not in described
    assert str(credentials.key_path.parent.parent) not in described


# --- Setup failures each get their own remedy --------------------------------


def test_missing_configuration_names_every_missing_variable() -> None:
    with pytest.raises(CredentialsError) as excinfo:
        load_credentials()
    assert set(excinfo.value.details["missing"]) == {
        "STOREPILOT_ASC_KEY_PATH",
        "STOREPILOT_ASC_KEY_ID",
        "STOREPILOT_ASC_ISSUER_ID",
    }
    assert "Users and Access -> Integrations" in excinfo.value.remedy


def test_a_missing_key_file_is_distinguished_from_a_bad_one(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match="not found"):
        load_credentials(
            key_path=tmp_path / "nope.p8", key_id=KEY_ID, issuer_id=ISSUER_ID
        )


def test_a_google_service_account_json_is_recognised_as_the_wrong_store(
    tmp_path: Path,
) -> None:
    wrong = tmp_path / "AuthKey_ABCD1234EF.p8"
    wrong.write_text('{"type": "service_account", "private_key": "..."}')
    with pytest.raises(CredentialsError) as excinfo:
        load_credentials(key_path=wrong, key_id=KEY_ID, issuer_id=ISSUER_ID)
    assert "does not look like a PEM private key" in excinfo.value.message
    assert "Google service account key (wrong store)" in excinfo.value.remedy


def test_an_rsa_key_is_refused_with_the_es256_explanation(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "rsa.p8"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(CredentialsError, match="is an RSA key"):
        load_credentials(key_path=path, key_id=KEY_ID, issuer_id=ISSUER_ID)


def test_a_non_p256_curve_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p384.p8"
    generate_ec_key(path, curve="secp384r1")
    with pytest.raises(CredentialsError) as excinfo:
        load_credentials(key_path=path, key_id=KEY_ID, issuer_id=ISSUER_ID)
    assert "secp256r1" in excinfo.value.message


def test_a_truncated_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.p8"
    pem = generate_ec_key(tmp_path / "good.p8")
    path.write_bytes(pem[: len(pem) // 2])
    with pytest.raises(CredentialsError, match="could not be parsed"):
        load_credentials(key_path=path, key_id=KEY_ID, issuer_id=ISSUER_ID)


def test_a_filename_pasted_into_the_key_id_is_caught(tmp_path: Path) -> None:
    path = tmp_path / f"AuthKey_{KEY_ID}.p8"
    generate_ec_key(path)
    with pytest.raises(CredentialsError) as excinfo:
        load_credentials(
            key_path=path, key_id=f"AuthKey_{KEY_ID}.p8", issuer_id=ISSUER_ID
        )
    assert KEY_ID in excinfo.value.remedy, "the remedy should show the id derived from the filename"


def test_key_id_and_issuer_id_swapped_is_caught(tmp_path: Path) -> None:
    """The classic paste error: they sit next to each other in the console."""
    path = tmp_path / f"AuthKey_{KEY_ID}.p8"
    generate_ec_key(path)
    with pytest.raises(CredentialsError) as excinfo:
        load_credentials(key_path=path, key_id=ISSUER_ID, issuer_id=KEY_ID)
    assert "not a UUID" in excinfo.value.message
    assert "check they are not reversed" in excinfo.value.remedy


def test_a_directory_instead_of_the_p8_is_caught(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match="directory, not a file"):
        load_credentials(key_path=tmp_path, key_id=KEY_ID, issuer_id=ISSUER_ID)


# --- 401 classification ------------------------------------------------------


def test_a_bundle_id_scoped_key_gets_its_own_remedy() -> None:
    error = rejected_token_error(
        "The token must include a 'bid' claim for individual keys", context="listing apps"
    )
    assert "bundle-id" in error.message
    assert "Team Key" in error.remedy


def test_a_generic_401_walks_the_user_through_the_usual_causes() -> None:
    error = rejected_token_error("NOT_AUTHORIZED", context="listing apps")
    for clue in ("Key ID", "Issuer ID", "revoked", "clock"):
        assert clue in error.remedy
