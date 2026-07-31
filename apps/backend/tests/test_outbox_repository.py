from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.extended_sqlalchemy import (
    SQLAlchemyBackgroundJobRepository,
    SQLAlchemyOutboxEventRepository,
)
from super_ai.memory.models import BackgroundJobEventModel, OutboxEventModel
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


async def _enqueue_job(repository: SQLAlchemyBackgroundJobRepository, job_id: str) -> None:
    await repository.enqueue(
        owner_user_id="owner",
        job_id=job_id,
        kind="test",
        resource_type="resource",
        resource_id=job_id,
    )


@pytest.mark.asyncio
async def test_append_event_commits_canonical_and_outbox_rows_atomically(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    jobs = SQLAlchemyBackgroundJobRepository(session_factory)
    try:
        await _enqueue_job(jobs, "atomic-job")

        event = await jobs.append_event(
            owner_user_id="owner",
            job_id="atomic-job",
            payload={"type": "job.progress", "percent": 50},
        )

        async with session_factory() as session:
            canonical = await session.scalar(
                select(func.count()).select_from(BackgroundJobEventModel)
            )
            outbox = (await session.scalars(select(OutboxEventModel))).one()
    finally:
        await engine.dispose()

    assert canonical == 1
    assert event.id == outbox.id
    assert outbox.owner_user_id == "owner"
    assert outbox.aggregate_type == "background_job"
    assert outbox.aggregate_id == "atomic-job"
    assert outbox.sequence == event.sequence
    assert outbox.event_type == "job.progress"
    assert outbox.payload == {"type": "job.progress", "percent": 50}
    assert outbox.created_at.tzinfo is not None
    assert outbox.available_at.tzinfo is not None


@pytest.mark.asyncio
async def test_append_event_rolls_back_both_rows_when_outbox_payload_cannot_flush(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    jobs = SQLAlchemyBackgroundJobRepository(session_factory)
    try:
        await _enqueue_job(jobs, "rollback-job")

        with pytest.raises(StatementError):
            await jobs.append_event(
                owner_user_id="owner",
                job_id="rollback-job",
                payload={"type": "job.progress", "invalid": {"not-json"}},
            )

        async with session_factory() as session:
            canonical = await session.scalar(
                select(func.count()).select_from(BackgroundJobEventModel)
            )
            outbox = await session.scalar(select(func.count()).select_from(OutboxEventModel))
    finally:
        await engine.dispose()

    assert canonical == 0
    assert outbox == 0


@pytest.mark.asyncio
async def test_claim_batch_gives_concurrent_dispatchers_disjoint_rows(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    jobs = SQLAlchemyBackgroundJobRepository(session_factory)
    first = SQLAlchemyOutboxEventRepository(session_factory)
    second = SQLAlchemyOutboxEventRepository(session_factory)
    try:
        for job_id in ("claim-one", "claim-two"):
            await _enqueue_job(jobs, job_id)
            await jobs.append_event(
                owner_user_id="owner",
                job_id=job_id,
                payload={"type": "job.progress", "job": job_id},
            )

        first_claim, second_claim = await asyncio.gather(
            first.claim_batch(worker_id="worker-one", limit=1, lease_seconds=30),
            second.claim_batch(worker_id="worker-two", limit=1, lease_seconds=30),
        )
    finally:
        await engine.dispose()

    assert len(first_claim) == 1
    assert len(second_claim) == 1
    assert {event.id for event in first_claim}.isdisjoint(event.id for event in second_claim)
    assert {event.claimed_by for event in (*first_claim, *second_claim)} == {
        "worker-one",
        "worker-two",
    }


@pytest.mark.asyncio
async def test_claim_batch_respects_active_lease_and_reclaims_expired_lease_once(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    jobs = SQLAlchemyBackgroundJobRepository(session_factory)
    initial = SQLAlchemyOutboxEventRepository(session_factory)
    replacement_one = SQLAlchemyOutboxEventRepository(session_factory)
    replacement_two = SQLAlchemyOutboxEventRepository(session_factory)
    try:
        await _enqueue_job(jobs, "lease-job")
        event = await jobs.append_event(
            owner_user_id="owner",
            job_id="lease-job",
            payload={"type": "job.progress"},
        )

        assert [row.id for row in await initial.claim_batch(
            worker_id="first", limit=1, lease_seconds=60
        )] == [event.id]
        assert await replacement_one.claim_batch(
            worker_id="second", limit=1, lease_seconds=60
        ) == []

        async with session_factory() as session, session.begin():
            row = await session.get(OutboxEventModel, event.id)
            assert row is not None
            row.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        claimed = await asyncio.gather(
            replacement_one.claim_batch(worker_id="second", limit=1, lease_seconds=60),
            replacement_two.claim_batch(worker_id="third", limit=1, lease_seconds=60),
        )
    finally:
        await engine.dispose()

    reclaimed = [row for batch in claimed for row in batch]
    assert [row.id for row in reclaimed] == [event.id]
    assert reclaimed[0].attempt_count == 2


@pytest.mark.asyncio
async def test_release_reschedules_claim_and_mark_published_is_idempotent(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    jobs = SQLAlchemyBackgroundJobRepository(session_factory)
    outbox = SQLAlchemyOutboxEventRepository(session_factory)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    published_at = retry_at + timedelta(minutes=1)
    try:
        await _enqueue_job(jobs, "release-job")
        event = await jobs.append_event(
            owner_user_id="owner",
            job_id="release-job",
            payload={"status": "update"},
        )
        [claimed] = await outbox.claim_batch(worker_id="worker", limit=1, lease_seconds=60)

        await outbox.release(
            claimed.id,
            error="x" * 10_000,
            available_at=retry_at,
        )
        async with session_factory() as session:
            released = await session.get(OutboxEventModel, event.id)
            assert released is not None
            assert released.claimed_by is None
            assert released.claim_expires_at is None
            assert released.available_at == retry_at
            assert released.last_error is not None
            assert len(released.last_error) <= 1_000
            assert released.event_type == "background_job.event"

        await outbox.mark_published(event.id, published_at=published_at)
        await outbox.mark_published(event.id, published_at=published_at - timedelta(seconds=1))
        async with session_factory() as session:
            published = await session.get(OutboxEventModel, event.id)
            assert published is not None
    finally:
        await engine.dispose()

    assert published.published_at == published_at
    assert published.claimed_by is None
    assert published.claim_expires_at is None


def test_memory_repository_bundle_exposes_outbox_repository(
    migrated_database_url_session: str,
) -> None:
    engine = create_memory_engine(migrated_database_url_session)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
    finally:
        engine.sync_engine.dispose()

    assert isinstance(repositories.outbox_events, SQLAlchemyOutboxEventRepository)
