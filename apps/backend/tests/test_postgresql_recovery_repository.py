from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import (
    AlertIncidentModel,
    BackgroundJobModel,
    DiagnosticReportModel,
    DiagnosticTaskModel,
    ProductionRecoveryApprovalModel,
    ProductionRecoveryAuditEventModel,
    ProductionRecoveryIntentModel,
    UserModel,
)
from super_ai.recovery.contracts import RecoveryStatus
from super_ai.recovery.repository import RecoveryIntentCreate
from super_ai.recovery.sqlalchemy import SQLAlchemyRecoveryIntentRepository


async def _seed_scope(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            UserModel(
                id="owner-a",
                email="owner-a@example.test",
                display_name="Owner A",
                password_hash="hash",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            DiagnosticTaskModel(
                id="diagnostic-a",
                owner_user_id="owner-a",
                status="succeeded",
                query="Diagnose order service",
                input_payload={},
                result_payload={},
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        session.add(
            DiagnosticReportModel(
                id="report-a",
                owner_user_id="owner-a",
                task_id="diagnostic-a",
                title="Order pool leak",
                content="Grounded report",
                payload={},
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            AlertIncidentModel(
                id="incident-a",
                owner_user_id="owner-a",
                source_id="local-alertmanager",
                group_key_hash="a" * 64,
                run_id=None,
                scenario_id=None,
                status="active",
                alert_name="OrderPoolExhausted",
                service="order-service",
                severity="critical",
                starts_at=now,
                last_seen_at=now,
                resolved_at=None,
                verification_status="pending",
                verified_at=None,
                verification_summary=None,
                delivery_count=1,
                diagnostic_task_id="diagnostic-a",
                created_at=now,
                updated_at=now,
            )
        )


def _request(
    *, intent_id: str, status: RecoveryStatus = "queued"
) -> RecoveryIntentCreate:
    return RecoveryIntentCreate(
        id=intent_id,
        owner_user_id="owner-a",
        incident_id="incident-a",
        diagnostic_task_id="diagnostic-a",
        report_id="report-a",
        action="restart_compose_service",
        target_key="live-eval-order-api",
        canonical_arguments={"service": "live-eval-order-api"},
        proposal_fingerprint="f" * 64,
        evidence_ids=("evidence-a",),
        validator_origin="deterministic_grounded",
        policy_authorization_code="compose_auto_allowlisted",
        risk_tier="low",
        automatic_eligible=True,
        approval_required=status == "awaiting_approval",
        status=status,
        trusted_snapshot={"reportFingerprint": "r" * 64},
    )


@pytest.mark.asyncio
async def test_concurrent_create_reuses_one_active_intent_job_and_event(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    factory = create_memory_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        await _seed_scope(factory, now=now)
        first = SQLAlchemyRecoveryIntentRepository(factory)
        second = SQLAlchemyRecoveryIntentRepository(factory)
        results = await asyncio.gather(
            first.create_intent_with_job_and_event(
                _request(intent_id="intent-a"),
                background_job_id="job-a",
                event_id="event-a",
                now=now,
            ),
            second.create_intent_with_job_and_event(
                _request(intent_id="intent-b"),
                background_job_id="job-b",
                event_id="event-b",
                now=now,
            ),
        )
        async with factory() as session:
            intent_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryIntentModel)
            )
            job_count = await session.scalar(
                select(func.count()).select_from(BackgroundJobModel)
            )
            event_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryAuditEventModel)
            )
    finally:
        await engine.dispose()

    assert {result.reused for result in results} == {False, True}
    assert len({result.intent.id for result in results}) == 1
    assert (intent_count, job_count, event_count) == (1, 1, 1)


@pytest.mark.asyncio
async def test_owner_scope_and_event_cursor_are_enforced(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    factory = create_memory_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        await _seed_scope(factory, now=now)
        repository = SQLAlchemyRecoveryIntentRepository(factory)
        created = await repository.create_intent_with_job_and_event(
            _request(intent_id="intent-owned"),
            background_job_id="job-owned",
            event_id="event-owned",
            now=now,
        )
        owned = await repository.get_owned(owner_user_id="owner-a", intent_id="intent-owned")
        foreign = await repository.get_owned(owner_user_id="owner-b", intent_id="intent-owned")
        events = await repository.list_events(
            owner_user_id="owner-a", intent_id="intent-owned", after_sequence=0
        )
        no_events = await repository.list_events(
            owner_user_id="owner-a", intent_id="intent-owned", after_sequence=1
        )
    finally:
        await engine.dispose()

    assert created.intent.id == "intent-owned"
    assert owned is not None
    assert foreign is None
    assert [event.sequence for event in events] == [1]
    assert no_events == []


@pytest.mark.parametrize("failure_stage", ["after_intent", "after_job", "after_event"])
@pytest.mark.asyncio
async def test_create_unit_of_work_rolls_back_every_partial_write(
    migrated_database_url: str,
    failure_stage: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    factory = create_memory_session_factory(engine)
    now = datetime.now(timezone.utc)

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("injected_transaction_failure")

    try:
        await _seed_scope(factory, now=now)
        repository = SQLAlchemyRecoveryIntentRepository(factory, failure_injector=fail)
        with pytest.raises(RuntimeError, match="injected_transaction_failure"):
            await repository.create_intent_with_job_and_event(
                _request(intent_id="intent-rollback"),
                background_job_id="job-rollback",
                event_id="event-rollback",
                now=now,
            )
        async with factory() as session:
            intent_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryIntentModel)
            )
            job_count = await session.scalar(
                select(func.count()).select_from(BackgroundJobModel)
            )
            event_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryAuditEventModel)
            )
    finally:
        await engine.dispose()

    assert (intent_count, job_count, event_count) == (0, 0, 0)


@pytest.mark.parametrize("failure_stage", ["after_approval", "after_job", "after_event"])
@pytest.mark.asyncio
async def test_approval_unit_of_work_rolls_back_to_awaiting_approval(
    migrated_database_url: str,
    failure_stage: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    factory = create_memory_session_factory(engine)
    now = datetime.now(timezone.utc)

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("injected_approval_failure")

    try:
        await _seed_scope(factory, now=now)
        base = SQLAlchemyRecoveryIntentRepository(factory)
        await base.create_intent_with_job_and_event(
            _request(intent_id="intent-approval", status="awaiting_approval"),
            background_job_id=None,
            event_id="event-created",
            now=now,
        )
        failing = SQLAlchemyRecoveryIntentRepository(factory, failure_injector=fail)
        with pytest.raises(RuntimeError, match="injected_approval_failure"):
            await failing.approve_with_job_and_event(
                owner_user_id="owner-a",
                intent_id="intent-approval",
                approval_id="approval-a",
                confirmation_fingerprint="c" * 64,
                background_job_id="job-approved",
                event_id="event-approved",
                now=now,
                expires_at=now + timedelta(minutes=10),
            )
        stored = await base.get_owned(
            owner_user_id="owner-a", intent_id="intent-approval"
        )
        async with factory() as session:
            approval_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryApprovalModel)
            )
            job_count = await session.scalar(
                select(func.count()).select_from(BackgroundJobModel)
            )
            event_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryAuditEventModel)
            )
    finally:
        await engine.dispose()

    assert stored is not None and stored.status == "awaiting_approval"
    assert (approval_count, job_count, event_count) == (0, 0, 1)


@pytest.mark.asyncio
async def test_concurrent_approval_converges_to_one_job_and_approval(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    factory = create_memory_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        await _seed_scope(factory, now=now)
        repository = SQLAlchemyRecoveryIntentRepository(factory)
        await repository.create_intent_with_job_and_event(
            _request(intent_id="intent-concurrent", status="awaiting_approval"),
            background_job_id=None,
            event_id="event-created",
            now=now,
        )
        results = await asyncio.gather(
            repository.approve_with_job_and_event(
                owner_user_id="owner-a",
                intent_id="intent-concurrent",
                approval_id="approval-a",
                confirmation_fingerprint="c" * 64,
                background_job_id="job-a",
                event_id="event-a",
                now=now,
                expires_at=now + timedelta(minutes=10),
            ),
            repository.approve_with_job_and_event(
                owner_user_id="owner-a",
                intent_id="intent-concurrent",
                approval_id="approval-b",
                confirmation_fingerprint="c" * 64,
                background_job_id="job-b",
                event_id="event-b",
                now=now,
                expires_at=now + timedelta(minutes=10),
            ),
        )
        async with factory() as session:
            approval_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryApprovalModel)
            )
            job_count = await session.scalar(
                select(func.count()).select_from(BackgroundJobModel)
            )
    finally:
        await engine.dispose()

    assert all(result is not None and result.status == "queued" for result in results)
    assert (approval_count, job_count) == (1, 1)
