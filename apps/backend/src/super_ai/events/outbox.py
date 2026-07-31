"""Dispatch durable Outbox records through a publisher boundary."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol

from super_ai.memory.repositories import OutboxEventRecord, OutboxEventRepository

logger = logging.getLogger(__name__)


class JobEventPublisher(Protocol):
    """Publishes an Outbox event to a non-canonical delivery transport."""

    async def publish(self, event: OutboxEventRecord) -> None:
        """Acknowledge the event only after the transport accepts it."""
        ...


class OutboxDispatcher:
    """Claim, publish, and acknowledge durable Outbox records."""

    def __init__(
        self,
        *,
        repository: OutboxEventRepository,
        publisher: JobEventPublisher,
        worker_id: str,
        batch_size: int = 100,
        lease_seconds: int = 30,
        minimum_backoff_seconds: int = 1,
        maximum_backoff_seconds: int = 60,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._minimum_backoff_seconds = minimum_backoff_seconds
        self._maximum_backoff_seconds = maximum_backoff_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start one idempotent polling task for this dispatcher instance."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"outbox-dispatcher:{self._worker_id}"
            )

    async def stop(self) -> None:
        """Cancel and await the polling task, leaving no retained task reference."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run_once(self) -> None:
        """Publish one bounded set of claimed rows."""
        events: Sequence[OutboxEventRecord] = await self._repository.claim_batch(
            worker_id=self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        for event in events:
            started_at = time.monotonic()
            try:
                await self._publisher.publish(event)
                await self._repository.mark_published(
                    event.id, published_at=datetime.now(timezone.utc)
                )
            except asyncio.CancelledError:
                await self._release(event, "CancelledError")
                logger.info(
                    "Outbox publication cancelled event_id=%s aggregate_id=%s "
                    "attempt=%s latency_ms=%d",
                    event.id,
                    event.aggregate_id,
                    event.attempt_count,
                    _latency_ms(started_at),
                )
                raise
            except Exception as exc:
                await self._release(event, type(exc).__name__)
                logger.warning(
                    "Outbox publication failed event_id=%s aggregate_id=%s "
                    "attempt=%s latency_ms=%d error=%s",
                    event.id,
                    event.aggregate_id,
                    event.attempt_count,
                    _latency_ms(started_at),
                    type(exc).__name__,
                )
            else:
                logger.info(
                    "Outbox publication acknowledged event_id=%s aggregate_id=%s "
                    "attempt=%s latency_ms=%d",
                    event.id,
                    event.aggregate_id,
                    event.attempt_count,
                    _latency_ms(started_at),
                )

    async def _run(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._poll_interval_seconds)

    async def _release(self, event: OutboxEventRecord, error: str) -> None:
        await self._repository.release(
            event.id,
            error=error[:1_000],
            available_at=datetime.now(timezone.utc)
            + timedelta(seconds=self._backoff_seconds(event.attempt_count)),
        )

    def _backoff_seconds(self, attempt_count: int) -> int:
        exponent = max(attempt_count - 1, 0)
        return min(self._minimum_backoff_seconds * (2**exponent), self._maximum_backoff_seconds)


def _latency_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1_000)
