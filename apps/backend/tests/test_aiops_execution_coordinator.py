from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from super_ai.aiops.execution import ExecutionCoordinator, ExecutionIdentity
from super_ai.memory.aiops_execution_sqlalchemy import (
    SQLAlchemyAiopsExecutionRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


def test_execution_identity_uses_canonical_json_order() -> None:
    first = ExecutionIdentity(
        task_id="task",
        graph_version="v2",
        node_name="planner",
        logical_iteration=0,
        input_payload={"b": 2, "a": 1},
    )
    second = ExecutionIdentity(
        task_id="task",
        graph_version="v2",
        node_name="planner",
        logical_iteration=0,
        input_payload={"a": 1, "b": 2},
    )

    assert first.input_fingerprint == second.input_fingerprint
    assert first.execution_key == second.execution_key


@pytest.mark.asyncio
async def test_same_execution_key_runs_operation_once(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"coordinator-task-{uuid4().hex}"
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Coordinate execution.",
            input_payload={},
        )
        repository = SQLAlchemyAiopsExecutionRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        first = ExecutionCoordinator(repository, worker_id="worker-a")
        second = ExecutionCoordinator(repository, worker_id="worker-b")
        operation = AsyncMock(return_value={"result": "once"})
        identity = ExecutionIdentity(
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
            node_name="planner",
            logical_iteration=0,
            input_payload={"query": "same"},
        )
        results = await asyncio.gather(
            first.run_once(identity, operation),
            second.run_once(identity, operation),
        )
    finally:
        await engine.dispose()

    assert operation.await_count == 1
    assert {item.cache_hit for item in results} == {False, True}
    assert {item.output["result"] for item in results} == {"once"}
