from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from super_ai.api.app import create_app
from super_ai.api.rate_limits import (
    AgentRateLimitService,
    RateLimitExceeded,
    RateLimitPolicy,
    enforce_rate_limit,
    load_rate_limit_policies,
)
from super_ai.mcp.cached_client import CachedMcpClient
from super_ai.mcp_client import McpToolDefinition
from super_ai.redis_runtime.rate_limit import RateLimitDecision


@dataclass
class FakeRateLimits:
    rejected_actions: set[str] = field(default_factory=lambda: set())
    calls: list[tuple[str, str]] = field(default_factory=lambda: [])

    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision:
        self.calls.append((owner_id, action))
        if action in self.rejected_actions:
            return RateLimitDecision(False, 0, 7, "local_fallback")
        return RateLimitDecision(True, 4, 0, "redis")


@pytest.mark.asyncio
async def test_diagnostic_create_returns_stable_429_contract(
    migrated_database_url: str,
) -> None:
    limits = FakeRateLimits({"diagnostic.create"})
    app = create_app(database_url=migrated_database_url, rate_limit_service=limits)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        user = await _register(client, "limit-diag@example.test")
        response = await client.post(
            "/aiops/diagnostics",
            headers=_auth_headers(user["accessToken"]),
            json={"query": "inspect api"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.json() == {
        "code": "rate_limit_exceeded",
        "action": "diagnostic.create",
        "retryAfterSeconds": 7,
    }
    assert limits.calls == [(user["user"]["id"], "diagnostic.create")]


@pytest.mark.asyncio
async def test_chat_authorization_precedes_bucket_and_valid_stream_is_limited(
    migrated_database_url: str,
) -> None:
    limits = FakeRateLimits({"chat.stream"})
    app = create_app(database_url=migrated_database_url, rate_limit_service=limits)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        user = await _register(client, "limit-chat@example.test")
        denied = await client.post(
            "/chat/sessions/not-owned/messages:stream",
            headers=_auth_headers(user["accessToken"]),
            json={"content": "hello"},
        )
        await app.state.memory_repositories.chat.create_session(
            owner_user_id=user["user"]["id"],
            session_id="owned-session",
        )
        limited = await client.post(
            "/chat/sessions/owned-session/messages:stream",
            headers=_auth_headers(user["accessToken"]),
            json={"content": "hello"},
        )

    assert denied.status_code == 403
    assert limited.status_code == 429
    assert limits.calls == [(user["user"]["id"], "chat.stream")]


class CountingMcpClient:
    calls = 0

    async def discover_tools(self) -> list[McpToolDefinition]:
        return []

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        self.calls += 1
        return {"ok": True}


@pytest.mark.asyncio
async def test_mcp_limit_runs_immediately_before_real_tool_execution() -> None:
    limits = FakeRateLimits({"mcp.tool_call"})
    inner = CountingMcpClient()

    async def guard() -> None:
        await enforce_rate_limit(limits, owner_id="owner-a", action="mcp.tool_call")

    client = CachedMcpClient(
        inner,
        cache=None,
        owner_id="owner-a",
        connection_id="connection-a",
        connection_version="v1",
        before_tool_call=guard,
    )
    with pytest.raises(RateLimitExceeded):
        await client.call_tool("restart", {})
    assert inner.calls == 0

    limits.rejected_actions.clear()
    assert await client.call_tool("restart", {}) == {"ok": True}
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_recovery_execute_is_fail_closed_without_redis() -> None:
    service = AgentRateLimitService(
        None,
        {
            "recovery.execute": RateLimitPolicy(
                capacity=1,
                refill_per_second=1 / 60,
                failure_mode="fail_closed",
            )
        },
    )
    decision = await service.acquire(owner_id="owner-a", action="recovery.execute")
    assert decision.allowed is False
    assert decision.mode == "fail_closed"


def test_project_rate_limit_policies_are_explicit() -> None:
    policies = load_rate_limit_policies()
    assert policies["diagnostic.create"].capacity == 5
    assert policies["diagnostic.create"].refill_per_second == pytest.approx(1 / 30)
    assert policies["chat.stream"].capacity == 10
    assert policies["mcp.tool_call"].capacity == 30
    assert policies["recovery.execute"].failure_mode == "fail_closed"


async def _register(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post(
        "/auth/register",
        json={
            "email": email,
            "displayName": "Rate Limited User",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


def _auth_headers(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
