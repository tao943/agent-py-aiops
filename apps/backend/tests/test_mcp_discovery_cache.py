"""Behavior tests for owner-scoped MCP tool discovery caching."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast

import pytest

from super_ai.mcp.cached_client import CachedMcpClient, connection_cache_version
from super_ai.mcp_client import McpToolDefinition
from super_ai.mcp_connections import McpConnectionService
from super_ai.memory.repositories import McpConnectionRecord, MemoryRepositories
from super_ai.redis_runtime.cache import CacheLookup, RuntimeCache


@dataclass
class CountingMcpClient:
    discovered: int = 0
    called: int = 0

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        self.discovered += 1
        return [
            McpToolDefinition(
                name="SearchLog",
                description="Search logs",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                server_name="connection-one",
            )
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.called += 1
        return {"name": name, "arguments": dict(arguments), "call": self.called}


def _empty_cache_values() -> dict[str, dict[str, object]]:
    return {}


def _empty_cache_writes() -> list[tuple[str, dict[str, object], int]]:
    return []


@dataclass
class MemoryCache(RuntimeCache):
    values: dict[str, dict[str, object]] = field(default_factory=_empty_cache_values)
    writes: list[tuple[str, dict[str, object], int]] = field(default_factory=_empty_cache_writes)
    state: str = "normal"

    async def get_json(self, key: str) -> CacheLookup[dict[str, object]]:
        if self.state == "degraded":
            return CacheLookup(state="degraded", value=None)
        value = self.values.get(key)
        return (
            CacheLookup(state="hit", value=value)
            if value is not None
            else CacheLookup(state="miss", value=None)
        )

    async def set_json(self, key: str, value: Mapping[str, object], ttl_seconds: int) -> bool:
        copied = dict(value)
        self.values[key] = copied
        self.writes.append((key, copied, ttl_seconds))
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture
def cache() -> MemoryCache:
    return MemoryCache()


def _client(
    *, cache: RuntimeCache, owner_id: str = "owner-one", version: str = "v1"
) -> tuple[CachedMcpClient, CountingMcpClient]:
    inner = CountingMcpClient()
    return (
        CachedMcpClient(
            inner,
            cache=cache,
            owner_id=owner_id,
            connection_id="connection-one",
            connection_version=version,
        ),
        inner,
    )


@pytest.mark.asyncio
async def test_same_owner_connection_and_version_discovers_upstream_once(
    cache: MemoryCache,
) -> None:
    client, inner = _client(cache=cache)

    assert await client.discover_tools() == await client.discover_tools()

    assert inner.discovered == 1
    assert cache.writes[0][2] == 300


@pytest.mark.asyncio
async def test_different_owner_version_or_behavioral_configuration_misses(
    cache: MemoryCache,
) -> None:
    first, first_inner = _client(cache=cache)
    other_owner, other_owner_inner = _client(cache=cache, owner_id="owner-two")
    other_version, other_version_inner = _client(cache=cache, version="v2")

    await first.discover_tools()
    await other_owner.discover_tools()
    await other_version.discover_tools()

    assert [
        first_inner.discovered,
        other_owner_inner.discovered,
        other_version_inner.discovered,
    ] == [1, 1, 1]
    assert len(cache.values) == 3
    assert connection_cache_version(
        updated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        behavioral_config={
            "transport": "sse",
            "url": "https://mcp.test/sse",
            "timeoutSeconds": 15,
            "retries": 1,
        },
    ) != connection_cache_version(
        updated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        behavioral_config={
            "transport": "streamable_http",
            "url": "https://mcp.test/sse",
            "timeoutSeconds": 15,
            "retries": 1,
        },
    )


@pytest.mark.asyncio
async def test_degraded_redis_calls_upstream(cache: MemoryCache) -> None:
    cache.state = "degraded"
    client, inner = _client(cache=cache)

    await client.discover_tools()

    assert inner.discovered == 1


@pytest.mark.asyncio
async def test_corrupt_or_schema_invalid_cached_tools_call_upstream_and_repair(
    cache: MemoryCache,
) -> None:
    client, inner = _client(cache=cache)
    await client.discover_tools()
    key = next(iter(cache.values))
    cache.values[key] = {"tools": [{"name": 1}]}

    tools = await client.discover_tools()

    assert inner.discovered == 2
    assert tools[0].name == "SearchLog"
    assert cache.values[key]["tools"] == [
        {
            "name": "SearchLog",
            "description": "Search logs",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "serverName": "connection-one",
        }
    ]


@pytest.mark.asyncio
async def test_call_tool_always_invokes_upstream_and_never_writes_a_tool_call_cache_entry(
    cache: MemoryCache,
) -> None:
    client, inner = _client(cache=cache)

    assert await client.call_tool("SearchLog", {"query": "first"}) == {
        "name": "SearchLog",
        "arguments": {"query": "first"},
        "call": 1,
    }
    assert await client.call_tool("SearchLog", {"query": "second"}) == {
        "name": "SearchLog",
        "arguments": {"query": "second"},
        "call": 2,
    }

    assert inner.called == 2
    assert cache.writes == []


@dataclass
class ScopedConnectionRepository:
    records: dict[tuple[str, str], McpConnectionRecord]

    async def get(self, *, owner_user_id: str, connection_id: str) -> McpConnectionRecord | None:
        return self.records.get((owner_user_id, connection_id))

    async def save_check(
        self,
        *,
        owner_user_id: str,
        connection_id: str,
        ok: bool,
        tools: list[dict[str, object]],
        error: str | None,
    ) -> McpConnectionRecord | None:
        del ok, tools, error
        return self.records.get((owner_user_id, connection_id))


def _record(owner_id: str) -> McpConnectionRecord:
    return McpConnectionRecord(
        id="connection-one",
        owner_user_id=owner_id,
        name="Connection",
        transport="sse",
        url="https://mcp.test/sse",
        enabled=True,
        timeout_seconds=15,
        retries=1,
        last_check_ok=None,
        last_tool_count=None,
        last_tools=[],
        last_error=None,
        last_checked_at=None,
        created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_authorized_connection_composition_cannot_consume_another_owners_discovery(
    cache: MemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_one = _record("owner-one")
    owner_two = _record("owner-two")
    repository = ScopedConnectionRepository(
        {("owner-one", owner_one.id): owner_one, ("owner-two", owner_two.id): owner_two}
    )
    repositories = SimpleNamespace(mcp_connections=repository)
    service = McpConnectionService(
        cast(MemoryRepositories, repositories),
        default_url="https://mcp.test/sse",
        default_timeout_seconds=15,
        default_retries=1,
        cache=cache,
    )
    upstream = CountingMcpClient()

    def fake_client_from_records(_records: list[McpConnectionRecord]) -> CountingMcpClient:
        return upstream

    monkeypatch.setattr("super_ai.mcp_connections._client_from_records", fake_client_from_records)

    await service.check(owner_user_id="owner-one", connection_id=owner_one.id)
    await service.check(owner_user_id="owner-one", connection_id=owner_one.id)
    await service.check(owner_user_id="owner-two", connection_id=owner_two.id)

    assert upstream.discovered == 2


@pytest.mark.asyncio
async def test_connection_version_and_cache_artifacts_do_not_contain_secrets(
    cache: MemoryCache, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "super-secret-token"
    version = connection_cache_version(
        updated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        behavioral_config={
            "transport": "sse",
            "url": "https://username:super-secret-token@mcp.test/sse?access_token=super-secret-token",
            "timeoutSeconds": 15,
            "headers": {"Authorization": f"Bearer {secret}"},
            "cookies": {"session": secret},
        },
    )
    client, _ = _client(cache=cache, version=version)

    with caplog.at_level(logging.WARNING):
        await client.discover_tools()

    key, payload, _ = cache.writes[0]
    artifacts = "\n".join((version, key, repr(payload), caplog.text))
    for forbidden in (secret, "username", "Authorization", "access_token", "cookies"):
        assert forbidden not in artifacts
