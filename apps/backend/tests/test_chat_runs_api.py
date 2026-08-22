from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from super_ai.chat.runs import ChatRunJobHandler
from super_ai.chat.streaming import (
    ChatAgentContentDelta,
    ChatAgentEvent,
    ChatAgentRequest,
    ChatStreamingService,
)
from super_ai.jobs import BackgroundJobContext, BackgroundJobRuntime
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import UserModel, utc_now
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


class FakeRunner:
    def stream(self, request: ChatAgentRequest) -> AsyncIterator[ChatAgentEvent]:
        del request

        async def events() -> AsyncIterator[ChatAgentEvent]:
            yield ChatAgentContentDelta("诊断已进入人工复核。")

        return events()


class FailOnceRunner:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, request: ChatAgentRequest) -> AsyncIterator[ChatAgentEvent]:
        del request
        self.calls += 1
        call = self.calls

        async def events() -> AsyncIterator[ChatAgentEvent]:
            if call == 1:
                raise TimeoutError("private provider detail")
            yield ChatAgentContentDelta("恢复后的公开回答")

        return events()


@pytest.mark.asyncio
async def test_chat_run_handler_converges_on_one_assistant_message(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    try:
        async with session_factory() as session:
            now = utc_now()
            session.add(
                UserModel(
                    id="owner_1",
                    email="run-handler@example.com",
                    display_name="Run Handler",
                    password_hash="unused",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.chat.create_session(owner_user_id="owner_1", session_id="session_1")
        assert repositories.chat_runs is not None
        assert repositories.background_jobs is not None
        created = await repositories.chat_runs.create_or_get(
            owner_user_id="owner_1",
            session_id="session_1",
            client_request_id="request_1",
            request_fingerprint="a" * 64,
            content="查看故障",
            metadata={},
        )
        job = await repositories.background_jobs.claim_next(
            worker_id="worker_1",
            lease_expires_at=utc_now() + timedelta(minutes=1),
        )
        assert job is not None
        handler = ChatRunJobHandler(
            repositories=repositories,
            streaming=ChatStreamingService(
                repositories=repositories,
                agent_runner=FakeRunner(),
            ),
        )

        await handler(BackgroundJobContext(job=job, repository=repositories.background_jobs))
        await handler(BackgroundJobContext(job=job, repository=repositories.background_jobs))

        run = await repositories.chat_runs.get_owned(
            owner_user_id="owner_1",
            session_id="session_1",
            run_id=created.run.id,
        )
        assert run is not None
        assert run.status == "succeeded"
        messages = await repositories.chat.list_messages(
            owner_user_id="owner_1", session_id="session_1"
        )
        assert [message.role for message in messages] == ["user", "assistant"]
        events = await repositories.chat_runs.list_events(owner_user_id="owner_1", run_id=run.id)
        assert [event.event_type for event in events] == [
            "run.status",
            "content.delta",
            "complete",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transient_attempt_failure_restarts_without_terminal_run_error(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    runtime: BackgroundJobRuntime | None = None
    try:
        async with session_factory() as session:
            now = utc_now()
            session.add(
                UserModel(
                    id="owner_1",
                    email="run-retry@example.com",
                    display_name="Run Retry",
                    password_hash="unused",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.chat.create_session(owner_user_id="owner_1", session_id="session_1")
        assert repositories.chat_runs is not None
        assert repositories.background_jobs is not None
        created = await repositories.chat_runs.create_or_get(
            owner_user_id="owner_1",
            session_id="session_1",
            client_request_id="request_retry",
            request_fingerprint="b" * 64,
            content="重试诊断",
            metadata={},
        )
        runner = FailOnceRunner()
        runtime = BackgroundJobRuntime(
            repositories.background_jobs,
            concurrency=1,
            poll_seconds=0.05,
        )
        runtime.register(
            "chat_agent_run",
            ChatRunJobHandler(
                repositories=repositories,
                streaming=ChatStreamingService(
                    repositories=repositories,
                    agent_runner=runner,
                ),
            ),
        )
        await runtime.start()
        for _ in range(60):
            current = await repositories.chat_runs.get_owned(
                owner_user_id="owner_1",
                session_id="session_1",
                run_id=created.run.id,
            )
            if current is not None and current.status == "succeeded":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("chat run did not recover before timeout")

        assert runner.calls == 2
        events = await repositories.chat_runs.list_events(
            owner_user_id="owner_1", run_id=created.run.id
        )
        assert [event.event_type for event in events] == [
            "run.status",
            "run.attempt_failed",
            "run.restarted",
            "content.delta",
            "complete",
        ]
        assert all(event.event_type != "error" for event in events)
    finally:
        if runtime is not None:
            await runtime.stop()
        await engine.dispose()
