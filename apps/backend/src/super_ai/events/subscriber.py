"""PostgreSQL-canonical background-job event subscriptions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Protocol

from super_ai.memory.repositories import BackgroundJobEventRecord

logger = logging.getLogger(__name__)


class JobEventWakeRelay(Protocol):
    """Local wake-up registry backed by an optional Redis relay."""

    def subscribe(self, *, owner_user_id: str, job_id: str) -> asyncio.Event:
        """Register and return the local wake-up event for one owned job."""
        ...

    def unsubscribe(self, *, owner_user_id: str, job_id: str, wake_up: asyncio.Event) -> None:
        """Remove precisely the subscription installed by ``subscribe``."""
        ...


class JobEventRepository(Protocol):
    """Canonical event-log reads needed by an SSE subscriber."""

    async def list_events(
        self, *, owner_user_id: str, job_id: str, after_sequence: int = 0
    ) -> list[BackgroundJobEventRecord]:
        """List durable events after the subscriber cursor."""
        ...

class JobEventSubscriber:
    """Yield durable job events, using Redis only to reduce polling latency."""

    def __init__(
        self,
        *,
        repository: JobEventRepository,
        relay: JobEventWakeRelay | None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._repository = repository
        self._relay = relay
        self._poll_interval_seconds = poll_interval_seconds

    async def iter_events(
        self,
        *,
        owner_user_id: str,
        job_id: str,
        after_sequence: int,
    ) -> AsyncGenerator[BackgroundJobEventRecord, None]:
        """Read every delivered event from PostgreSQL in strictly rising sequence order."""
        last_delivered_sequence = after_sequence
        wake_up: asyncio.Event | None = None
        retry_delay_seconds = self._poll_interval_seconds

        try:
            while True:
                events = await self._repository.list_events(
                    owner_user_id=owner_user_id,
                    job_id=job_id,
                    after_sequence=last_delivered_sequence,
                )
                for event in events:
                    if event.sequence <= last_delivered_sequence:
                        continue
                    last_delivered_sequence = event.sequence
                    yield event

                if wake_up is None and self._relay is not None:
                    try:
                        wake_up = self._relay.subscribe(
                            owner_user_id=owner_user_id,
                            job_id=job_id,
                        )
                        retry_delay_seconds = self._poll_interval_seconds
                        continue
                    except Exception as exc:
                        logger.warning(
                            "Redis SSE relay unavailable; using PostgreSQL polling: %s",
                            type(exc).__name__,
                        )
                if wake_up is None:
                    await asyncio.sleep(
                        retry_delay_seconds
                        if self._relay is not None
                        else self._poll_interval_seconds
                    )
                    if self._relay is not None:
                        retry_delay_seconds = min(retry_delay_seconds * 2, 5.0)
                    continue

                try:
                    await asyncio.wait_for(wake_up.wait(), timeout=self._poll_interval_seconds)
                except TimeoutError:
                    pass
                finally:
                    wake_up.clear()
        finally:
            if wake_up is not None and self._relay is not None:
                self._relay.unsubscribe(
                    owner_user_id=owner_user_id,
                    job_id=job_id,
                    wake_up=wake_up,
                )
