"""ES256 JWTs for the App Store Connect API and the App Store Server API.

Key handling rules (see docs/SECURITY.md):
- The .p8 key is read from a path given in the environment. It is never
  hardcoded, never logged, never returned from a tool, and never part of repr().
- Tokens live 15 minutes (Apple's ceiling is 20) and are cached in memory only.
- For GET requests, tokens are scoped by default: the `scope` claim names the one
  request the token may be used for, so a leaked token cannot read anything else
  and can never write.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import jwt

from .logs import REDACTOR, log

ASC_AUDIENCE = "appstoreconnect-v1"
TOKEN_LIFETIME_S = 15 * 60  # Apple rejects tokens that live longer than 20 minutes.
REFRESH_MARGIN_S = 60
# Apple ignores these query parameters when it matches a token's scope.
SCOPE_IGNORED_PARAMS = {"limit", "cursor", "sort"}


class CredentialError(RuntimeError):
    """Configuration problem the user must fix (message is safe to show)."""


class PrivateKey:
    """A .p8 key loaded on first use. repr() and str() never show the material."""

    def __init__(self, path: Path, label: str) -> None:
        self._path = path
        self._label = label
        self._pem: str | None = None

    def __repr__(self) -> str:
        return f"PrivateKey(label={self._label!r}, material=<redacted>)"

    __str__ = __repr__

    @property
    def path(self) -> Path:
        return self._path

    def pem(self) -> str:
        if self._pem is None:
            if not self._path.is_file():
                raise CredentialError(f"{self._label} key file not found at the configured path")
            mode = self._path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                log("key_permissions_too_open", level=30, key_label=self._label,
                    hint="chmod 600 the .p8 file; it is readable by other users")
            text = self._path.read_text()
            if "PRIVATE KEY" not in text:
                raise CredentialError(f"{self._label} key file is not a PEM private key")
            REDACTOR.register(text.strip())
            self._pem = text
        return self._pem


def scope_for(method: str, url: str) -> str:
    """Apple scope string for one request, e.g. 'GET /v1/builds?filter[app]=123'."""
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in SCOPE_IGNORED_PARAMS]
    query = "&".join(f"{k}={v}" for k, v in pairs)
    path = unquote(parts.path)
    return f"{method.upper()} {path}" + (f"?{query}" if query else "")


@dataclass
class _Cached:
    token: str
    expires_at: float


class AscTokenProvider:
    """Mints App Store Connect API team-key tokens."""

    def __init__(self, key_id: str, issuer_id: str, key: PrivateKey, *, scoped: bool = True,
                 clock: Callable[[], float] = time.time) -> None:
        self._key_id = key_id
        self._issuer_id = issuer_id
        self._key = key
        self._scoped = scoped
        self._clock = clock
        self._cache: dict[str, _Cached] = {}
        REDACTOR.register(key_id, issuer_id)

    def __repr__(self) -> str:
        return "AscTokenProvider(<redacted>)"

    def token_for(self, method: str, url: str) -> str:
        scope = scope_for(method, url) if (self._scoped and method.upper() == "GET") else ""
        now = self._clock()
        cached = self._cache.get(scope)
        if cached and cached.expires_at - REFRESH_MARGIN_S > now:
            return cached.token
        iat = int(now)
        payload: dict[str, object] = {"iss": self._issuer_id, "iat": iat,
                                      "exp": iat + TOKEN_LIFETIME_S, "aud": ASC_AUDIENCE}
        if scope:
            payload["scope"] = [scope]
        token = jwt.encode(payload, self._key.pem(), algorithm="ES256",
                           headers={"kid": self._key_id, "typ": "JWT"})
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[scope] = _Cached(token, iat + TOKEN_LIFETIME_S)
        return token


class ServerApiTokenProvider:
    """Mints App Store Server API tokens (In-App Purchase key, `bid` claim)."""

    def __init__(self, key_id: str, issuer_id: str, bundle_id: str, key: PrivateKey,
                 clock: Callable[[], float] = time.time) -> None:
        self._key_id = key_id
        self._issuer_id = issuer_id
        self._bundle_id = bundle_id
        self._key = key
        self._clock = clock
        self._cached: _Cached | None = None
        REDACTOR.register(key_id, issuer_id)

    def __repr__(self) -> str:
        return "ServerApiTokenProvider(<redacted>)"

    def token_for(self, method: str, url: str) -> str:
        now = self._clock()
        if self._cached and self._cached.expires_at - REFRESH_MARGIN_S > now:
            return self._cached.token
        iat = int(now)
        payload = {"iss": self._issuer_id, "iat": iat, "exp": iat + TOKEN_LIFETIME_S,
                   "aud": ASC_AUDIENCE, "bid": self._bundle_id}
        token = jwt.encode(payload, self._key.pem(), algorithm="ES256",
                           headers={"kid": self._key_id, "typ": "JWT"})
        self._cached = _Cached(token, iat + TOKEN_LIFETIME_S)
        return token


def running_under_test() -> bool:
    """True inside pytest. The write path refuses to run here, whatever the flags say."""
    return "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("RELEASE_GUARD_UNDER_TEST") == "1"
