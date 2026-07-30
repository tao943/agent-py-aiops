from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from super_ai.redis_runtime.client import RedisHealth, create_redis_client, ping_redis
from super_ai.redis_runtime.config import (
    RedisRuntimeConfigurationError,
    RedisRuntimeSettings,
    load_redis_runtime_settings,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("config_name", "expected_url"),
    [
        ("project.json", "redis://localhost:6379/0"),
        ("project.template.json", "redis://localhost:6379/0"),
        ("project.test.json", "redis://localhost:6379/15"),
    ],
)
def test_project_configs_load_redis_runtime_settings(
    config_name: str,
    expected_url: str,
) -> None:
    settings = load_redis_runtime_settings(ROOT / "config" / config_name)

    assert settings == RedisRuntimeSettings(
        url=expected_url,
        stream_prefix="agent-py",
        stream_maxlen=10_000,
        block_timeout_ms=1_000,
        event_dedupe_ttl_seconds=86_400,
        mcp_cache_ttl_seconds=60,
        retrieval_cache_ttl_seconds=300,
        rate_limit_capacity=60,
        rate_limit_refill_per_second=1.0,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6379/0",
        "redis:///0",
        "redis+socket:///tmp/redis.sock",
    ],
)
def test_settings_reject_unsupported_or_hostless_urls(url: str) -> None:
    with pytest.raises(RedisRuntimeConfigurationError, match="Redis URL"):
        RedisRuntimeSettings(url=url)


def test_settings_representations_and_validation_errors_redact_passwords() -> None:
    secret_url = "redis://alice:super-secret@localhost:6379/0"
    settings = RedisRuntimeSettings(url=secret_url)

    assert "super-secret" not in repr(settings)
    with pytest.raises(RedisRuntimeConfigurationError) as error:
        RedisRuntimeSettings(url="http://alice:super-secret@localhost:6379/0")
    assert "super-secret" not in str(error.value)


def test_create_redis_client_uses_bounded_safe_connection_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_from_url(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("super_ai.redis_runtime.client.Redis.from_url", fake_from_url)

    client = create_redis_client(RedisRuntimeSettings(url="redis://localhost:6379/0"))

    assert client is sentinel
    assert captured == {
        "url": "redis://localhost:6379/0",
        "decode_responses": True,
        "health_check_interval": 30,
        "socket_connect_timeout": 1.0,
        "socket_timeout": 2.0,
        "retry_on_timeout": True,
    }


async def test_ping_redis_returns_healthy_result() -> None:
    class HealthyClient:
        async def ping(self) -> bool:
            return True

    result = await ping_redis(HealthyClient())

    assert result == RedisHealth(ok=True, error=None)


async def test_ping_redis_returns_sanitized_failure_without_raising() -> None:
    class UnhealthyClient:
        async def ping(self) -> bool:
            raise RuntimeError("redis://alice:super-secret@localhost:6379/0 unavailable")

    result = await ping_redis(UnhealthyClient())

    assert result.ok is False
    assert result.error is not None
    assert "super-secret" not in result.error
