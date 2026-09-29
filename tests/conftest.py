"""Shared fixtures. No test in this suite may reach the network."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from mcp import Client

from release_guard.config import Settings
from release_guard.fake_asc import FakeApple, demo_scenario
from release_guard.logs import configure
from release_guard.policy import load_policy
from release_guard.runtime import Runtime
from release_guard.server import create_server

APP_ID = "1234567890"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_network_and_clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block real sockets and strip real credentials from the environment."""

    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"test tried to reach the network: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    for name in list(os.environ):
        if name.startswith(("ASC_", "RELEASE_GUARD_")):
            monkeypatch.delenv(name, raising=False)
    configure("INFO")
    yield


@pytest.fixture(scope="session")
def ec_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def key_file(tmp_path: Path, ec_key_pem: str) -> Path:
    path = tmp_path / "AuthKey_TESTKEY123.p8"
    path.write_text(ec_key_pem)
    path.chmod(0o600)
    return path


@pytest.fixture
def fake() -> FakeApple:
    return FakeApple(demo_scenario())


async def _no_sleep(_: float) -> None:
    return None


def make_runtime(fake: FakeApple, **overrides: object) -> Runtime:
    settings = Settings(app_id=APP_ID, **overrides)  # type: ignore[arg-type]
    return Runtime(settings=settings, policy=load_policy(settings.policy_path),
                   asc_transport=fake.transport(), server_api_transport=fake.transport(), sleep=_no_sleep)


@pytest.fixture
def runtime(fake: FakeApple) -> Runtime:
    return make_runtime(fake)


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[Client]:
    async with Client(create_server(runtime), raise_exceptions=True) as c:
        yield c


def non_get_asc_requests(fake: FakeApple) -> list[str]:
    return [f"{r.method} {r.url.path}" for r in fake.requests
            if r.method != "GET" and r.url.path != "/inApps/v1/notifications/history"]
