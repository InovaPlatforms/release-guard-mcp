"""Resilient, read-only-by-default HTTP client for Apple's APIs.

- Retries 429/5xx/transport errors with exponential backoff and full jitter.
- Honors Retry-After (seconds or HTTP-date) and Apple's X-Rate-Limit header.
- Every tool call has a deadline that fits inside the MCP host's tool timeout
  (Codex defaults to 60s), so a slow Apple API returns a clear error instead of
  a host-side timeout.
- Follows JSON:API `links.next` pagination, only on the configured host, so a
  bearer token is never sent to a URL a response handed us.
- Refuses every non-GET request unless it is an allowlisted query POST or the
  caller holds a WritePermit, which only the guarded submit path can mint.
"""

from __future__ import annotations

import contextvars
import email.utils
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import anyio
import httpx

from .logs import log

# Monotonic deadline for the tool call being served (set by the MCP layer).
current_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("rg_deadline", default=None)
# Per-tool-call request counter (a one-element list), so concurrent tool calls are counted separately.
current_http_calls: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("rg_http_calls",
                                                                                       default=None)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
APPLE_REQUEST_ID_HEADERS = ("x-request-id", "x-apple-request-uuid", "apple-request-id")
_RATE = re.compile(r"user-hour-lim:(\d+);\s*user-hour-rem:(\d+)")


class TokenProvider(Protocol):
    def token_for(self, method: str, url: str) -> str: ...


class ReadOnlyViolation(RuntimeError):
    """A write was attempted without a WritePermit."""


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, *, code: str | None = None,
                 request_id: str | None = None, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after_s = retry_after_s


class DeadlineExceeded(ApiError):
    pass


_PERMIT_KEY = object()


@dataclass(frozen=True)
class WritePermit:
    """Proof that every write gate passed. Only `submit.py` can construct one."""

    reason: str
    _key: object = field(repr=False, default=None)

    def __post_init__(self) -> None:
        if self._key is not _PERMIT_KEY:
            raise ReadOnlyViolation("WritePermit can only be issued by the guarded submit path")


def issue_permit(reason: str) -> WritePermit:
    return WritePermit(reason=reason, _key=_PERMIT_KEY)


@dataclass
class RateLimitState:
    limit: int | None = None
    remaining: int | None = None


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - now)


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0,
                  rng: Callable[[], float] = random.random) -> float:
    """Full-jitter exponential backoff (attempt is 1-based)."""
    return rng() * min(cap, base * (2 ** (attempt - 1)))


class ResilientClient:
    def __init__(self, base_url: str, tokens: TokenProvider | None, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 max_attempts: int = 4, timeout_s: float = 20.0,
                 max_retry_after_s: float = 20.0,
                 safe_post_paths: tuple[str, ...] = (),
                 sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[], float] = random.random,
                 concurrency: int = 4) -> None:
        self.base_url = base_url.rstrip("/")
        self._host = urlsplit(self.base_url).netloc
        self._tokens = tokens
        self._max_attempts = max_attempts
        self._max_retry_after_s = max_retry_after_s
        self._safe_post_paths = safe_post_paths
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._limiter = anyio.Semaphore(concurrency)
        self.rate_limit = RateLimitState()
        self.deadline: float | None = None
        self.calls = 0
        timeout = httpx.Timeout(timeout_s, connect=5.0)
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout,
                                       headers={"Accept": "application/json",
                                                "User-Agent": "release-guard-mcp/0.1"})

    async def aclose(self) -> None:
        await self._http.aclose()

    def set_deadline(self, seconds: float) -> None:
        self.deadline = self._clock() + seconds

    # -- guards -------------------------------------------------------------------------------
    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            if urlsplit(path_or_url).netloc != self._host:
                raise ApiError(0, "refusing to follow a link to a different host")
            return path_or_url
        return self.base_url + path_or_url

    def _check_write(self, method: str, url: str, permit: WritePermit | None) -> None:
        if method == "GET":
            return
        path = urlsplit(url).path
        if method == "POST" and path in self._safe_post_paths:
            return  # read-only query that Apple exposes as POST (notification history)
        if not isinstance(permit, WritePermit):
            log("write_blocked", level=logging.WARNING, method=method, path=path)
            raise ReadOnlyViolation(f"{method} {path} blocked: Release Guard is read-only")

    # -- core ---------------------------------------------------------------------------------
    async def request(self, method: str, path_or_url: str, *, params: dict[str, Any] | None = None,
                      json: Any = None, permit: WritePermit | None = None) -> dict[str, Any]:
        method = method.upper()
        url = self._url(path_or_url)
        req = self._http.build_request(method, url, params=params, json=json)
        full_url = str(req.url)
        self._check_write(method, full_url, permit)
        is_write = method != "GET" and permit is not None
        path = urlsplit(full_url).path

        deadline = self.deadline if self.deadline is not None else current_deadline.get()
        attempt = 0
        while True:
            attempt += 1
            if deadline is not None and self._clock() >= deadline:
                raise DeadlineExceeded(0, "tool deadline reached before Apple answered; try again")
            headers = {}
            if self._tokens is not None:
                headers["Authorization"] = "Bearer " + self._tokens.token_for(method, full_url)
            started = self._clock()
            status: int | None = None
            error: Exception | None = None
            response: httpx.Response | None = None
            try:
                async with self._limiter:
                    self.calls += 1
                    counter = current_http_calls.get()
                    if counter is not None:
                        counter[0] += 1
                    response = await self._http.request(method, full_url, json=json, headers=headers)
                status = response.status_code
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = exc
            duration_ms = int((self._clock() - started) * 1000)
            apple_id = None
            if response is not None:
                apple_id = next((response.headers.get(h) for h in APPLE_REQUEST_ID_HEADERS
                                 if response.headers.get(h)), None)
                self._observe_rate_limit(response)
            log("http_request", method=method, path=path, status=status, attempt=attempt,
                duration_ms=duration_ms, apple_request_id=apple_id,
                rate_limit_remaining=self.rate_limit.remaining,
                error=type(error).__name__ if error else None)

            if response is not None and status is not None and status < 400:
                if not response.content:
                    return {}
                return response.json()

            retry_after = parse_retry_after(response.headers.get("retry-after")) if response is not None else None
            retryable = (status in RETRYABLE_STATUS) or error is not None
            # A write that may have reached Apple is never retried, except a 429, which Apple
            # rejected before doing anything.
            if is_write and status != 429:
                retryable = False
            if not retryable or attempt >= self._max_attempts:
                raise self._to_error(response, error, apple_id, retry_after)
            if retry_after is not None and retry_after > self._max_retry_after_s:
                raise self._to_error(response, error, apple_id, retry_after)
            delay = max(retry_after or 0.0, backoff_delay(attempt, rng=self._rng))
            if deadline is not None and self._clock() + delay >= deadline:
                raise self._to_error(response, error, apple_id, retry_after)
            log("http_retry", level=logging.WARNING, method=method, path=path, attempt=attempt,
                delay_s=round(delay, 2), status=status, retry_after_s=retry_after)
            await self._sleep(delay)

    def _observe_rate_limit(self, response: httpx.Response) -> None:
        header = response.headers.get("x-rate-limit")
        if not header:
            return
        match = _RATE.search(header)
        if match:
            self.rate_limit.limit = int(match.group(1))
            self.rate_limit.remaining = int(match.group(2))
            if self.rate_limit.remaining < max(25, self.rate_limit.limit // 50):
                log("rate_limit_low", level=logging.WARNING, remaining=self.rate_limit.remaining,
                    limit=self.rate_limit.limit)

    @staticmethod
    def _to_error(response: httpx.Response | None, error: Exception | None, apple_id: str | None,
                  retry_after: float | None) -> ApiError:
        if response is None:
            return ApiError(0, f"network error talking to Apple ({type(error).__name__}); retry shortly",
                            request_id=apple_id)
        status = response.status_code
        code = title = detail = None
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("errors"):
                first = body["errors"][0]
                code, title, detail = first.get("code"), first.get("title"), first.get("detail")
            elif isinstance(body, dict) and body.get("errorCode"):
                code, detail = str(body.get("errorCode")), body.get("errorMessage")
        except ValueError:
            pass
        if status == 401:
            message = ("Apple rejected the API token (401). Check ASC_KEY_ID, ASC_ISSUER_ID and the key "
                       "file, and that the key has not been revoked.")
        elif status == 403:
            message = f"The API key lacks permission for this request (403 {code or ''}). {detail or ''}".strip()
        elif status == 404:
            message = f"Not found (404). {detail or title or ''}".strip()
        elif status == 429:
            wait = f" Retry after about {int(retry_after)}s." if retry_after else ""
            message = f"Apple rate limit reached (429 {code or 'RATE_LIMIT_EXCEEDED'}).{wait}"
        else:
            message = f"Apple returned {status} {code or ''}: {detail or title or 'no detail'}".strip()
        return ApiError(status, message, code=code, request_id=apple_id, retry_after_s=retry_after)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def get_optional(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            return await self.get(path, params)
        except ApiError as err:
            if err.status == 404:
                return None
            raise

    async def paginate(self, path: str, params: dict[str, Any] | None = None, *,
                       max_pages: int = 10) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """Follow JSON:API links.next. Returns (data, included, truncated)."""
        data: list[dict[str, Any]] = []
        included: list[dict[str, Any]] = []
        body = await self.get(path, params)
        pages = 1
        while True:
            chunk = body.get("data") or []
            data.extend(chunk if isinstance(chunk, list) else [chunk])
            included.extend(body.get("included") or [])
            next_url = (body.get("links") or {}).get("next")
            if not next_url:
                return data, included, False
            if pages >= max_pages:
                return data, included, True
            body = await self.get(next_url)
            pages += 1
