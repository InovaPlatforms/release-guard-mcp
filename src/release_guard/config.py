"""Runtime settings, read from the environment the MCP host passes to the server.

Only paths and identifiers live here. Key material is loaded lazily by
`release_guard.auth` and never stored on this object.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Backend = Literal["live", "demo"]

ASC_BASE_URL = "https://api.appstoreconnect.apple.com"
SERVER_API_BASE_URLS = {
    "production": "https://api.storekit.apple.com",
    "sandbox": "https://api.storekit-sandbox.apple.com",
}

_TRUE = {"1", "true", "yes", "on"}


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _path(env: Mapping[str, str], name: str) -> Path | None:
    raw = (env.get(name) or "").strip()
    return Path(raw).expanduser() if raw else None


def _str(env: Mapping[str, str], name: str) -> str | None:
    raw = (env.get(name) or "").strip()
    return raw or None


@dataclass(frozen=True)
class Settings:
    # App Store Connect API team key (Users and Access > Integrations > App Store Connect API).
    key_id: str | None = None
    issuer_id: str | None = None
    private_key_path: Path | None = None
    # In-App Purchase key, required only by the App Store Server API (notification history).
    iap_key_id: str | None = None
    iap_key_path: Path | None = None
    # Defaults the agent can omit.
    app_id: str | None = None
    bundle_id: str | None = None
    # Local repository checks (build number, commit ancestry, export compliance plist).
    repo_path: Path | None = None
    xcconfig_path: str | None = None
    info_plist_path: str | None = None
    main_ref: str = "origin/main"
    policy_path: Path | None = None
    # Safety and display.
    allow_writes: bool = False
    scoped_tokens: bool = True
    redact_ids: bool = False
    backend: Backend = "live"
    # Networking.
    asc_base_url: str = ASC_BASE_URL
    max_attempts: int = 4
    request_timeout_s: float = 20.0
    tool_deadline_s: float = 45.0
    max_retry_after_s: float = 20.0
    log_file: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        backend = (env.get("RELEASE_GUARD_BACKEND") or "live").strip().lower()
        if backend not in ("live", "demo"):
            raise ValueError("RELEASE_GUARD_BACKEND must be 'live' or 'demo'")
        return cls(
            key_id=_str(env, "ASC_KEY_ID"),
            issuer_id=_str(env, "ASC_ISSUER_ID"),
            private_key_path=_path(env, "ASC_PRIVATE_KEY_PATH"),
            iap_key_id=_str(env, "ASC_IAP_KEY_ID"),
            iap_key_path=_path(env, "ASC_IAP_KEY_PATH"),
            app_id=_str(env, "ASC_APP_ID"),
            bundle_id=_str(env, "ASC_BUNDLE_ID"),
            repo_path=_path(env, "RELEASE_GUARD_REPO"),
            xcconfig_path=_str(env, "RELEASE_GUARD_XCCONFIG"),
            info_plist_path=_str(env, "RELEASE_GUARD_INFO_PLIST"),
            main_ref=_str(env, "RELEASE_GUARD_MAIN_REF") or "origin/main",
            policy_path=_path(env, "RELEASE_GUARD_POLICY"),
            allow_writes=_flag(env, "RELEASE_GUARD_ALLOW_WRITES", False),
            scoped_tokens=_flag(env, "RELEASE_GUARD_SCOPED_TOKENS", True),
            redact_ids=_flag(env, "RELEASE_GUARD_REDACT_IDS", False),
            backend=backend,  # type: ignore[arg-type]
            asc_base_url=_str(env, "RELEASE_GUARD_ASC_BASE_URL") or ASC_BASE_URL,
            tool_deadline_s=float(env.get("RELEASE_GUARD_TOOL_DEADLINE_S") or 45.0),
            log_file=_path(env, "RELEASE_GUARD_LOG_FILE"),
        )

    def missing_asc_credentials(self) -> list[str]:
        missing = []
        if not self.key_id:
            missing.append("ASC_KEY_ID")
        if not self.issuer_id:
            missing.append("ASC_ISSUER_ID")
        if not self.private_key_path:
            missing.append("ASC_PRIVATE_KEY_PATH")
        return missing

    def missing_server_api_credentials(self) -> list[str]:
        missing = []
        if not self.iap_key_id:
            missing.append("ASC_IAP_KEY_ID")
        if not self.iap_key_path:
            missing.append("ASC_IAP_KEY_PATH")
        if not self.issuer_id:
            missing.append("ASC_ISSUER_ID")
        if not self.bundle_id:
            missing.append("ASC_BUNDLE_ID")
        return missing

    def secret_values(self) -> list[str]:
        """Identifiers that must never appear in logs (masked by the log redactor)."""
        return [v for v in (self.key_id, self.issuer_id, self.iap_key_id) if v]
