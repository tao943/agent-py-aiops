"""Optional Redis lease used only to reduce duplicate database contention."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

from .repositories import RedisMode

_COMPARE_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class RedisLeaseClient(Protocol):
    async def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool,
        px: int,
    ) -> object: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


class AlertLease:
    """One idempotently releasable lease result."""

    def __init__(
        self,
        mode: RedisMode,
        release_callback: Callable[[], Awaitable[None]],
    ) -> None:
        self.mode: RedisMode = mode
        self._release_callback: Callable[[], Awaitable[None]] | None = release_callback

    async def release(self) -> None:
        callback = self._release_callback
        self._release_callback = None
        if callback is not None:
            await callback()


class AlertLeaseManager:
    """Acquire short leases without making Redis a correctness dependency."""

    def __init__(self, client: RedisLeaseClient | None, *, lease_ms: int) -> None:
        self._client = client
        self._lease_ms = lease_ms

    async def acquire(self, source_id: str, group_key_hash: str) -> AlertLease:
        client = self._client
        if client is None:
            return AlertLease("degraded", _noop)
        key = f"agentpy:alert-lease:{source_id}:{group_key_hash}"
        token = secrets.token_hex(16)
        try:
            acquired = await asyncio.wait_for(
                client.set(key, token, nx=True, px=self._lease_ms),
                timeout=0.25,
            )
        except Exception:
            return AlertLease("degraded", _noop)
        if not acquired:
            await asyncio.sleep(min(self._lease_ms / 10_000, 0.05))
            return AlertLease("contended", _noop)
        return AlertLease("primary", lambda: self._release(key, token))

    async def _release(self, key: str, token: str) -> None:
        client = self._client
        if client is None:
            return
        try:
            await asyncio.wait_for(client.eval(_COMPARE_DELETE, 1, key, token), timeout=0.25)
        except Exception:
            return


async def _noop() -> None:
    return
