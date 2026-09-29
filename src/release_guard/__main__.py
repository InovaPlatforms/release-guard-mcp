"""Entry point: `release-guard-mcp` (or `python -m release_guard`) serves MCP over stdio."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import Settings
from .logs import configure, log
from .runtime import Runtime


def _check(settings: Settings) -> dict[str, object]:
    """Configuration report with booleans only; safe to paste into an issue."""
    key = settings.private_key_path
    iap = settings.iap_key_path
    return {
        "version": __version__,
        "backend": settings.backend,
        "asc_credentials": not settings.missing_asc_credentials(),
        "asc_key_file_exists": bool(key and key.is_file()),
        "server_api_credentials": not settings.missing_server_api_credentials(),
        "iap_key_file_exists": bool(iap and iap.is_file()),
        "default_app_id_set": bool(settings.app_id),
        "repo_configured": bool(settings.repo_path and settings.repo_path.is_dir()),
        "writes_enabled": settings.allow_writes,
        "scoped_tokens": settings.scoped_tokens,
        "redact_ids": settings.redact_ids,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release-guard-mcp", description=__doc__)
    parser.add_argument("--check", action="store_true", help="print a configuration report and exit")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure(os.environ.get("RELEASE_GUARD_LOG_LEVEL", "INFO"), settings.log_file)
    if args.check:
        print(json.dumps(_check(settings), indent=2))
        return 0

    from .server import create_server

    runtime = Runtime.from_settings(settings)
    log("server_start", version=__version__, backend=settings.backend, writes_enabled=settings.allow_writes,
        scoped_tokens=settings.scoped_tokens, policy=runtime.policy.source)
    create_server(runtime).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
