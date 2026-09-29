"""Structured JSON logs on stderr, with request ids and a secret redactor.

stdout belongs to the MCP stdio transport, so every log line goes to stderr
(and optionally to RELEASE_GUARD_LOG_FILE). Each line is one JSON object.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

LOGGER_NAME = "release_guard"

# Correlates every HTTP call made while serving one tool call.
current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("rg_request_id", default="-")
current_tool: contextvars.ContextVar[str] = contextvars.ContextVar("rg_tool", default="-")

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_SENSITIVE_KEYS = {"authorization", "token", "private_key", "key", "password", "demoaccountpassword", "secret"}


def new_request_id() -> str:
    return "rg-" + uuid.uuid4().hex[:10]


class Redactor:
    """Masks JWTs, PEM blocks, bearer tokens and registered identifier values."""

    def __init__(self) -> None:
        self._exact: set[str] = set()

    def register(self, *values: str | None) -> None:
        for value in values:
            if value and len(value) >= 4:
                self._exact.add(value)

    def text(self, value: str) -> str:
        value = _PEM.sub("[REDACTED_PRIVATE_KEY]", value)
        value = _JWT.sub("[REDACTED_JWT]", value)
        value = _BEARER.sub(r"\1[REDACTED]", value)
        for secret in self._exact:
            value = value.replace(secret, "[REDACTED_ID]")
        return value

    def obj(self, value: Any, key: str | None = None) -> Any:
        if key and key.lower() in _SENSITIVE_KEYS:
            return "[REDACTED]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


REDACTOR = Redactor()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "request_id": current_request_id.get(),
            "tool": current_tool.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(REDACTOR.obj(payload), default=str, separators=(",", ":"))


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever sys.stderr is at emit time (keeps working under capture/redirects)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def configure(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    # httpx logs full URLs at INFO in its own format; our http_request event replaces it.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    stderr = _StderrHandler()
    stderr.setFormatter(JsonFormatter())
    logger.addHandler(stderr)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
    return logger


def log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"fields": fields})
