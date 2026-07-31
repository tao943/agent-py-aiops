"""Atomic Redis token-bucket limiting with a bounded local contingency mode."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from redis.exceptions import RedisError

LOGGER = logging.getLogger(__name__)
_ACTION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TOKEN_BUCKET_SCRIPT = """
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens')) or capacity
local updated_at = tonumber(redis.call('HGET', KEYS[1], 'updated_at')) or now
tokens = math.min(capacity, tokens + math.max(0, now - updated_at) * refill)
local allowed = tokens >= cost
if allowed then
    tokens = tokens - cost
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
local retry_after = 0
if not allowed then
    retry_after = math.ceil((cost - tokens) / refill)
end
return {allowed and 1 or 0, math.floor(tokens), retry_after}
"""


class RedisRateLimitClient(Protocol):
    """Minimal Redis boundary used for atomic token-bucket decisions."""

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Awaitable[object]:
        """Evaluate a Lua script without exposing a Redis client implementation."""
        ...


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Observable result of one limit acquisition."""

    allowed: bool
    remaining: int
    retry_after_seconds: int
    mode: Literal["redis", "local_fallback", "fail_closed"]


@dataclass(slots=True)
class _LocalBucket:
    tokens: float
    updated_at: float
    last_accessed_at: float


class DistributedRateLimiter:
    """Use server-time Redis buckets, with an explicitly selected outage policy."""

    def __init__(
        self,
        client: RedisRateLimitClient,
        *,
        capacity: int,
        refill_per_second: float,
        failure_mode: Literal["local_fallback", "fail_closed"] = "local_fallback",
        max_local_buckets: int = 1_000,
        local_stale_after_seconds: float | None = None,
    ) -> None:
        _validate_positive(capacity, "capacity")
        _validate_positive(refill_per_second, "refill_per_second")
        if failure_mode not in {"local_fallback", "fail_closed"}:
            raise ValueError("failure_mode must be 'local_fallback' or 'fail_closed'.")
        if max_local_buckets <= 0:
            raise ValueError("max_local_buckets must be positive.")
        stale_after = local_stale_after_seconds
        if stale_after is None:
            stale_after = 2.0 * capacity / refill_per_second
        _validate_positive(stale_after, "local_stale_after_seconds")
        self._client = client
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._failure_mode = failure_mode
        self._ttl_seconds = max(1, math.ceil(2.0 * capacity / refill_per_second))
        self._max_local_buckets = max_local_buckets
        self._local_stale_after_seconds = stale_after
        self._local_buckets: dict[tuple[str, str], _LocalBucket] = {}
        self._local_lock = asyncio.Lock()

    async def acquire(self, *, action: str, owner_id: str, cost: int = 1) -> RateLimitDecision:
        """Atomically acquire tokens for one safe action and owner scope."""
        _validate_action(action)
        _validate_positive(cost, "cost")
        owner_hash = _owner_hash(owner_id)
        key = f"agent-py:limit:{action}:{owner_hash}"
        try:
            result = await self._client.eval(
                _TOKEN_BUCKET_SCRIPT,
                1,
                key,
                self._capacity,
                self._refill_per_second,
                cost,
                self._ttl_seconds,
            )
        except RedisError:
            self._log_degraded(action)
            if self._failure_mode == "fail_closed":
                return RateLimitDecision(False, 0, 0, "fail_closed")
            return await self._acquire_locally(action, owner_hash, cost)
        allowed, remaining, retry_after = _parse_result(result)
        return RateLimitDecision(allowed, remaining, retry_after, "redis")

    async def _acquire_locally(
        self, action: str, owner_hash: str, cost: int
    ) -> RateLimitDecision:
        now = time.monotonic()
        bucket_key = (action, owner_hash)
        async with self._local_lock:
            self._evict_local_buckets(now, protected_key=bucket_key)
            bucket = self._local_buckets.get(bucket_key)
            if bucket is None:
                self._make_room_for(bucket_key)
                bucket = _LocalBucket(float(self._capacity), now, now)
                self._local_buckets[bucket_key] = bucket
            bucket.tokens = min(
                float(self._capacity),
                bucket.tokens + max(0.0, now - bucket.updated_at) * self._refill_per_second,
            )
            bucket.updated_at = now
            bucket.last_accessed_at = now
            allowed = bucket.tokens >= cost
            if allowed:
                bucket.tokens -= cost
            retry_after = (
                0
                if allowed
                else math.ceil((cost - bucket.tokens) / self._refill_per_second)
            )
            return RateLimitDecision(
                allowed,
                math.floor(bucket.tokens),
                retry_after,
                "local_fallback",
            )

    def _evict_local_buckets(self, now: float, *, protected_key: tuple[str, str]) -> None:
        stale_keys = sorted(
            key
            for key, bucket in self._local_buckets.items()
            if key != protected_key
            and now - bucket.last_accessed_at >= self._local_stale_after_seconds
        )
        for key in stale_keys:
            del self._local_buckets[key]

    def _make_room_for(self, incoming_key: tuple[str, str]) -> None:
        if (
            incoming_key in self._local_buckets
            or len(self._local_buckets) < self._max_local_buckets
        ):
            return
        oldest_key = min(
            self._local_buckets,
            key=lambda key: (self._local_buckets[key].last_accessed_at, key),
        )
        del self._local_buckets[oldest_key]

    @staticmethod
    def _log_degraded(action: str) -> None:
        LOGGER.warning(
            "Redis rate-limit operation degraded.",
            extra={"rate_limit_action": action, "rate_limit_mode": "degraded"},
        )

    @property
    def local_bucket_count(self) -> int:
        """Return local contingency state cardinality for bounded-runtime observability."""
        return len(self._local_buckets)


def _validate_positive(value: int | float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive.")


def _validate_action(action: str) -> None:
    if not _ACTION_PATTERN.fullmatch(action):
        raise ValueError(
            "action must contain only lowercase letters, digits, underscores, and hyphens."
        )


def _owner_hash(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()


def _parse_result(result: object) -> tuple[bool, int, int]:
    values = cast(list[object] | tuple[object, ...], result)
    if len(values) != 3:
        raise RuntimeError("Redis rate-limit script returned an invalid result.")
    allowed, remaining, retry_after = values
    return bool(int(cast(str | int, allowed))), int(cast(str | int, remaining)), int(
        cast(str | int, retry_after)
    )
