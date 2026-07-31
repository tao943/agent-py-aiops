# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from super_ai.events.outbox import JobEventPublisher, OutboxDispatcher
from super_ai.events.subscriber import JobEventSubscriber
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.extended_sqlalchemy import (
    SQLAlchemyBackgroundJobRepository,
    SQLAlchemyOutboxEventRepository,
)
from super_ai.memory.models import BackgroundJobEventModel, OutboxEventModel
from super_ai.memory.repositories import JsonDict, OutboxEventRecord, OutboxEventRepository
from super_ai.redis_runtime.config import RedisRuntimeSettings
from super_ai.redis_runtime.streams import RedisStreamJobEventPublisher


class UnavailablePublisher:
    """Dependency-controlled Redis outage; no shared service is stopped."""

    async def publish(self, event: OutboxEventRecord) -> None:
        raise ConnectionError(f"Redis publication unavailable for {event.id}")


class FailFirstPublicationAcknowledgement:
    """Simulate acknowledgement loss after Redis has accepted one event."""

    def __init__(self, repository: OutboxEventRepository) -> None:
        self._repository = repository
        self._failed = False

    async def claim_batch(
        self, *, worker_id: str, limit: int, lease_seconds: int
    ) -> Sequence[OutboxEventRecord]:
        return await self._repository.claim_batch(
            worker_id=worker_id, limit=limit, lease_seconds=lease_seconds
        )

    async def mark_published(self, event_id: str, *, published_at: datetime) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("simulated publication acknowledgement loss")
        await self._repository.mark_published(event_id, published_at=published_at)

    async def release(self, event_id: str, *, error: str, available_at: datetime) -> None:
        await self._repository.release(event_id, error=error, available_at=available_at)


@pytest.mark.asyncio
async def test_redis_recovery_preserves_postgresql_events_and_publishes_each_sequence_once(
    migrated_database_url: str,
) -> None:
    """Redis is optional delivery infrastructure; PostgreSQL remains canonical."""
    redis_prefix = f"task6-{uuid4().hex}"
    settings = RedisRuntimeSettings(
        url="redis://localhost:6379/15",
        stream_prefix=redis_prefix,
        stream_maxlen=20,
        event_dedupe_ttl_seconds=60,
    )
    redis = Redis.from_url(settings.url, decode_responses=True)
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    jobs = SQLAlchemyBackgroundJobRepository(session_factory)
    outbox = SQLAlchemyOutboxEventRepository(session_factory)
    event_payloads: list[JsonDict] = [
        {"type": "job.progress", "step": 1},
        {"type": "job.progress", "step": 2},
        {"type": "complete", "step": 3},
    ]
    try:
        await redis.ping()
        await jobs.enqueue(
            owner_user_id="recovery-owner",
            job_id="recovery-job",
            kind="test",
            resource_type="resource",
            resource_id="recovery-job",
        )
        for payload in event_payloads:
            await jobs.append_event(
                owner_user_id="recovery-owner", job_id="recovery-job", payload=payload
            )

        unavailable_dispatcher = OutboxDispatcher(
            repository=outbox,
            publisher=UnavailablePublisher(),
            worker_id="outage-worker",
            minimum_backoff_seconds=0,
            maximum_backoff_seconds=0,
        )
        await unavailable_dispatcher.run_once()

        async with session_factory() as session:
            canonical = list(
                await session.scalars(
                    select(BackgroundJobEventModel).order_by(BackgroundJobEventModel.sequence)
                )
            )
            pending = list(
                await session.scalars(
                    select(OutboxEventModel).order_by(OutboxEventModel.sequence)
                )
            )
        assert [event.sequence for event in canonical] == [1, 2, 3]
        assert [event.sequence for event in pending] == [1, 2, 3]
        assert all(event.published_at is None for event in pending)

        subscriber = JobEventSubscriber(repository=jobs, relay=None, poll_interval_seconds=0.01)
        events = subscriber.iter_events(
            owner_user_id="recovery-owner", job_id="recovery-job", after_sequence=0
        )
        delivered = [await anext(events) for _ in event_payloads]
        await events.aclose()
        assert [event.sequence for event in delivered] == [1, 2, 3]
        assert [event.payload for event in delivered] == event_payloads

        publisher: JobEventPublisher = RedisStreamJobEventPublisher(redis, settings)
        acknowledgement_loss_dispatcher = OutboxDispatcher(
            repository=FailFirstPublicationAcknowledgement(outbox),
            publisher=publisher,
            worker_id="recovered-worker",
            minimum_backoff_seconds=0,
            maximum_backoff_seconds=0,
        )
        await acknowledgement_loss_dispatcher.run_once()
        await OutboxDispatcher(
            repository=outbox,
            publisher=publisher,
            worker_id="retry-worker",
            minimum_backoff_seconds=0,
            maximum_backoff_seconds=0,
        ).run_once()

        async with session_factory() as session:
            published = list(
                await session.scalars(
                    select(OutboxEventModel).order_by(OutboxEventModel.sequence)
                )
            )
        stream_entries = await redis.xrange(f"{redis_prefix}:aiops:events")
        assert stream_entries is not None
        stream_sequences: list[str] = []
        for _, fields in stream_entries:
            assert fields is not None
            sequence = fields.get("sequence")
            assert sequence is not None
            stream_sequences.append(str(sequence))
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{redis_prefix}:*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await engine.dispose()

    assert all(event.published_at is not None for event in published)
    assert stream_sequences == ["1", "2", "3"]
