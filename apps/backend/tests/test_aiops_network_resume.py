from __future__ import annotations

import asyncio
from typing import Any

import pytest

from super_ai.aiops.diagnostics import (
    _is_transient_infrastructure_error,  # pyright: ignore[reportPrivateUsage]
    _stable_public_id,  # pyright: ignore[reportPrivateUsage]
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


def test_retryable_infrastructure_errors_are_allowlisted_without_messages() -> None:
    class ConnectionResetByPeer(RuntimeError):
        pass

    assert _is_transient_infrastructure_error(TimeoutError("private endpoint"))
    assert _is_transient_infrastructure_error(ConnectionError("private endpoint"))
    assert _is_transient_infrastructure_error(ConnectionResetByPeer("private endpoint"))
    assert not _is_transient_infrastructure_error(OSError("missing local config"))
    assert not _is_transient_infrastructure_error(ValueError("contract failure"))


def test_public_execution_ids_are_stable_and_namespaced() -> None:
    first = _stable_public_id("tool", "task-1", "step-1", "InspectPostgres", "abc")
    repeated = _stable_public_id(
        "tool", "task-1", "step-1", "InspectPostgres", "abc"
    )
    different = _stable_public_id(
        "evidence", "task-1", "step-1", "InspectPostgres", "abc"
    )

    assert first == repeated
    assert first.startswith("tool_")
    assert different.startswith("evidence_")
    assert first != different


@pytest.mark.asyncio
async def test_retry_reuses_committed_diagnostic_side_effects(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repositories = create_sqlalchemy_memory_repositories(
        create_memory_session_factory(engine)
    )
    try:
        task = await repositories.diagnostics.create_task(
            owner_user_id="retry-user",
            task_id="diagnostic-network-retry",
            status="accepted",
            query="Investigate a retryable incident",
            input_payload={},
        )
        step_arguments: Any = {
            "owner_user_id": task.owner_user_id,
            "step_id": "diagnostic_step_stable",
            "task_id": task.id,
            "sequence": 1,
            "phase": "executor",
            "status": "completed",
            "payload": {"safe": True},
        }
        first_step = await repositories.diagnostics.create_step(**step_arguments)
        repeated_step = await repositories.diagnostics.create_step(**step_arguments)

        audit_repository = repositories.tool_call_audits
        assert audit_repository is not None
        audit_arguments: Any = {
            "owner_user_id": task.owner_user_id,
            "audit_id": "tool_stable",
            "diagnostic_task_id": task.id,
            "tool_name": "InspectPostgres",
            "arguments": {"database": "orders"},
        }
        first_audit = await audit_repository.create_for_diagnostic_task(
            **audit_arguments
        )
        repeated_audit = await audit_repository.create_for_diagnostic_task(
            **audit_arguments
        )

        evidence_arguments: Any = {
            "owner_user_id": task.owner_user_id,
            "evidence_id": "evidence_stable",
            "task_id": task.id,
            "step_id": first_step.id,
            "tool_call_id": first_audit.id,
            "kind": "tool_observation",
            "source": "InspectPostgres",
            "summary": "A safe structured observation.",
            "payload": {"sqlstate": "40P01"},
        }
        first_evidence = await repositories.diagnostics.create_evidence(
            **evidence_arguments
        )
        repeated_evidence = await repositories.diagnostics.create_evidence(
            **evidence_arguments
        )

        checkpoint_arguments: Any = {
            "owner_user_id": task.owner_user_id,
            "checkpoint_record_id": "checkpoint_stable",
            "task_id": task.id,
            "thread_id": f"aiops:{task.id}",
            "checkpoint_ns": "executor",
            "checkpoint_id": "executor-1",
            "checkpoint_payload": {"completed": True},
            "metadata": {"node": "executor"},
        }
        first_checkpoint = await repositories.diagnostics.save_checkpoint(
            **checkpoint_arguments
        )
        repeated_checkpoint = await repositories.diagnostics.save_checkpoint(
            **checkpoint_arguments
        )

        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id, task_id=task.id
        )
        evidence = await repositories.diagnostics.list_evidence(
            owner_user_id=task.owner_user_id, task_id=task.id
        )
        audits = await audit_repository.list_for_diagnostic_task(
            owner_user_id=task.owner_user_id, diagnostic_task_id=task.id
        )
    finally:
        await engine.dispose()

    assert first_step.id == repeated_step.id
    assert first_audit.id == repeated_audit.id
    assert first_evidence.id == repeated_evidence.id
    assert first_checkpoint.id == repeated_checkpoint.id
    assert len(steps) == 1
    assert len(evidence) == 1
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_concurrent_workers_allocate_unique_increasing_step_sequences(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    first_repository = create_sqlalchemy_memory_repositories(session_factory).diagnostics
    second_repository = create_sqlalchemy_memory_repositories(session_factory).diagnostics
    try:
        await first_repository.create_task(
            owner_user_id="sequence-user",
            task_id="diagnostic-sequence-race",
            status="running",
            query="Investigate concurrent branches",
            input_payload={"benchmarkMode": "live"},
        )
        await asyncio.gather(
            first_repository.create_step(
                owner_user_id="sequence-user",
                step_id="step-runtime",
                task_id="diagnostic-sequence-race",
                sequence=1,
                phase="investigator_dispatch",
                status="completed",
                payload={"dispatchId": "runtime"},
            ),
            second_repository.create_step(
                owner_user_id="sequence-user",
                step_id="step-log",
                task_id="diagnostic-sequence-race",
                sequence=1,
                phase="investigator_dispatch",
                status="completed",
                payload={"dispatchId": "log"},
            ),
        )
        steps = await first_repository.list_steps(
            owner_user_id="sequence-user",
            task_id="diagnostic-sequence-race",
        )
    finally:
        await engine.dispose()

    assert [step.sequence for step in steps] == [1, 2]
