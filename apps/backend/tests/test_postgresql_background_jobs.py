from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.extended_sqlalchemy import SQLAlchemyBackgroundJobRepository
from super_ai.memory.models import BackgroundJobModel


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
    persisted_statuses: list[str] = []
    for job in persisted:
        assert job is not None
        persisted_statuses.append(job.status)
    assert persisted_statuses == ["running", "running"]
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


@pytest.mark.asyncio
async def test_events_for_jobs_with_matching_suffixes_have_unique_ids(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    repository = SQLAlchemyBackgroundJobRepository(session_factory)
    first_job_id = "first-job-1234567890abcdef"
    second_job_id = "second-job-1234567890abcdef"
    try:
        for job_id in (first_job_id, second_job_id):
            await repository.enqueue(
                owner_user_id="owner",
                job_id=job_id,
                kind="test",
                resource_type="resource",
                resource_id=job_id,
            )
        first_event = await repository.append_event(
            owner_user_id="owner",
            job_id=first_job_id,
            payload={"event": "first"},
        )
        second_event = await repository.append_event(
            owner_user_id="owner",
            job_id=second_job_id,
            payload={"event": "second"},
        )
    finally:
        await engine.dispose()

    assert [first_event.sequence, second_event.sequence] == [1, 1]
    assert first_event.id != second_event.id


@pytest.mark.asyncio
async def test_claim_next_skips_a_candidate_locked_by_another_transaction(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    repository = SQLAlchemyBackgroundJobRepository(session_factory)
    now = datetime.now(timezone.utc)
    try:
        await repository.enqueue(
            owner_user_id="owner",
            job_id="first-candidate",
            kind="test",
            resource_type="resource",
            resource_id="first",
            available_at=now,
        )
        await repository.enqueue(
            owner_user_id="owner",
            job_id="second-candidate",
            kind="test",
            resource_type="resource",
            resource_id="second",
            available_at=now,
        )

        async with session_factory() as locking_session:
            async with locking_session.begin():
                locked = (
                    await locking_session.scalars(
                        select(BackgroundJobModel)
                        .where(
                            BackgroundJobModel.status == "queued",
                            BackgroundJobModel.available_at <= now,
                            BackgroundJobModel.cancel_requested_at.is_(None),
                        )
                        .order_by(
                            BackgroundJobModel.available_at.asc(),
                            BackgroundJobModel.created_at.asc(),
                            BackgroundJobModel.id.asc(),
                        )
                        .with_for_update()
                        .limit(1)
                    )
                ).one()
                assert locked.id == "first-candidate"
                while_locked = await asyncio.wait_for(
                    repository.claim_next(
                        worker_id="worker-one",
                        lease_expires_at=now + timedelta(seconds=30),
                        now=now,
                    ),
                    timeout=1,
                )

        after_release = await repository.claim_next(
            worker_id="worker-two",
            lease_expires_at=now + timedelta(seconds=30),
            now=now,
        )
    finally:
        await engine.dispose()

    assert while_locked is not None
    assert while_locked.id == "second-candidate"
    assert after_release is not None
    assert after_release.id == "first-candidate"
