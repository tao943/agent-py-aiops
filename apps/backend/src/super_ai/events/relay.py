"""Instance-local Redis Streams wake-up relay.

Operators can inspect abandoned groups with ``XINFO GROUPS <prefix>:aiops:events``
and remove a confirmed-crashed instance only with ``XGROUP DESTROY`` for that
instance's group.  This relay never deletes the shared stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import defaultdict
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from super_ai.redis_runtime.config import RedisRuntimeSettings

logger = logging.getLogger(__name__)


class RedisJobEventRelay:
    """Fan Redis wake metadata into owner-and-job scoped local notifications."""

    def __init__(
        self, *, client: Redis, settings: RedisRuntimeSettings, instance_id: str
    ) -> None:
        self._client = client
        self._stream_key = f"{settings.stream_prefix}:aiops:events"
        self._group_name = f"{settings.stream_prefix}:sse:{instance_id}"
        self._consumer_name = f"{instance_id}:{os.getpid()}"
        self._block_timeout_ms = settings.block_timeout_ms
        self._subscriptions: dict[tuple[str, str], set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._group_created = False

    @property
    def group_name(self) -> str:
        """The sole group this relay is permitted to destroy."""
        return self._group_name

    def subscribe(self, *, owner_user_id: str, job_id: str) -> asyncio.Event:
        """Register a local waiter without exposing raw owner IDs to Redis."""
        wake_up = asyncio.Event()
        key = (_owner_id_hash(owner_user_id), job_id)
        self._subscriptions[key].add(wake_up)
        return wake_up

    def unsubscribe(self, *, owner_user_id: str, job_id: str, wake_up: asyncio.Event) -> None:
        """Remove exactly one local waiter, including cancelled clients."""
        key = (_owner_id_hash(owner_user_id), job_id)
        waiters = self._subscriptions.get(key)
        if waiters is None:
            return
        waiters.discard(wake_up)
        if not waiters:
            del self._subscriptions[key]

    def has_subscription(self, *, owner_user_id: str, job_id: str) -> bool:
        """Return whether this instance has an active scoped subscription."""
        return bool(self._subscriptions.get((_owner_id_hash(owner_user_id), job_id)))

    async def start(self) -> None:
        """Create this instance's group and start one idempotent read loop."""
        if self._task is not None and not self._task.done():
            return
        try:
            await self._client.xgroup_create(
                self._stream_key,
                self._group_name,
                id="$",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_created = True
        self._task = asyncio.create_task(self._run(), name=f"redis-sse-relay:{self._group_name}")

    async def stop(self) -> None:
        """Stop reading and destroy only this relay's own consumer group."""
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        if self._group_created:
            try:
                await self._client.xgroup_destroy(self._stream_key, self._group_name)
            except ResponseError:
                pass
            finally:
                self._group_created = False

    async def _run(self) -> None:
        reconnect_delay_seconds = 0.25
        while True:
            try:
                entries = cast(
                    list[tuple[str, list[tuple[str, dict[str, str]]]]],
                    await self._client.xreadgroup(
                        self._group_name,
                        self._consumer_name,
                        {self._stream_key: ">"},
                        count=100,
                        block=self._block_timeout_ms,
                    ),
                )
                for _, stream_entries in entries:
                    for message_id, fields in stream_entries:
                        self._route(fields)
                        await self._client.xack(self._stream_key, self._group_name, message_id)
                reconnect_delay_seconds = 0.25
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Redis SSE relay degraded: %s", type(exc).__name__)
                await asyncio.sleep(reconnect_delay_seconds)
                reconnect_delay_seconds = min(reconnect_delay_seconds * 2, 5.0)

    def _route(self, fields: dict[str, str]) -> None:
        owner_id_hash = fields.get("owner_id_hash")
        job_id = fields.get("job_id")
        sequence = _positive_sequence(fields.get("sequence"))
        if (
            owner_id_hash is None
            or not _is_owner_hash(owner_id_hash)
            or not job_id
            or sequence is None
        ):
            return
        for wake_up in tuple(self._subscriptions.get((owner_id_hash, job_id), ())):
            wake_up.set()


def _owner_id_hash(owner_user_id: str) -> str:
    return hashlib.sha256(owner_user_id.encode()).hexdigest()


def _is_owner_hash(value: str | None) -> bool:
    if value is None or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _positive_sequence(value: str | None) -> int | None:
    if value is None or not value.isdecimal():
        return None
    sequence = int(value)
    return sequence if sequence > 0 else None
