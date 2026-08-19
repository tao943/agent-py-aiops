from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from super_ai.aiops.checkpointing import PostgresDiagnosticCheckpointSaver
from super_ai.memory.aiops_execution_sqlalchemy import (
    SQLAlchemyLangGraphCheckpointRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


@pytest.mark.asyncio
async def test_postgres_saver_round_trips_parent_and_idempotent_writes(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"saver-task-{uuid4().hex}"
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Checkpoint graph.",
            input_payload={},
        )
        repository = SQLAlchemyLangGraphCheckpointRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        saver = PostgresDiagnosticCheckpointSaver(
            repository,
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        base_config = cast(
            Any,
            {
                "configurable": {
                    "thread_id": f"aiops:{task_id}:aiops-diagnostic-v2",
                    "checkpoint_ns": "",
                }
            },
        )
        first = empty_checkpoint()
        first["channel_values"] = {"model_call_count": 3}
        first_config = await saver.aput(
            base_config,
            first,
            cast(Any, {"source": "input", "step": 0, "parents": {}}),
            {},
        )
        repeated_first_config = await saver.aput(
            base_config,
            first,
            cast(Any, {"source": "input", "step": 0, "parents": {}}),
            {},
        )
        updated_first = first.copy()
        updated_first["channel_values"] = {"model_call_count": 4}
        updated_first_config = await saver.aput(
            base_config,
            updated_first,
            cast(Any, {"source": "loop", "step": 1, "parents": {}}),
            {},
        )
        updated_first_stored = await saver.aget_tuple(updated_first_config)
        second = empty_checkpoint()
        second["channel_values"] = {
            "model_call_count": 5,
            "soft_deadline_at": "persisted",
        }
        second_config = await saver.aput(
            first_config,
            second,
            cast(Any, {"source": "loop", "step": 1, "parents": {}}),
            {},
        )
        await saver.aput_writes(
            second_config,
            [("audit", {"safeErrorCode": None})],
            "langgraph-task",
            "pull:validator",
        )
        await saver.aput_writes(
            second_config,
            [("audit", {"safeErrorCode": None})],
            "langgraph-task",
            "pull:validator",
        )
        stored = await saver.aget_tuple(second_config)
        listed = [item async for item in saver.alist(base_config)]
    finally:
        await engine.dispose()

    assert stored is not None
    assert repeated_first_config == first_config
    assert updated_first_config == first_config
    assert updated_first_stored is not None
    assert updated_first_stored.checkpoint["channel_values"]["model_call_count"] == 4
    assert updated_first_stored.metadata.get("step") == 1
    assert stored.checkpoint["channel_values"]["model_call_count"] == 5
    assert stored.parent_config == first_config
    assert len(stored.pending_writes or []) == 1
    assert len(listed) == 2


def test_saver_rejects_another_task_thread() -> None:
    saver = PostgresDiagnosticCheckpointSaver(
        cast(Any, object()),
        task_id="task-a",
        graph_version="aiops-diagnostic-v2",
    )

    with pytest.raises(ValueError, match="outside"):
        saver._identity_parts(  # pyright: ignore[reportPrivateUsage]
            cast(Any, {"configurable": {"thread_id": "aiops:task-b:v2"}})
        )
