from __future__ import annotations

from typing import Any

from super_ai.alert_ingestion.redis_runtime import AlertLeaseManager


class RecordingRedis:
    def __init__(self, *, set_result: bool = True, fail: bool = False) -> None:
        self.set_result = set_result
        self.fail = fail
        self.set_calls: list[tuple[str, str, bool, int]] = []
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool,
        px: int,
    ) -> bool:
        if self.fail:
            raise TimeoutError("redis unavailable")
        self.set_calls.append((name, value, nx, px))
        return self.set_result

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        if self.fail:
            raise TimeoutError("redis unavailable")
        self.eval_calls.append((script, numkeys, keys_and_args))
        return 1


async def test_lease_uses_set_nx_px_and_compare_delete() -> None:
    client = RecordingRedis()
    lease = await AlertLeaseManager(client, lease_ms=2000).acquire("source", "a" * 64)

    await lease.release()

    assert lease.mode == "primary"
    key, token, nx, px = client.set_calls[0]
    assert key == f"agentpy:alert-lease:source:{'a' * 64}"
    assert token
    assert nx is True
    assert px == 2000
    script, numkeys, keys_and_args = client.eval_calls[0]
    assert "redis.call('get', KEYS[1])" in script
    assert "redis.call('del', KEYS[1])" in script
    assert numkeys == 1
    assert keys_and_args == (key, token)


async def test_lease_contention_waits_then_allows_postgresql_path() -> None:
    client = RecordingRedis(set_result=False)

    lease = await AlertLeaseManager(client, lease_ms=2000).acquire("source", "b" * 64)
    await lease.release()

    assert lease.mode == "contended"
    assert client.eval_calls == []


async def test_redis_failure_returns_degraded_lease_without_raising() -> None:
    client = RecordingRedis(fail=True)

    lease = await AlertLeaseManager(client, lease_ms=2000).acquire("source", "c" * 64)
    await lease.release()

    assert lease.mode == "degraded"


async def test_release_failure_is_safely_ignored() -> None:
    client = RecordingRedis()
    lease = await AlertLeaseManager(client, lease_ms=2000).acquire("source", "d" * 64)
    client.fail = True

    await lease.release()

    assert lease.mode == "degraded"
