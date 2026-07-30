"""Redis client construction and health probing."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from super_ai.redis_runtime.config import RedisRuntimeSettings


class RedisPingClient(Protocol):
    """Minimal client boundary required by the readiness health probe."""

    def ping(self) -> Awaitable[bool]:
        """Return whether the Redis server responds to PING."""
        ...


@dataclass(frozen=True, slots=True)
class RedisHealth:
    """Structured, sanitized Redis health result."""

    ok: bool
    error: str | None


def create_redis_client(settings: RedisRuntimeSettings) -> Redis:
    """Construct the lazily connecting Redis client used by application composition."""
    return Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.url,
        decode_responses=True,
        health_check_interval=30,
        socket_connect_timeout=1.0,
        socket_timeout=2.0,
        retry_on_timeout=True,
    )


async def ping_redis(client: RedisPingClient) -> RedisHealth:
    """Probe Redis without allowing readiness composition to fail closed."""
    try:
        return RedisHealth(ok=bool(await client.ping()), error=None)
    except Exception as exc:
        return RedisHealth(ok=False, error=f"Redis ping failed: {type(exc).__name__}")
