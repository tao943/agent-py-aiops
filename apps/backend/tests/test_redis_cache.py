"""Contract tests for typed Redis JSON cache primitives."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from super_ai.redis_runtime.cache import CacheLookup, RedisJsonCache, build_cache_key


class RedisTestClient(Protocol):
    async def ping(self) -> bool: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ex: int) -> bool: ...

    async def delete(self, *keys: str) -> int: ...

    async def exists(self, key: str) -> int: ...

    async def ttl(self, key: str) -> int: ...

    async def aclose(self) -> None: ...

    def scan_iter(self, *, match: str) -> AsyncIterator[str]: ...


@pytest.fixture
def task_prefix() -> str:
    return f"task1-{uuid4()}"


@pytest.fixture
async def redis_client(task_prefix: str) -> AsyncIterator[RedisTestClient]:
    client = cast(
        RedisTestClient,
        Redis.from_url("redis://localhost:6379/15", decode_responses=True),  # pyright: ignore[reportUnknownMemberType]
    )
    await client.ping()
    try:
        yield client
    finally:
        keys = [key async for key in client.scan_iter(match=f"agent-py:cache:{task_prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def test_build_cache_key_is_stable_isolated_and_never_contains_raw_inputs() -> None:
    owner = "alice@example.test"
    sensitive_input = {"query": "password reset token", "headers": {"Authorization": "secret"}}
    first = build_cache_key(
        purpose="mcp-discovery",
        owner_id=owner,
        version={"enabled": True, "connection": "one"},
        input_value=sensitive_input,
    )
    reordered = build_cache_key(
        purpose="mcp-discovery",
        owner_id=owner,
        version={"connection": "one", "enabled": True},
        input_value={"headers": {"Authorization": "secret"}, "query": "password reset token"},
    )
    other_owner = build_cache_key(
        purpose="mcp-discovery",
        owner_id="bob@example.test",
        version={"connection": "one", "enabled": True},
        input_value=sensitive_input,
    )

    assert first == reordered
    assert first != other_owner
    assert first.startswith("agent-py:cache:mcp-discovery:")
    for raw_value in (owner, "password reset token", "secret", "Authorization"):
        assert raw_value not in first


@pytest.mark.asyncio
async def test_get_json_distinguishes_miss_hit_and_applies_ttl(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    cache = RedisJsonCache(redis_client, max_value_bytes=256)
    key = build_cache_key(
        purpose=task_prefix,
        owner_id="owner-one",
        version="v1",
        input_value={"input": "one"},
    )

    assert await cache.get_json(key) == CacheLookup(state="miss", value=None)
    assert await cache.set_json(key, {"b": 2, "a": 1}, ttl_seconds=30) is True
    assert await redis_client.get(key) == '{"a":1,"b":2}'
    assert await redis_client.ttl(key) > 0
    assert await cache.get_json(key) == CacheLookup(state="hit", value={"a": 1, "b": 2})


@pytest.mark.asyncio
async def test_corrupt_or_non_object_entries_are_deleted_and_treated_as_misses(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    cache = RedisJsonCache(redis_client, max_value_bytes=256)
    for cached_value in ("not-json", "[1,2,3]"):
        key = build_cache_key(
            purpose=task_prefix,
            owner_id="owner-one",
            version=cached_value,
            input_value={"input": "one"},
        )
        await redis_client.set(key, cached_value, ex=30)

        assert await cache.get_json(key) == CacheLookup(state="miss", value=None)
        assert await redis_client.exists(key) == 0


@pytest.mark.asyncio
async def test_oversized_or_unserializable_values_are_not_cached(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    cache = RedisJsonCache(redis_client, max_value_bytes=16)
    key = build_cache_key(
        purpose=task_prefix,
        owner_id="owner-one",
        version="v1",
        input_value={"input": "one"},
    )

    assert await cache.set_json(key, {"value": "x" * 64}, ttl_seconds=30) is False
    assert await cache.set_json(key, {"invalid": object()}, ttl_seconds=30) is False
    assert await redis_client.exists(key) == 0


@dataclass
class FailingRedis:
    message: str

    async def get(self, key: str) -> str | None:
        raise RedisConnectionError(self.message)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        raise RedisConnectionError(self.message)

    async def delete(self, *keys: str) -> int:
        raise RedisConnectionError(self.message)


@pytest.mark.asyncio
async def test_redis_errors_degrade_without_exposing_key_or_connection_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_key = build_cache_key(
        purpose="mcp-discovery",
        owner_id="alice@example.test",
        version="v1",
        input_value={"query": "private query"},
    )
    cache = RedisJsonCache(
        FailingRedis("redis://alice:password@localhost:6379/15 unavailable"), max_value_bytes=256
    )

    with caplog.at_level(logging.WARNING):
        assert await cache.get_json(sensitive_key) == CacheLookup(state="degraded", value=None)
        assert await cache.set_json(sensitive_key, {"ok": True}, ttl_seconds=30) is False
        await cache.delete(sensitive_key)

    assert "password" not in caplog.text
    assert "alice@example.test" not in caplog.text
    assert "private query" not in caplog.text
    assert sensitive_key not in caplog.text


@pytest.mark.asyncio
async def test_set_json_requires_positive_ttl(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    cache = RedisJsonCache(redis_client, max_value_bytes=256)
    key = build_cache_key(
        purpose=task_prefix,
        owner_id="owner-one",
        version="v1",
        input_value={"input": "one"},
    )

    with pytest.raises(ValueError, match="ttl_seconds"):
        await cache.set_json(key, {"ok": True}, ttl_seconds=0)
