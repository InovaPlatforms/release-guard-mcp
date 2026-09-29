"""Wires settings, credentials, HTTP clients and policy together for one server process."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .asc import AppStoreConnect
from .auth import AscTokenProvider, CredentialError, PrivateKey, ServerApiTokenProvider
from .config import SERVER_API_BASE_URLS, Settings
from .logs import REDACTOR
from .policy import Policy, load_policy
from .server_api import HISTORY_PATH, AppStoreServerApi
from .transport import ResilientClient


@dataclass
class Runtime:
    settings: Settings
    policy: Policy
    # Injected in tests and demo mode; None means real network.
    asc_transport: httpx.AsyncBaseTransport | None = None
    server_api_transport: httpx.AsyncBaseTransport | None = None
    # Injected in tests so retries do not really wait.
    sleep: Callable[[float], Awaitable[None]] | None = None
    _asc: AppStoreConnect | None = field(default=None, repr=False)
    _server_api: dict[str, AppStoreServerApi] = field(default_factory=dict, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Runtime:
        REDACTOR.register(*settings.secret_values())
        runtime = cls(settings=settings, policy=load_policy(settings.policy_path))
        if settings.backend == "demo":
            from .fake_asc import demo_transports

            runtime.asc_transport, runtime.server_api_transport = demo_transports()
        return runtime

    @property
    def offline(self) -> bool:
        return self.asc_transport is not None

    def mask(self, value: str | None) -> str | None:
        """Shorten Apple resource ids when RELEASE_GUARD_REDACT_IDS=1 (demos, screenshots)."""
        if value is None or not self.settings.redact_ids:
            return value
        return "…" + value[-4:] if len(value) > 6 else "…"

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"max_attempts": self.settings.max_attempts,
                                  "timeout_s": self.settings.request_timeout_s,
                                  "max_retry_after_s": self.settings.max_retry_after_s}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        return kwargs

    def asc(self) -> AppStoreConnect:
        if self._asc is None:
            s = self.settings
            missing = s.missing_asc_credentials()
            if missing and not self.offline:
                raise CredentialError("App Store Connect credentials are not configured. Set "
                                      + ", ".join(missing) + " in the MCP server environment.")
            tokens = None
            if not missing:
                assert s.private_key_path and s.key_id and s.issuer_id
                tokens = AscTokenProvider(s.key_id, s.issuer_id,
                                          PrivateKey(s.private_key_path, "App Store Connect API"),
                                          scoped=s.scoped_tokens)
            client = ResilientClient(s.asc_base_url, tokens, transport=self.asc_transport, **self._client_kwargs())
            self._asc = AppStoreConnect(client)
        return self._asc

    def server_api(self, environment: str) -> AppStoreServerApi:
        if environment not in self._server_api:
            s = self.settings
            missing = s.missing_server_api_credentials()
            if missing and not self.offline:
                raise CredentialError("App Store Server API credentials are not configured. Set "
                                      + ", ".join(missing) + " (an In-App Purchase key) in the MCP "
                                      "server environment.")
            tokens = None
            if not missing:
                assert s.iap_key_id and s.issuer_id and s.bundle_id and s.iap_key_path
                tokens = ServerApiTokenProvider(s.iap_key_id, s.issuer_id, s.bundle_id,
                                                PrivateKey(s.iap_key_path, "In-App Purchase"))
            client = ResilientClient(SERVER_API_BASE_URLS[environment], tokens,
                                     transport=self.server_api_transport, safe_post_paths=(HISTORY_PATH,),
                                     **self._client_kwargs())
            self._server_api[environment] = AppStoreServerApi(client)
        return self._server_api[environment]

    def clients(self) -> list[ResilientClient]:
        out = [api.client for api in self._server_api.values()]
        if self._asc:
            out.append(self._asc.client)
        return out
