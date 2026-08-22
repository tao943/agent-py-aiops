from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel, utc_now
from super_ai.memory.repositories import (
    ChatRunIdempotencyConflict,
    ChatToolExecutionClaim,
)
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


async def _repositories(database_url: str):  # type: ignore[no-untyped-def]
    engine = create_memory_engine(database_url)
    session_factory = create_memory_session_factory(engine)
    async with session_factory() as session:
        now = utc_now()
        session.add(
            UserModel(
                id="owner_1",
                email="chat-runs@example.com",
                display_name="Chat Runs",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    repositories = create_sqlalchemy_memory_repositories(session_factory)
    await repositories.chat.create_session(owner_user_id="owner_1", session_id="session_1")
    return engine, repositories


@pytest.mark.asyncio
async def test_concurrent_create_run_returns_one_record(
    migrated_database_url: str,
) -> None:
    engine, repositories = await _repositories(migrated_database_url)
    try:
        assert repositories.chat_runs is not None
        results = await asyncio.gather(
            *[
                repositories.chat_runs.create_or_get(
                    owner_user_id="owner_1",
                    session_id="session_1",
                    client_request_id="request_1",
                    request_fingerprint="a" * 64,
                    content="查看事故状态",
                    metadata={},
                )
                for _ in range(8)
            ]
        )
        assert len({item.run.id for item in results}) == 1
        assert len({item.background_job_id for item in results}) == 1
        assert sum(not item.reused for item in results) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_request_key_with_different_fingerprint_is_conflict(
    migrated_database_url: str,
) -> None:
    engine, repositories = await _repositories(migrated_database_url)
    try:
        assert repositories.chat_runs is not None
        await repositories.chat_runs.create_or_get(
            owner_user_id="owner_1",
            session_id="session_1",
            client_request_id="request_1",
            request_fingerprint="a" * 64,
            content="first",
            metadata={},
        )
        with pytest.raises(ChatRunIdempotencyConflict):
            await repositories.chat_runs.create_or_get(
                owner_user_id="owner_1",
                session_id="session_1",
                client_request_id="request_1",
                request_fingerprint="b" * 64,
                content="second",
                metadata={},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_events_receive_contiguous_sequences(
    migrated_database_url: str,
) -> None:
    engine, repositories = await _repositories(migrated_database_url)
    try:
        assert repositories.chat_runs is not None
        created = await repositories.chat_runs.create_or_get(
            owner_user_id="owner_1",
            session_id="session_1",
            client_request_id="request_1",
            request_fingerprint="a" * 64,
            content="status",
            metadata={},
        )
        events = await asyncio.gather(
            *[
                repositories.chat_runs.append_event(
                    owner_user_id="owner_1",
                    run_id=created.run.id,
                    event_type="content.delta",
                    public_payload={"delta": str(index)},
                )
                for index in range(8)
            ]
        )
        assert sorted(event.sequence for event in events) == list(range(1, 9))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_tool_claim_has_one_owner(
    migrated_database_url: str,
) -> None:
    engine, repositories = await _repositories(migrated_database_url)
    try:
        assert repositories.chat_runs is not None
        assert repositories.chat_tool_executions is not None
        created = await repositories.chat_runs.create_or_get(
            owner_user_id="owner_1",
            session_id="session_1",
            client_request_id="request_1",
            request_fingerprint="a" * 64,
            content="status",
            metadata={},
        )
        claim = ChatToolExecutionClaim(
            tool_call_key="c" * 64,
            owner_user_id="owner_1",
            chat_run_id=created.run.id,
            logical_step="2",
            tool_name="get_diagnostic_status",
            arguments_fingerprint="d" * 64,
            lease_owner="worker_1",
            lease_expires_at=utc_now() + timedelta(minutes=1),
            side_effecting=False,
        )
        claims = await asyncio.gather(
            *[repositories.chat_tool_executions.claim(claim) for _ in range(8)]
        )
        assert sum(item.action == "acquired" for item in claims) == 1
        assert all(item.action in {"acquired", "wait"} for item in claims)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_approval_create_or_get_is_pending_and_non_executable(
    migrated_database_url: str,
) -> None:
    engine, repositories = await _repositories(migrated_database_url)
    try:
        assert repositories.recovery_approvals is not None
        await repositories.diagnostics.create_task(
            owner_user_id="owner_1",
            task_id="diagnostic_1",
            status="completed",
            query="database lock",
        )
        first = await repositories.recovery_approvals.create_or_get(
            owner_user_id="owner_1",
            diagnostic_task_id="diagnostic_1",
            proposal_fingerprint="e" * 64,
            request_reason="需要人工确认",
            chat_run_id=None,
        )
        second = await repositories.recovery_approvals.create_or_get(
            owner_user_id="owner_1",
            diagnostic_task_id="diagnostic_1",
            proposal_fingerprint="e" * 64,
            request_reason="重复请求",
            chat_run_id=None,
        )
        assert second.id == first.id
        assert second.status == "pending"
        assert second.execution_permitted is False
    finally:
        await engine.dispose()
