from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from super_ai.api.observability import RedisFeatureMetrics
from super_ai.api.rate_limits import AgentRateLimitService, RateLimitPolicy
from super_ai.redis_runtime.cache import RedisJsonCache


class UnavailableCacheRedis:
    def get(self, key: str) -> Awaitable[str | bytes | None]:
        del key
        raise RedisConnectionError("private redis endpoint")

    def set(self, key: str, value: str, *, ex: int) -> Awaitable[bool | None]:
        del key, value, ex
        raise RedisConnectionError("private redis endpoint")

    def delete(self, *keys: str) -> Awaitable[int]:
        del keys
        raise RedisConnectionError("private redis endpoint")


@pytest.mark.asyncio
async def test_redis_loss_is_observable_while_cache_and_limits_degrade_safely() -> None:
    metrics = RedisFeatureMetrics()
    cache = RedisJsonCache(UnavailableCacheRedis(), metrics=metrics)
    lookup = await cache.get_json(
        "agent-py:cache:mcp-discovery:"
        + "a" * 64
        + ":"
        + "b" * 64
        + ":"
        + "c" * 64
    )
    limits = AgentRateLimitService(
        None,
        {
            "diagnostic.create": RateLimitPolicy(
                capacity=1,
                refill_per_second=1.0,
                failure_mode="local_fallback",
            )
        },
        metrics=metrics,
    )
    first = await limits.acquire(owner_id="owner-a", action="diagnostic.create")
    second = await limits.acquire(owner_id="owner-a", action="diagnostic.create")

    assert lookup.state == "degraded"
    assert (first.allowed, first.mode) == (True, "local_fallback")
    assert (second.allowed, second.mode) == (False, "local_fallback")
    snapshot = metrics.snapshot()
    assert snapshot["cache"] == {"mcp-discovery:degraded": 1}
    latency = cast(dict[str, float], snapshot["cacheAverageLatencyMs"])
    assert latency["mcp-discovery"] >= 0
    assert snapshot["rateLimit"] == {
        "diagnostic.create:allow:local_fallback": 1,
        "diagnostic.create:reject:local_fallback": 1,
    }
