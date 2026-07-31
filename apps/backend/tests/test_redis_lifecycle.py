"""Production Redis event-delivery lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import httpx
import pytest
from fastapi import Request
from redis.asyncio import Redis

import super_ai.api.app as api_app
from super_ai.api.app import create_app
from super_ai.redis_runtime.config import RedisRuntimeSettings


@dataclass
class FakeRedisClient:
    closed: int = 0

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class FakeBackgroundRuntime:
    started: int = 0
    stopped: int = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@dataclass
class FakeDispatcher:
    started: int = 0
    stopped: int = 0
    events: list[str] = field(default_factory=lambda: list[str]())

    def start(self) -> None:
        self.started += 1
        self.events.append("dispatcher.start")

    async def stop(self) -> None:
        self.stopped += 1
        self.events.append("dispatcher.stop")


@dataclass
class FakeRelay:
    started: int = 0
    stopped: int = 0
    events: list[str] = field(default_factory=lambda: list[str]())

    async def start(self) -> None:
        self.started += 1
        self.events.append("relay.start")

    async def stop(self) -> None:
        self.stopped += 1
        self.events.append("relay.stop")


async def _healthy_postgresql(_request: Request) -> dict[str, object]:
    return {"ok": True, "error": None}


async def _healthy_milvus(_request: Request) -> dict[str, object]:
    return {"ok": True, "error": None}


async def _healthy_llm(_request: Request) -> dict[str, object]:
    return {"ok": True, "error": None}


async def _healthy_mcp(_request: Request) -> dict[str, object]:
    return {"ok": True, "error": None}


@pytest.mark.asyncio
async def test_lifespan_runs_one_redis_delivery_pair_and_closes_its_owned_client() -> None:
    client = FakeRedisClient()
    dispatcher = FakeDispatcher()
    relay = FakeRelay()
    app = create_app(
        redis_settings=RedisRuntimeSettings(url="redis://localhost:6379/15"),
        redis_client_factory=lambda _settings: cast(Redis, client),
        redis_dispatcher=dispatcher,
        redis_relay=relay,
    )
    runtime = FakeBackgroundRuntime()
    app.state.background_job_runtime = runtime
    app.state.redis_lifecycle_error = "Redis unavailable: ConnectionError"

    async with app.router.lifespan_context(app):
        assert runtime.started == 1
        assert dispatcher.started == 1
        assert relay.started == 1
        assert client.closed == 0
        assert app.state.redis_lifecycle_error is None

    assert runtime.stopped == 1
    assert dispatcher.stopped == 1
    assert relay.stopped == 1
    assert client.closed == 1
    assert dispatcher.events == ["dispatcher.start", "dispatcher.stop"]
    assert relay.events == ["relay.start", "relay.stop"]


@pytest.mark.asyncio
async def test_redis_relay_start_failure_keeps_postgresql_runtime_operational() -> None:
    class FailingRelay(FakeRelay):
        async def start(self) -> None:
            self.started += 1
            raise ConnectionError("redis://user:password@redis.test:6379/15 is unavailable")

    dispatcher = FakeDispatcher()
    relay = FailingRelay()
    app = create_app(
        redis_settings=RedisRuntimeSettings(url="redis://localhost:6379/15"),
        redis_client_factory=lambda _settings: cast(Redis, FakeRedisClient()),
        redis_dispatcher=dispatcher,
        redis_relay=relay,
    )
    runtime = FakeBackgroundRuntime()
    app.state.background_job_runtime = runtime

    async with app.router.lifespan_context(app):
        assert runtime.started == 1
        assert dispatcher.started == 1
        assert relay.started == 1

    assert runtime.stopped == 1
    assert dispatcher.stopped == 1
    assert relay.stopped == 1


@pytest.mark.asyncio
async def test_lifespan_relay_failure_reports_redis_degraded_when_ping_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRelay(FakeRelay):
        async def start(self) -> None:
            self.started += 1
            raise ConnectionError("redis://user:password@redis.test:6379/15 is unavailable")

    client = FakeRedisClient()
    relay = FailingRelay()
    app = create_app(
        redis_settings=RedisRuntimeSettings(url="redis://localhost:6379/15"),
        redis_client=cast(Redis, client),
        redis_relay=relay,
    )
    runtime = FakeBackgroundRuntime()
    app.state.background_job_runtime = runtime
    monkeypatch.setattr(api_app, "_postgresql_readiness_payload", _healthy_postgresql)
    monkeypatch.setattr(api_app, "_milvus_readiness_payload", _healthy_milvus)
    monkeypatch.setattr(api_app, "_llm_readiness_payload", _healthy_llm)
    monkeypatch.setattr(api_app, "_mcp_readiness_payload", _healthy_mcp)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.get("/ready")

        assert runtime.started == 1
        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["status"] == "degraded"
        assert payload["dependencies"]["redis"] == {
            "ok": False,
            "error": "Redis is unavailable.",
        }
        assert "password" not in response.text

    assert runtime.stopped == 1
