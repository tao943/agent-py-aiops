from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from super_ai.api.app import create_app
from super_ai.chat.pending_actions import PendingChatActionService
from super_ai.chat.run_events import PublicRunEventError, public_run_event, tool_call_key
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


def test_tool_call_key_is_stable_for_canonical_arguments() -> None:
    first = tool_call_key("run_1", "2", "get_report", {"b": 2, "a": 1})
    second = tool_call_key("run_1", "2", "get_report", {"a": 1, "b": 2})
    assert first == second
    assert len(first) == 64


def test_public_run_event_rejects_private_nested_keys() -> None:
    with pytest.raises(PublicRunEventError):
        public_run_event(
            sequence=2,
            event_type="content.delta",
            payload={"safe": {"reasoning": "private"}},
            timestamp=utc_now(),
        )


@pytest.mark.asyncio
async def test_run_api_is_owner_scoped_and_replays_after_last_event_id(
    migrated_database_url: str,
) -> None:
    app = create_app(database_url=migrated_database_url, chat_agent_runner=FakeRunner())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        owner = await _register(client, "run-owner@example.com", "Run Owner")
        other = await _register(client, "run-other@example.com", "Run Other")
        owner_headers = _auth_headers(owner["accessToken"])
        other_headers = _auth_headers(other["accessToken"])
        session_response = await client.post("/chat/sessions", headers=owner_headers, json={})
        session_id = session_response.json()["data"]["id"]
        create_response = await client.post(
            f"/chat/sessions/{session_id}/runs",
            headers=owner_headers,
            json={
                "content": "查看事故",
                "clientRequestId": "request_api_1",
            },
        )
        assert create_response.status_code == 202, create_response.text
        run_id = create_response.json()["data"]["id"]
        active_response = await client.get(
            f"/chat/sessions/{session_id}/runs/active", headers=owner_headers
        )
        forbidden_response = await client.get(
            f"/chat/sessions/{session_id}/runs/{run_id}", headers=other_headers
        )

        repositories = app.state.memory_repositories
        assert repositories.chat_runs is not None
        for index in range(3):
            await repositories.chat_runs.append_event(
                owner_user_id=owner["user"]["id"],
                run_id=run_id,
                event_type="content.delta",
                public_payload={"delta": str(index), "sequence": index + 1},
            )
        await repositories.chat_runs.fail_with_event(
            owner_user_id=owner["user"]["id"],
            run_id=run_id,
            error_code="CHAT_AGENT_MODEL_FAILED",
            public_payload={
                "code": "CHAT_AGENT_MODEL_FAILED",
                "retryable": False,
                "runId": run_id,
            },
        )
        replay_response = await client.get(
            f"/chat/sessions/{session_id}/runs/{run_id}/events",
            headers={**owner_headers, "Last-Event-ID": "2"},
        )

    assert create_response.status_code == 202
    assert create_response.json()["data"]["status"] == "queued"
    assert "agentConfigurationSnapshot" in create_response.json()["data"]
    assert active_response.json()["data"]["id"] == run_id
    assert forbidden_response.status_code == 404
    assert [line for line in replay_response.text.splitlines() if line.startswith("id: ")] == [
        "id: 3",
        "id: 4",
    ]


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


async def _register(client: httpx.AsyncClient, email: str, display_name: str) -> dict[str, Any]:
    response = await client.post(
        "/auth/register",
        json={
            "email": email,
            "displayName": display_name,
            "password": "correct horse battery staple",
        },
    )
    return response.json()["data"]


def _auth_headers(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_pending_action_api_is_owner_scoped_idempotent_and_listed(
    migrated_database_url: str,
) -> None:
    app = create_app(database_url=migrated_database_url, chat_agent_runner=FakeRunner())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        owner = await _register(client, "action-owner@example.com", "Action Owner")
        other = await _register(client, "action-other@example.com", "Action Other")
        owner_headers = _auth_headers(owner["accessToken"])
        other_headers = _auth_headers(other["accessToken"])
        session_response = await client.post("/chat/sessions", headers=owner_headers, json={})
        session_id = session_response.json()["data"]["id"]
        repositories = app.state.memory_repositories
        assert repositories.pending_chat_actions is not None
        action = await PendingChatActionService(
            repositories.pending_chat_actions
        ).preview_start(
            owner_user_id=owner["user"]["id"],
            session_id=session_id,
            incident_id="incident_1",
        )

        pending = await client.get(
            f"/chat/sessions/{session_id}/actions/pending",
            headers=owner_headers,
        )
        first = await client.post(
            f"/chat/actions/{action.id}/confirm", headers=owner_headers
        )
        duplicate = await client.post(
            f"/chat/actions/{action.id}/confirm", headers=owner_headers
        )
        foreign = await client.post(
            f"/chat/actions/{action.id}/confirm", headers=other_headers
        )

    assert pending.status_code == 200
    assert pending.json()["data"]["items"][0]["id"] == action.id
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["data"] == duplicate.json()["data"]
    assert first.json()["data"]["status"] == "confirmed"
    assert foreign.status_code == 404
