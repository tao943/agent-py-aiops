from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

import pytest

from super_ai.chat.memory import ChatMemoryService
from super_ai.chat.memory_jobs import (
    StructuredMemoryCompactionHandler,
    schedule_compaction,
)
from super_ai.jobs import BackgroundJobContext
from super_ai.llm import LlmProvider
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


@dataclass
class FakeResponse:
    content: str


class JsonMemoryModel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.inputs: list[object] = []

    async def ainvoke(self, value: object) -> FakeResponse:
        self.inputs.append(value)
        if self.fail:
            raise TimeoutError("provider timeout must not be persisted")
        return FakeResponse(
            json.dumps(
                {
                    "user_goals": [
                        {
                            "value": "定位订单服务故障",
                            "source_message_ids": ["z-user"],
                            "citation_ids": [],
                            "trust": "user_asserted",
                        }
                    ],
                    "confirmed_facts": [],
                    "preferences": [],
                    "decisions": [],
                    "open_tasks": [],
                    "resource_refs": [],
                },
                ensure_ascii=False,
            )
        )


class FakeProvider:
    def __init__(self, model: JsonMemoryModel) -> None:
        self.model = model

    def create_chat_model(self) -> JsonMemoryModel:
        return self.model


@pytest.mark.asyncio
async def test_concurrent_compaction_scheduling_reuses_one_stable_job(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        session = await repositories.chat.create_session(
            owner_user_id="owner", session_id="chat-compact"
        )
        boundary = await repositories.chat.append_message(
            owner_user_id="owner",
            session_id=session.id,
            message_id="message-boundary",
            role="assistant",
            content="boundary",
        )

        jobs = await asyncio.gather(
            *(
                schedule_compaction(
                    repositories=repositories,
                    owner_user_id="owner",
                    session=session,
                    through_message_id=boundary.id,
                )
                for _ in range(8)
            )
        )
        persisted = await repositories.background_jobs.list(owner_user_id="owner")  # type: ignore[union-attr]
    finally:
        await engine.dispose()

    assert len({job.id for job in jobs}) == 1
    assert len(persisted) == 1
    assert persisted[0].kind == "chat_memory_compaction"


@pytest.mark.asyncio
async def test_compaction_uses_timestamp_and_id_boundary_and_advances_only_on_success(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    model = JsonMemoryModel()
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        session = await repositories.chat.create_session(
            owner_user_id="owner", session_id="chat-boundary"
        )
        same_time = datetime(2026, 8, 22, tzinfo=timezone.utc)
        message_roles = (
            ("z-user", "user"),
            ("a-assistant", "assistant"),
            ("zz-later", "user"),
        )
        for message_id, role in message_roles:
            await repositories.chat.append_message(
                owner_user_id="owner",
                session_id=session.id,
                message_id=message_id,
                role=role,
                content=message_id,
                created_at=same_time,
            )
        job = await schedule_compaction(
            repositories=repositories,
            owner_user_id="owner",
            session=session,
            through_message_id="z-user",
        )
        context = BackgroundJobContext(job=job, repository=repositories.background_jobs)  # type: ignore[arg-type]
        await StructuredMemoryCompactionHandler(
            repositories=repositories,
            llm_provider=cast(LlmProvider, FakeProvider(model)),
        )(context)
        await StructuredMemoryCompactionHandler(
            repositories=repositories,
            llm_provider=cast(LlmProvider, FakeProvider(model)),
        )(context)
        persisted = await repositories.chat.get_session(
            owner_user_id="owner", session_id=session.id
        )
    finally:
        await engine.dispose()

    assert persisted is not None
    assert persisted.memory_summary_version == 1
    assert persisted.memory_through_message_id == "z-user"
    prompt = str(model.inputs[0])
    assert "a-assistant" in prompt
    assert "z-user" in prompt
    assert "zz-later" not in prompt
    assert len(model.inputs) == 1


@pytest.mark.asyncio
async def test_compaction_model_failure_does_not_advance_memory_version(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        session = await repositories.chat.create_session(
            owner_user_id="owner", session_id="chat-failure"
        )
        message = await repositories.chat.append_message(
            owner_user_id="owner",
            session_id=session.id,
            message_id="message-1",
            role="user",
            content="keep",
        )
        job = await schedule_compaction(
            repositories=repositories,
            owner_user_id="owner",
            session=session,
            through_message_id=message.id,
        )
        context = BackgroundJobContext(job=job, repository=repositories.background_jobs)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="CHAT_MEMORY_MODEL_FAILED"):
            await StructuredMemoryCompactionHandler(
                repositories=repositories,
                llm_provider=cast(LlmProvider, FakeProvider(JsonMemoryModel(fail=True))),
            )(context)
        persisted = await repositories.chat.get_session(
            owner_user_id="owner", session_id=session.id
        )
    finally:
        await engine.dispose()

    assert persisted is not None
    assert persisted.memory_summary_version == 0
    assert persisted.memory_through_message_id is None


@pytest.mark.asyncio
async def test_sixty_percent_schedules_only_adaptive_sessions(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        adaptive = await repositories.chat.create_session(
            owner_user_id="owner", session_id="chat-adaptive-60"
        )
        manual = await repositories.chat.create_session(
            owner_user_id="owner", session_id="chat-manual-60"
        )
        manual = (
            await repositories.chat.update_memory_state(
                owner_user_id="owner", session_id=manual.id, memory_mode="manual"
            )
        ) or manual
        for target in (adaptive, manual):
            for index in range(14):
                await repositories.chat.append_message(
                    owner_user_id="owner",
                    session_id=target.id,
                    message_id=f"{target.id}-{index:02d}",
                    role="user" if index % 2 == 0 else "assistant",
                    content="context " * 40,
                )
        service = ChatMemoryService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, FakeProvider(JsonMemoryModel())),
            context_window_tokens=500,
        )
        for target in (adaptive, manual):
            history = await repositories.chat.list_messages(
                owner_user_id="owner", session_id=target.id
            )
            await service.refresh_usage(
                owner_user_id="owner",
                session=target,
                history=history,
                system_prompt="system",
            )
        jobs = await repositories.background_jobs.list(owner_user_id="owner")  # type: ignore[union-attr]
    finally:
        await engine.dispose()

    assert [job.payload["sessionId"] for job in jobs] == [adaptive.id]


@pytest.mark.asyncio
async def test_eighty_five_percent_compacts_synchronously_before_hard_limit(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    model = JsonMemoryModel()
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        session = await repositories.chat.create_session(
            owner_user_id="owner", session_id="chat-sync-85"
        )
        for index in range(14):
            await repositories.chat.append_message(
                owner_user_id="owner",
                session_id=session.id,
                message_id=f"message-{index:02d}",
                role="user" if index % 2 == 0 else "assistant",
                content=("old context " * 300 if index < 2 else "recent"),
            )
        history = await repositories.chat.list_messages(
            owner_user_id="owner", session_id=session.id
        )
        service = ChatMemoryService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, FakeProvider(model)),
            context_window_tokens=2_000,
        )
        prepared = await service.prepare_message(
            owner_user_id="owner",
            session=session,
            history=history,
            system_prompt="system",
            content="continue",
        )
    finally:
        await engine.dispose()

    assert prepared.session.memory_summary_version == 1
    assert prepared.session.memory_through_message_id == "message-01"
    assert len(model.inputs) == 1
