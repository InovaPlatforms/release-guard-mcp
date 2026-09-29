"""Retries, Retry-After, rate limits, deadlines, pagination and the read-only guard."""

from __future__ import annotations

import email.utils
import json
import logging
import time

import httpx
import pytest

from release_guard.transport import (
    ApiError,
    DeadlineExceeded,
    ReadOnlyViolation,
    ResilientClient,
    WritePermit,
    backoff_delay,
    issue_permit,
    parse_retry_after,
)

pytestmark = pytest.mark.anyio
BASE = "https://api.appstoreconnect.apple.com"


class Script:
    """A transport that replays a list of responses (or exceptions) and records requests."""

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, httpx.Response)
        return step


def ok(body: dict | None = None, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body or {"data": []}, headers=headers)


def err(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"status": str(status), "code": "X", "detail": "d"}]},
                          headers=headers)


def make(script: Script, sleeps: list[float] | None = None, **kw: object) -> ResilientClient:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return ResilientClient(BASE, None, transport=httpx.MockTransport(script), sleep=fake_sleep,
                           rng=lambda: 1.0, **kw)  # type: ignore[arg-type]


async def test_retries_5xx_then_succeeds() -> None:
    script, sleeps = Script(err(503), err(502), ok({"data": [1]})), []
    client = make(script, sleeps)
    assert await client.get("/v1/apps") == {"data": [1]}
    assert len(script.requests) == 3 and sleeps == [0.5, 1.0]


async def test_honors_retry_after_seconds_on_429() -> None:
    script, sleeps = Script(err(429, **{"Retry-After": "3"}), ok()), []
    await make(script, sleeps).get("/v1/apps")
    assert sleeps == [3.0]


async def test_parses_http_date_retry_after() -> None:
    future = email.utils.formatdate(time.time() + 5, usegmt=True)
    assert 3 <= (parse_retry_after(future) or 0) <= 6
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("garbage") is None and parse_retry_after(None) is None


async def test_long_retry_after_fails_fast_with_the_wait_time() -> None:
    script = Script(err(429, **{"Retry-After": "3600"}), ok())
    with pytest.raises(ApiError) as info:
        await make(script, max_retry_after_s=20).get("/v1/apps")
    assert info.value.status == 429 and info.value.retry_after_s == 3600
    assert "Retry after about 3600s" in str(info.value) and len(script.requests) == 1


async def test_gives_up_after_max_attempts() -> None:
    script = Script(err(503))
    with pytest.raises(ApiError) as info:
        await make(script, max_attempts=3).get("/v1/apps")
    assert info.value.status == 503 and len(script.requests) == 3


async def test_retries_timeouts_and_connection_errors() -> None:
    script = Script(httpx.ReadTimeout("slow"), httpx.ConnectError("down"), ok())
    await make(script).get("/v1/apps")
    assert len(script.requests) == 3


async def test_client_errors_are_not_retried_and_explain_the_fix() -> None:
    script = Script(err(401))
    with pytest.raises(ApiError, match="ASC_KEY_ID") as info:
        await make(script).get("/v1/apps")
    assert info.value.status == 401 and len(script.requests) == 1


async def test_get_optional_maps_404_to_none() -> None:
    assert await make(Script(err(404))).get_optional("/v1/x") is None


async def test_deadline_stops_retrying() -> None:
    now = [100.0]
    script = Script(err(503))
    client = ResilientClient(BASE, None, transport=httpx.MockTransport(script), clock=lambda: now[0],
                             sleep=lambda s: _advance(now, s), rng=lambda: 1.0, max_attempts=10)
    client.set_deadline(2.0)
    with pytest.raises(ApiError):
        await client.get("/v1/apps")
    assert len(script.requests) <= 3


async def _advance(now: list[float], seconds: float) -> None:
    now[0] += seconds


async def test_expired_deadline_raises_before_calling_apple() -> None:
    script = Script(ok())
    client = make(script)
    client.deadline = time.monotonic() - 1
    with pytest.raises(DeadlineExceeded):
        await client.get("/v1/apps")
    assert script.requests == []


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
async def test_read_only_guard_blocks_writes_before_the_network(method: str) -> None:
    script = Script(ok())
    with pytest.raises(ReadOnlyViolation):
        await make(script).request(method, "/v1/reviewSubmissions", json={})
    assert script.requests == []


async def test_allowlisted_query_post_passes_the_guard() -> None:
    script = Script(ok({"notificationHistory": []}))
    client = make(script, safe_post_paths=("/inApps/v1/notifications/history",))
    await client.request("POST", "/inApps/v1/notifications/history", json={"startDate": 1})
    assert len(script.requests) == 1


async def test_write_permit_cannot_be_forged() -> None:
    with pytest.raises(ReadOnlyViolation):
        WritePermit(reason="forged")
    assert issue_permit("ok").reason == "ok"


async def test_permitted_write_is_not_retried_on_5xx_but_is_on_429() -> None:
    script = Script(err(503), ok())
    with pytest.raises(ApiError):
        await make(script).request("PATCH", "/v1/x", json={}, permit=issue_permit("test"))
    assert len(script.requests) == 1
    script = Script(err(429, **{"Retry-After": "1"}), ok())
    await make(script).request("PATCH", "/v1/x", json={}, permit=issue_permit("test"))
    assert len(script.requests) == 2


async def test_pagination_follows_next_links_and_reports_truncation() -> None:
    pages = [ok({"data": [{"id": "1"}], "links": {"next": f"{BASE}/v1/items?cursor=2"}}),
             ok({"data": [{"id": "2"}], "links": {"next": f"{BASE}/v1/items?cursor=3"}}),
             ok({"data": [{"id": "3"}], "links": {}})]
    data, _, truncated = await make(Script(*pages)).paginate("/v1/items")
    assert [d["id"] for d in data] == ["1", "2", "3"] and not truncated
    pages = [ok({"data": [{"id": "1"}], "links": {"next": f"{BASE}/v1/items?cursor=2"}}), ok({"data": []})]
    data, _, truncated = await make(Script(*pages)).paginate("/v1/items", max_pages=1)
    assert truncated and len(data) == 1


async def test_never_follows_a_next_link_to_another_host() -> None:
    script = Script(ok({"data": [], "links": {"next": "https://evil.example.com/steal"}}))
    with pytest.raises(ApiError, match="different host"):
        await make(script).paginate("/v1/items")
    assert all(r.url.host == "api.appstoreconnect.apple.com" for r in script.requests)


async def test_parses_x_rate_limit_and_warns_when_low(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("release_guard")
    logger.addHandler(caplog.handler)
    try:
        client = make(Script(ok(**{"X-Rate-Limit": "user-hour-lim:3500;user-hour-rem:20;"})))
        await client.get("/v1/apps")
    finally:
        logger.removeHandler(caplog.handler)
    assert (client.rate_limit.limit, client.rate_limit.remaining) == (3500, 20)
    assert any(r.getMessage() == "rate_limit_low" for r in caplog.records)


def test_backoff_is_bounded_full_jitter() -> None:
    assert backoff_delay(1, rng=lambda: 1.0) == 0.5
    assert backoff_delay(10, rng=lambda: 1.0) == 8.0
    assert backoff_delay(3, rng=lambda: 0.0) == 0.0


async def test_logs_have_request_fields_and_no_bearer_token(capsys: pytest.CaptureFixture[str]) -> None:
    class Tokens:
        def token_for(self, method: str, url: str) -> str:
            return "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJ4In0.c2lnbmF0dXJl"

    script = Script(ok(**{"x-request-id": "APPLE123"}))
    client = ResilientClient(BASE, Tokens(), transport=httpx.MockTransport(script))
    await client.get("/v1/apps")
    assert script.requests[0].headers["authorization"].startswith("Bearer eyJ")
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    event = next(line for line in lines if line["event"] == "http_request")
    assert event["apple_request_id"] == "APPLE123" and event["status"] == 200 and "duration_ms" in event
    assert "eyJ" not in json.dumps(lines)
