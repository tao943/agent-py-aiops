"""Contract tests for distributed Redis token-bucket rate limiting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from super_ai.redis_runtime.rate_limit import DistributedRateLimiter


class RedisTestClient(Protocol):
    async def ping(self) -> bool: ...

    async def delete(self, *keys: str) -> int: ...

    async def ttl(self, key: str) -> int: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...

    async def aclose(self) -> None: ...

    def scan_iter(self, *, match: str) -> AsyncIterator[str]: ...


@pytest.fixture
def task_prefix() -> str:
    return f"task4-{uuid4()}"


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
        keys = [key async for key in client.scan_iter(match=f"agent-py:limit:{task_prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_acquire_allows_initial_burst_then_rejects_with_retry_after(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    limiter = DistributedRateLimiter(
        redis_client,
        capacity=2,
        refill_per_second=1.0,
    )

    first = await limiter.acquire(action=task_prefix, owner_id="alice@example.test")
    second = await limiter.acquire(action=task_prefix, owner_id="alice@example.test")
    rejected = await limiter.acquire(action=task_prefix, owner_id="alice@example.test")

    assert (first.allowed, first.remaining, first.retry_after_seconds, first.mode) == (
        True,
        1,
        0,
        "redis",
    )
    assert (second.allowed, second.remaining, second.retry_after_seconds, second.mode) == (
        True,
        0,
        0,
        "redis",
    )
    assert rejected.allowed is False
    assert rejected.remaining == 0
    assert rejected.retry_after_seconds == 1
    assert rejected.mode == "redis"


@pytest.mark.asyncio
async def test_server_time_refills_a_bucket_without_exceeding_capacity(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    limiter = DistributedRateLimiter(redis_client, capacity=1, refill_per_second=50.0)

    assert (await limiter.acquire(action=task_prefix, owner_id="owner")).allowed is True
    assert (await limiter.acquire(action=task_prefix, owner_id="owner")).allowed is False
    await asyncio.sleep(0.05)
    refilled = await limiter.acquire(action=task_prefix, owner_id="owner")

    assert (refilled.allowed, refilled.remaining, refilled.mode) == (True, 0, "redis")


@pytest.mark.asyncio
async def test_rate_limit_keys_isolate_actions_and_owners_and_hide_owner_ids(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    limiter = DistributedRateLimiter(redis_client, capacity=1, refill_per_second=1.0)
    action_two = f"{task_prefix}-two"

    assert (
        await limiter.acquire(action=task_prefix, owner_id="alice@example.test")
    ).allowed is True
    assert (await limiter.acquire(action=task_prefix, owner_id="bob@example.test")).allowed is True
    assert (await limiter.acquire(action=action_two, owner_id="alice@example.test")).allowed is True

    keys = [key async for key in redis_client.scan_iter(match=f"agent-py:limit:{task_prefix}*")]
    assert len(keys) == 3
    assert all(key.startswith("agent-py:limit:") for key in keys)
    assert all("alice@example.test" not in key and "bob@example.test" not in key for key in keys)


@pytest.mark.asyncio
async def test_hash_expiry_is_at_least_twice_the_full_refill_period(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    limiter = DistributedRateLimiter(redis_client, capacity=3, refill_per_second=2.0)

    await limiter.acquire(action=task_prefix, owner_id="owner")
    key = [key async for key in redis_client.scan_iter(match=f"agent-py:limit:{task_prefix}:*")][0]

    assert await redis_client.ttl(key) >= 3


@pytest.mark.asyncio
async def test_concurrent_acquisitions_never_overspend_capacity(
    redis_client: RedisTestClient, task_prefix: str
) -> None:
    limiter = DistributedRateLimiter(redis_client, capacity=5, refill_per_second=0.01)

    decisions = await asyncio.gather(
        *(limiter.acquire(action=task_prefix, owner_id="owner") for _ in range(20))
    )

    assert sum(decision.allowed for decision in decisions) == 5
    assert all(decision.mode == "redis" for decision in decisions)


@dataclass
class FailingRedis:
    message: str

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        raise RedisConnectionError(self.message)


@pytest.mark.asyncio
async def test_redis_failure_uses_locked_local_fallback_without_leaking_sensitive_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = DistributedRateLimiter(
        FailingRedis("redis://alice:password@redis.test:6379/15 unavailable"),
        capacity=1,
        refill_per_second=1.0,
    )

    with caplog.at_level(logging.WARNING):
        first = await limiter.acquire(action="chat", owner_id="alice@example.test")
        second = await limiter.acquire(action="chat", owner_id="alice@example.test")

    assert (first.allowed, first.mode) == (True, "local_fallback")
    assert (second.allowed, second.mode) == (False, "local_fallback")
    assert "password" not in caplog.text
    assert "alice@example.test" not in caplog.text


@pytest.mark.asyncio
async def test_fail_closed_does_not_create_or_consume_local_state() -> None:
    limiter = DistributedRateLimiter(
        FailingRedis("private failure"),
        capacity=1,
        refill_per_second=1.0,
        failure_mode="fail_closed",
    )

    decision = await limiter.acquire(action="chat", owner_id="alice@example.test")

    assert decision == type(decision)(False, 0, 0, "fail_closed")
    assert limiter.local_bucket_count == 0


@pytest.mark.asyncio
async def test_local_fallback_is_bounded_and_evicts_oldest_bucket_deterministically() -> None:
    limiter = DistributedRateLimiter(
        FailingRedis("private failure"),
        capacity=1,
        refill_per_second=1.0,
        max_local_buckets=2,
        local_stale_after_seconds=3600.0,
    )

    await limiter.acquire(action="chat", owner_id="owner-one")
    await limiter.acquire(action="chat", owner_id="owner-two")
    await limiter.acquire(action="chat", owner_id="owner-three")

    assert limiter.local_bucket_count == 2
    assert (await limiter.acquire(action="chat", owner_id="owner-one")).allowed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "cost"),
    [("UPPER", 1), ("unsafe/action", 1), ("chat", 0), ("chat", -1)],
)
async def test_acquire_validates_safe_action_and_positive_cost(action: str, cost: int) -> None:
    limiter = DistributedRateLimiter(FailingRedis("not called"), capacity=1, refill_per_second=1.0)

    with pytest.raises(ValueError):
        await limiter.acquire(action=action, owner_id="owner", cost=cost)


@pytest.mark.parametrize("max_local_buckets", [float("nan"), 1.5, 0, True])
def test_constructor_rejects_non_integer_local_bucket_limits(
    max_local_buckets: object,
) -> None:
    with pytest.raises(ValueError):
        DistributedRateLimiter(
            FailingRedis("not called"),
            capacity=1,
            refill_per_second=1.0,
            max_local_buckets=cast(int, max_local_buckets),
        )


@pytest.mark.parametrize("capacity", [1.5, float("nan"), True])
def test_constructor_rejects_non_integer_capacity(capacity: object) -> None:
    with pytest.raises(ValueError):
        DistributedRateLimiter(
            FailingRedis("not called"),
            capacity=cast(int, capacity),
            refill_per_second=1.0,
        )
