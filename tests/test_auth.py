"""ES256 JWT minting, scoping, caching and key hygiene."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from release_guard.auth import (
    TOKEN_LIFETIME_S,
    AscTokenProvider,
    CredentialError,
    PrivateKey,
    ServerApiTokenProvider,
    running_under_test,
    scope_for,
)
from release_guard.logs import REDACTOR

KEY_ID = "TESTKEY123"
ISSUER = "11111111-2222-3333-4444-555555555555"


def _public(pem: str):
    return serialization.load_pem_private_key(pem.encode(), None).public_key()


def test_asc_token_has_apple_required_claims(key_file: Path, ec_key_pem: str) -> None:
    provider = AscTokenProvider(KEY_ID, ISSUER, PrivateKey(key_file, "asc"), scoped=False, clock=lambda: 1_000_000)
    token = provider.token_for("GET", "https://api.appstoreconnect.apple.com/v1/apps")
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    claims = jwt.decode(token, _public(ec_key_pem), algorithms=["ES256"], audience="appstoreconnect-v1",
                        options={"verify_exp": False})
    assert claims["iss"] == ISSUER
    assert claims["exp"] - claims["iat"] == TOKEN_LIFETIME_S <= 20 * 60
    assert "scope" not in claims


def test_get_tokens_are_scoped_to_one_request(key_file: Path, ec_key_pem: str) -> None:
    provider = AscTokenProvider(KEY_ID, ISSUER, PrivateKey(key_file, "asc"), scoped=True)
    url = ("https://api.appstoreconnect.apple.com/v1/builds?filter%5Bapp%5D=123&"
           "filter%5BpreReleaseVersion.version%5D=2.88&sort=-uploadedDate&limit=20&cursor=abc")
    claims = jwt.decode(provider.token_for("GET", url), _public(ec_key_pem), algorithms=["ES256"],
                        audience="appstoreconnect-v1")
    # limit, cursor and sort are ignored by Apple's scope matcher, so they are left out.
    assert claims["scope"] == ["GET /v1/builds?filter[app]=123&filter[preReleaseVersion.version]=2.88"]


def test_writes_never_get_a_scope_and_scope_helper_is_stable() -> None:
    assert scope_for("get", "https://x/v1/apps/1?limit=5") == "GET /v1/apps/1"
    assert scope_for("GET", "https://x/v1/apps?filter%5Bplatform%5D=IOS") == "GET /v1/apps?filter[platform]=IOS"


def test_non_get_token_is_unscoped(key_file: Path, ec_key_pem: str) -> None:
    provider = AscTokenProvider(KEY_ID, ISSUER, PrivateKey(key_file, "asc"), scoped=True)
    claims = jwt.decode(provider.token_for("PATCH", "https://api.appstoreconnect.apple.com/v1/builds/1"),
                        _public(ec_key_pem), algorithms=["ES256"], audience="appstoreconnect-v1")
    assert "scope" not in claims


def test_tokens_are_cached_then_refreshed_before_expiry(key_file: Path) -> None:
    now = [1_000_000.0]
    provider = AscTokenProvider(KEY_ID, ISSUER, PrivateKey(key_file, "asc"), scoped=False, clock=lambda: now[0])
    first = provider.token_for("GET", "https://a/v1/apps")
    now[0] += 60
    assert provider.token_for("GET", "https://a/v1/apps") == first
    now[0] += TOKEN_LIFETIME_S - 90  # inside the 60s refresh margin
    assert provider.token_for("GET", "https://a/v1/apps") != first


def test_server_api_token_carries_bundle_id(key_file: Path, ec_key_pem: str) -> None:
    provider = ServerApiTokenProvider("IAPKEY1234", ISSUER, "com.example.app", PrivateKey(key_file, "iap"))
    claims = jwt.decode(provider.token_for("POST", "https://api.storekit.apple.com/x"), _public(ec_key_pem),
                        algorithms=["ES256"], audience="appstoreconnect-v1")
    assert claims["bid"] == "com.example.app" and claims["iss"] == ISSUER


def test_private_key_never_shows_material(key_file: Path, ec_key_pem: str) -> None:
    key = PrivateKey(key_file, "asc")
    key.pem()
    assert "PRIVATE" not in repr(key) and "PRIVATE" not in str(key)
    assert "redacted" in repr(AscTokenProvider(KEY_ID, ISSUER, key))


def test_missing_and_malformed_keys_raise_safe_errors(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="not found"):
        PrivateKey(tmp_path / "missing.p8", "asc").pem()
    bad = tmp_path / "bad.p8"
    bad.write_text("not a key")
    with pytest.raises(CredentialError, match="not a PEM"):
        PrivateKey(bad, "asc").pem()


def test_world_readable_key_logs_a_warning(key_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    key_file.chmod(0o644)
    logger = logging.getLogger("release_guard")
    logger.addHandler(caplog.handler)
    try:
        PrivateKey(key_file, "asc").pem()
    finally:
        logger.removeHandler(caplog.handler)
    assert any(r.getMessage() == "key_permissions_too_open" for r in caplog.records)


def test_redactor_masks_jwts_pems_bearer_and_ids(key_file: Path, ec_key_pem: str) -> None:
    provider = AscTokenProvider(KEY_ID, ISSUER, PrivateKey(key_file, "asc"))
    token = provider.token_for("GET", "https://a/v1/apps")
    text = json.dumps({"auth": f"Bearer {token}", "pem": ec_key_pem, "note": f"kid {KEY_ID} iss {ISSUER}"})
    clean = REDACTOR.text(text)
    for secret in (token, KEY_ID, ISSUER, "BEGIN PRIVATE KEY"):
        assert secret not in clean


def test_running_under_test_is_true_in_pytest() -> None:
    assert running_under_test()
