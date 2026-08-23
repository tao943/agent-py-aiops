from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from super_ai.alert_ingestion.repositories import DiagnosticScheduleResult
from super_ai.chat.aiops_bridge import RecoveryApprovalRequest
from super_ai.chat.pending_action_jobs import PendingChatActionJobHandler
from super_ai.chat.pending_actions import (
    PendingActionNotFound,
    PendingChatActionService,
)
from super_ai.jobs import BackgroundJobContext
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel, utc_now
from super_ai.memory.repositories import MemoryRepositories
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


async def _service(
    database_url: str,
) -> tuple[AsyncEngine, MemoryRepositories, PendingChatActionService]:
    engine = create_memory_engine(database_url)
    session_factory = create_memory_session_factory(engine)
    async with session_factory() as session:
        now = utc_now()
        session.add_all(
            (
                UserModel(
                    id="owner_a",
                    email="pending-a@example.com",
                    display_name="Pending A",
                    password_hash="unused",
                    created_at=now,
                    updated_at=now,
                ),
                UserModel(
                    id="owner_b",
                    email="pending-b@example.com",
                    display_name="Pending B",
                    password_hash="unused",
                    created_at=now,
                    updated_at=now,
                ),
            )
        )
        await session.commit()
    repositories = create_sqlalchemy_memory_repositories(session_factory)
    await repositories.chat.create_session(owner_user_id="owner_a", session_id="session_a")
    assert (
        await repositories.chat.get_session(
            owner_user_id="owner_a", session_id="session_a"
        )
        is not None
    )
    assert repositories.pending_chat_actions is not None
    return engine, repositories, PendingChatActionService(
        repositories.pending_chat_actions
    )


@pytest.mark.asyncio
async def test_concurrent_confirm_enqueues_one_stable_job(
    migrated_database_url: str,
) -> None:
    engine, _, service = await _service(migrated_database_url)
    try:
        action = await service.preview_start(
            owner_user_id="owner_a",
            session_id="session_a",
            incident_id="incident_1",
        )

        first, second = await asyncio.gather(
            service.confirm(owner_user_id="owner_a", action_id=action.id),
            service.confirm(owner_user_id="owner_a", action_id=action.id),
        )

        assert first.status == second.status == "confirmed"
        assert first.background_job_id == second.background_job_id
        assert first.background_job_id == f"job_chat_action_{action.id}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_owner_and_expired_actions_do_not_confirm(
    migrated_database_url: str,
) -> None:
    engine, _, service = await _service(migrated_database_url)
    try:
        action = await service.preview_start(
            owner_user_id="owner_a",
            session_id="session_a",
            incident_id="incident_2",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        with pytest.raises(PendingActionNotFound):
            await service.confirm(owner_user_id="owner_b", action_id=action.id)
        expired = await service.confirm(owner_user_id="owner_a", action_id=action.id)

        assert expired.status == "expired"
        assert expired.background_job_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_history_does_not_block_new_action(
    migrated_database_url: str,
) -> None:
    engine, _, service = await _service(migrated_database_url)
    try:
        first = await service.preview_start(
            owner_user_id="owner_a",
            session_id="session_a",
            incident_id="incident_3",
        )
        cancelled = await service.cancel(owner_user_id="owner_a", action_id=first.id)
        second = await service.preview_start(
            owner_user_id="owner_a",
            session_id="session_a",
            incident_id="incident_3",
        )

        assert cancelled.status == "cancelled"
        assert second.id != first.id
        assert second.status == "pending"
    finally:
        await engine.dispose()


class IdempotentFakeBridge:
    def __init__(self) -> None:
        self.start_calls = 0

    async def start_incident_diagnostic(
        self, *, owner_user_id: str, incident_id: str, note: str | None
    ) -> DiagnosticScheduleResult:
        del note
        assert owner_user_id == "owner_a"
        assert incident_id == "incident_4"
        self.start_calls += 1
        return DiagnosticScheduleResult("diagnostic_4", "job_diagnostic_4", True)

    async def create_recovery_approval_request(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        reason: str,
        chat_run_id: str | None,
    ) -> RecoveryApprovalRequest:
        del owner_user_id, task_id, reason, chat_run_id
        raise AssertionError("recovery approval is not expected")


@pytest.mark.asyncio
async def test_confirmed_action_handler_converges_without_duplicate_execution(
    migrated_database_url: str,
) -> None:
    engine, repositories, service = await _service(migrated_database_url)
    try:
        action = await service.preview_start(
            owner_user_id="owner_a",
            session_id="session_a",
            incident_id="incident_4",
        )
        await service.confirm(owner_user_id="owner_a", action_id=action.id)
        assert repositories.background_jobs is not None
        job = await repositories.background_jobs.claim_next(
            worker_id="worker_1",
            lease_expires_at=utc_now() + timedelta(minutes=1),
        )
        assert job is not None
        bridge = IdempotentFakeBridge()
        handler = PendingChatActionJobHandler(
            repositories=repositories,
            bridge=bridge,
        )
        context = BackgroundJobContext(job=job, repository=repositories.background_jobs)

        await handler(context)
        await handler(context)

        assert bridge.start_calls == 1
        assert repositories.pending_chat_actions is not None
        stored = await repositories.pending_chat_actions.get_owned(
            owner_user_id="owner_a", action_id=action.id
        )
        assert stored is not None
        assert stored.status == "executed"
        assert stored.execution_result_id == "diagnostic_4"
    finally:
        await engine.dispose()
