"""Configuration-backed Agent operation rate-limit enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from redis.exceptions import RedisError

from super_ai.project_config import project_config_section
from super_ai.redis_runtime.rate_limit import (
    DistributedRateLimiter,
    RateLimitDecision,
    RedisRateLimitClient,
)


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    capacity: int
    refill_per_second: float
    failure_mode: Literal["local_fallback", "fail_closed"]


class RateLimitService(Protocol):
    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision: ...


class RateLimitMetrics(Protocol):
    def record_rate_limit(
        self,
        action: str,
        *,
        allowed: bool,
        mode: Literal["redis", "local_fallback", "fail_closed"],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RateLimitExceeded(Exception):
    action: str
    decision: RateLimitDecision


class _UnavailableRedisClient:
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        del script, numkeys, keys_and_args
        raise RedisError("Redis unavailable")


class AgentRateLimitService:
    def __init__(
        self,
        client: RedisRateLimitClient | None,
        policies: Mapping[str, RateLimitPolicy],
        *,
        metrics: RateLimitMetrics | None = None,
    ) -> None:
        resolved_client = client or _UnavailableRedisClient()
        self._limiters = {
            action: DistributedRateLimiter(
                resolved_client,
                capacity=policy.capacity,
                refill_per_second=policy.refill_per_second,
                failure_mode=policy.failure_mode,
            )
            for action, policy in policies.items()
        }
        self._metrics = metrics

    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision:
        try:
            limiter = self._limiters[action]
        except KeyError as exc:
            raise ValueError(f"Unknown rate-limit action: {action}") from exc
        decision = await limiter.acquire(owner_id=owner_id, action=action)
        if self._metrics is not None:
            self._metrics.record_rate_limit(
                action,
                allowed=decision.allowed,
                mode=decision.mode,
            )
        return decision


async def enforce_rate_limit(
    service: RateLimitService,
    *,
    owner_id: str,
    action: str,
) -> RateLimitDecision:
    decision = await service.acquire(owner_id=owner_id, action=action)
    if not decision.allowed:
        raise RateLimitExceeded(action, decision)
    return decision


def load_rate_limit_policies(config_path: str | None = None) -> dict[str, RateLimitPolicy]:
    raw = project_config_section("rateLimits", config_path=config_path)
    policies: dict[str, RateLimitPolicy] = {}
    for action, raw_policy in raw.items():
        if not isinstance(raw_policy, Mapping):
            raise ValueError("Rate-limit policies must be named objects.")
        policy = cast(Mapping[object, object], raw_policy)
        capacity = policy.get("capacity")
        refill_tokens = policy.get("refillTokens")
        refill_period = policy.get("refillPeriodSeconds")
        failure_mode = policy.get("failureMode", "local_fallback")
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity <= 0
            or isinstance(refill_tokens, bool)
            or not isinstance(refill_tokens, int)
            or refill_tokens <= 0
            or isinstance(refill_period, bool)
            or not isinstance(refill_period, (int, float))
            or refill_period <= 0
            or failure_mode not in {"local_fallback", "fail_closed"}
        ):
            raise ValueError(f"Invalid rate-limit policy: {action}")
        policies[action] = RateLimitPolicy(
            capacity=capacity,
            refill_per_second=refill_tokens / float(refill_period),
            failure_mode=cast(Literal["local_fallback", "fail_closed"], failure_mode),
        )
    return policies
