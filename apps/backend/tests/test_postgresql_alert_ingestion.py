from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.alert_ingestion.domain import AlertDeliveryStatus
from super_ai.alert_ingestion.repositories import AlertPersistenceError, IngestionWrite
from super_ai.alert_ingestion.sqlalchemy import SQLAlchemyAlertIngestionRepository
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import (
    AlertEventModel,
    AlertIncidentModel,
    BackgroundJobModel,
    DiagnosticTaskModel,
    UserModel,
)


def _write(
    sequence: int,
    *,
    status: AlertDeliveryStatus = "firing",
    filtered: bool = False,
    group_hash: str = "a" * 64,
    payload_hash: str | None = None,
) -> IngestionWrite:
    received_at = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc) + timedelta(
        milliseconds=sequence
    )
    return IngestionWrite(
        owner_user_id="owner",
        source_id="local-alertmanager",
        status=status,
        group_key_hash=group_hash,
        payload_sha256=payload_hash or f"{sequence:064x}",
        normalized_payload={"version": "4", "status": status, "alerts": []},
        query="Investigate HighLatency affecting order-service.",
        safe_alert={"labels": {"service": "order-service"}},
        filtered=filtered,
        received_at=received_at,
        alert_name="HighLatency",
        service="order-service",
        severity="critical",
        starts_at=received_at,
    )


async def _seed_user(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            UserModel(
                id="owner",
                email="owner@example.test",
                display_name="Owner",
                password_hash="not-used",
            )
        )


async def test_twenty_concurrent_firings_create_one_incident_task_and_job(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_user(session_factory)
    repositories = [SQLAlchemyAlertIngestionRepository(session_factory) for _ in range(20)]
    try:
        results = await asyncio.gather(
            *(repository.apply(_write(index)) for index, repository in enumerate(repositories))
        )
        async with session_factory() as session:
            incident_count = await session.scalar(select(func.count(AlertIncidentModel.id)))
            task_count = await session.scalar(select(func.count(DiagnosticTaskModel.id)))
            job_count = await session.scalar(select(func.count(BackgroundJobModel.id)))
            event_count = await session.scalar(select(func.count(AlertEventModel.id)))
            incident = (await session.scalars(select(AlertIncidentModel))).one()
            task = (await session.scalars(select(DiagnosticTaskModel))).one()
            job = (await session.scalars(select(BackgroundJobModel))).one()
    finally:
        await engine.dispose()

    assert sum(result.disposition == "incident_created" for result in results) == 1
    assert {result.incident_id for result in results} == {incident.id}
    assert incident_count == task_count == job_count == 1
    assert event_count == 20
    assert incident.delivery_count == 20
    assert incident.diagnostic_task_id == task.id == job.resource_id
    assert task.input_payload == {
        "query": "Investigate HighLatency affecting order-service.",
        "alert": {"labels": {"service": "order-service"}},
    }
    assert "executionPermitted" not in task.input_payload


async def test_identical_delivery_retry_updates_count_but_deduplicates_event(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_user(session_factory)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        first = await repository.apply(_write(1, payload_hash="b" * 64))
        second = await repository.apply(_write(2, payload_hash="b" * 64))
        async with session_factory() as session:
            incident = (await session.scalars(select(AlertIncidentModel))).one()
            event_count = await session.scalar(select(func.count(AlertEventModel.id)))
    finally:
        await engine.dispose()

    assert first.disposition == "incident_created"
    assert second.disposition == "duplicate_updated"
    assert second.diagnostic_task_id == first.diagnostic_task_id
    assert incident.delivery_count == 2
    assert event_count == 1


async def test_resolved_closes_active_incident_and_firing_reopens_new_lifecycle(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_user(session_factory)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        first = await repository.apply(_write(1))
        resolved = await repository.apply(_write(2, status="resolved"))
        reopened = await repository.apply(_write(3))
        async with session_factory() as session:
            incidents = list(
                (
                    await session.scalars(
                        select(AlertIncidentModel).order_by(AlertIncidentModel.created_at)
                    )
                ).all()
            )
            task_count = await session.scalar(select(func.count(DiagnosticTaskModel.id)))
    finally:
        await engine.dispose()

    assert resolved.disposition == "incident_resolved"
    assert resolved.diagnostic_task_id == first.diagnostic_task_id
    assert reopened.disposition == "incident_created"
    assert reopened.incident_id != first.incident_id
    assert [incident.status for incident in incidents] == ["resolved", "active"]
    assert task_count == 2


async def test_filtered_and_orphan_resolved_only_create_audit_events(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_user(session_factory)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        filtered = await repository.apply(_write(1, filtered=True))
        orphan = await repository.apply(_write(2, status="resolved", group_hash="c" * 64))
        async with session_factory() as session:
            incident_count = await session.scalar(select(func.count(AlertIncidentModel.id)))
            task_count = await session.scalar(select(func.count(DiagnosticTaskModel.id)))
            dispositions = set((await session.scalars(select(AlertEventModel.disposition))).all())
    finally:
        await engine.dispose()

    assert filtered.disposition == "filtered"
    assert orphan.disposition == "orphan_resolved"
    assert filtered.incident_id is orphan.incident_id is None
    assert incident_count == task_count == 0
    assert dispositions == {"filtered", "orphan_resolved"}


async def test_repository_remains_usable_after_active_uniqueness_contention(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_user(session_factory)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        await asyncio.gather(repository.apply(_write(1)), repository.apply(_write(2)))
        follow_up = await repository.apply(_write(3, group_hash="d" * 64))
    finally:
        await engine.dispose()

    assert follow_up.disposition == "incident_created"


async def test_commit_failure_is_safely_wrapped_and_rolls_back_all_rows(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        with pytest.raises(AlertPersistenceError, match="unavailable"):
            await repository.apply(_write(1))
        async with session_factory() as session:
            incident_count = await session.scalar(select(func.count(AlertIncidentModel.id)))
            task_count = await session.scalar(select(func.count(DiagnosticTaskModel.id)))
            job_count = await session.scalar(select(func.count(BackgroundJobModel.id)))
        await _seed_user(session_factory)
        recovered = await repository.apply(_write(2))
    finally:
        await engine.dispose()

    assert incident_count == task_count == job_count == 0
    assert recovered.disposition == "incident_created"
