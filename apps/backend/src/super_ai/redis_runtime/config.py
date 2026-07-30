"""Typed Redis runtime configuration loaded from project JSON."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from super_ai.project_config import (
    ProjectConfigurationError,
    project_config_section,
    required_float,
    required_int,
    required_str,
)


class RedisRuntimeConfigurationError(RuntimeError):
    """Raised when Redis runtime configuration is invalid."""


@dataclass(frozen=True, slots=True)
class RedisRuntimeSettings:
    """Runtime settings for Redis-backed recoverable infrastructure."""

    url: str = field(repr=False)
    stream_prefix: str = "agent-py"
    stream_maxlen: int = 10_000
    block_timeout_ms: int = 1_000
    event_dedupe_ttl_seconds: int = 86_400
    mcp_cache_ttl_seconds: int = 60
    retrieval_cache_ttl_seconds: int = 300
    rate_limit_capacity: int = 60
    rate_limit_refill_per_second: float = 1.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise RedisRuntimeConfigurationError(
                "Redis URL must use redis:// or rediss:// and include a host."
            )
        if not self.stream_prefix.strip():
            raise RedisRuntimeConfigurationError("Redis streamPrefix must be a non-empty string.")
        for value, key in (
            (self.stream_maxlen, "streamMaxlen"),
            (self.block_timeout_ms, "blockTimeoutMs"),
            (self.event_dedupe_ttl_seconds, "eventDedupeTtlSeconds"),
            (self.mcp_cache_ttl_seconds, "mcpCacheTtlSeconds"),
            (self.retrieval_cache_ttl_seconds, "retrievalCacheTtlSeconds"),
            (self.rate_limit_capacity, "rateLimit.capacity"),
            (self.rate_limit_refill_per_second, "rateLimit.refillPerSecond"),
        ):
            if value <= 0:
                raise RedisRuntimeConfigurationError(f"Redis setting must be positive: {key}")


def load_redis_runtime_settings(
    config_path: Path | str | None = None,
) -> RedisRuntimeSettings:
    """Load Redis runtime settings from the repository project JSON config."""
    try:
        config = project_config_section("redis", config_path=config_path)
        rate_limit = config.get("rateLimit")
        if not isinstance(rate_limit, Mapping):
            raise ProjectConfigurationError("Project config field must be an object: rateLimit")
        typed_rate_limit = cast(Mapping[str, object], rate_limit)
        return RedisRuntimeSettings(
            url=required_str(config, "url"),
            stream_prefix=required_str(config, "streamPrefix"),
            stream_maxlen=required_int(config, "streamMaxlen"),
            block_timeout_ms=required_int(config, "blockTimeoutMs"),
            event_dedupe_ttl_seconds=required_int(config, "eventDedupeTtlSeconds"),
            mcp_cache_ttl_seconds=required_int(config, "mcpCacheTtlSeconds"),
            retrieval_cache_ttl_seconds=required_int(config, "retrievalCacheTtlSeconds"),
            rate_limit_capacity=required_int(typed_rate_limit, "capacity"),
            rate_limit_refill_per_second=required_float(typed_rate_limit, "refillPerSecond"),
        )
    except ProjectConfigurationError as exc:
        raise RedisRuntimeConfigurationError(str(exc)) from exc
