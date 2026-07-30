from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.extended_sqlalchemy import SQLAlchemyBackgroundJobRepository


@pytest.mark.asyncio
async def test_concurrent_workers_claim_distinct_queued_jobs(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    writer = SQLAlchemyBackgroundJobRepository(session_factory)
    first_worker = SQLAlchemyBackgroundJobRepository(session_factory)
    second_worker = SQLAlchemyBackgroundJobRepository(session_factory)
    now = datetime.now(timezone.utc)
    try:
        await writer.enqueue(
            owner_user_id="owner",
            job_id="job-one",
            kind="test",
            resource_type="resource",
            resource_id="one",
            available_at=now,
        )
        await writer.enqueue(
            owner_user_id="owner",
            job_id="job-two",
            kind="test",
            resource_type="resource",
            resource_id="two",
            available_at=now,
        )

        first, second = await asyncio.gather(
            first_worker.claim_next(
                worker_id="worker-one",
                lease_expires_at=now + timedelta(seconds=30),
                now=now,
            ),
            second_worker.claim_next(
                worker_id="worker-two",
                lease_expires_at=now + timedelta(seconds=30),
                now=now,
            ),
        )
        third = await writer.claim_next(
            worker_id="worker-three",
            lease_expires_at=now + timedelta(seconds=30),
            now=now,
        )
        persisted = await asyncio.gather(
            writer.get(owner_user_id="owner", job_id="job-one"),
            writer.get(owner_user_id="owner", job_id="job-two"),
        )
    finally:
        await engine.dispose()

    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert [job.status for job in persisted] == ["running", "running"]
    assert third is None


@pytest.mark.asyncio
async def test_concurrent_workers_reclaim_an_expired_lease_once(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    writer = SQLAlchemyBackgroundJobRepository(session_factory)
    first_worker = SQLAlchemyBackgroundJobRepository(session_factory)
    second_worker = SQLAlchemyBackgroundJobRepository(session_factory)
    now = datetime.now(timezone.utc)
    try:
        await writer.enqueue(
            owner_user_id="owner",
            job_id="expired-job",
            kind="test",
            resource_type="resource",
            resource_id="expired",
            available_at=now,
        )
        initial = await writer.claim_next(
            worker_id="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
            now=now,
        )

        first, second = await asyncio.gather(
            first_worker.claim_next(
                worker_id="replacement-one",
                lease_expires_at=now + timedelta(seconds=30),
                now=now,
            ),
            second_worker.claim_next(
                worker_id="replacement-two",
                lease_expires_at=now + timedelta(seconds=30),
                now=now,
            ),
        )
        persisted = await writer.get(owner_user_id="owner", job_id="expired-job")
    finally:
        await engine.dispose()

    assert initial is not None
    reclaimed = [job for job in (first, second) if job is not None]
    assert [job.id for job in reclaimed] == ["expired-job"]
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.lease_owner in {"replacement-one", "replacement-two"}


@pytest.mark.asyncio
async def test_concurrent_events_receive_unique_ordered_sequences(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    writer = SQLAlchemyBackgroundJobRepository(session_factory)
    repositories = [SQLAlchemyBackgroundJobRepository(session_factory) for _ in range(20)]
    try:
        await writer.enqueue(
            owner_user_id="owner",
            job_id="event-job",
            kind="test",
            resource_type="resource",
            resource_id="events",
        )
        await asyncio.gather(
            *(
                repository.append_event(
                    owner_user_id="owner",
                    job_id="event-job",
                    payload={"event": index},
                )
                for index, repository in enumerate(repositories, start=1)
            )
        )
        events = await writer.list_events(owner_user_id="owner", job_id="event-job")
    finally:
        await engine.dispose()

    sequences = [event.sequence for event in events]
    assert sequences == list(range(1, 21))
    assert len(set(sequences)) == 20
