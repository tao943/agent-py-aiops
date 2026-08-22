from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from super_ai.aiops.diagnostics import (
    _is_transient_infrastructure_error,  # pyright: ignore[reportPrivateUsage]
    _stable_public_id,  # pyright: ignore[reportPrivateUsage]
)
from super_ai.aiops.evidence_aggregation import (
    SpecialistAggregationContext,
    aggregate_specialist_results,
)
from super_ai.aiops.investigation import EvidenceClaim
from super_ai.aiops.specialists import SpecialistAssignment, SpecialistResult
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


@pytest.mark.asyncio
async def test_specialist_checkpoints_replay_without_duplicate_evidence(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repositories = create_sqlalchemy_memory_repositories(
        create_memory_session_factory(engine)
    )
    owner_user_id = "specialist-replay-user"
    task_id = "diagnostic-specialist-replay"
    graph_version = "evidence-driven-v4"
    now = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
    assignment = SpecialistAssignment(
        role="runtime",
        objective="Test public evidence.",
        hypotheses_to_test=("pool_lifecycle_failure",),
        required_causal_roles=("mechanism",),
        allowed_tools=frozenset({"InspectOrderPoolState"}),
        trusted_arguments_by_tool={"InspectOrderPoolState": {}},
        maximum_tool_steps=1,
        model_call_budget=2,
        soft_deadline_at=now + timedelta(minutes=2),
        hard_deadline_at=now + timedelta(minutes=3),
    )
    try:
        await repositories.diagnostics.create_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="running",
            query="Investigate public pool evidence",
            input_payload={},
        )
        audit = await repositories.diagnostics.add_tool_call_audit(
            owner_user_id=owner_user_id,
            audit_id="call-specialist-runtime",
            task_id=task_id,
            tool_name="InspectOrderPoolState",
            status="completed",
            arguments={},
            result_payload={},
            started_at=now,
            completed_at=now,
        )
        evidence = await repositories.diagnostics.create_evidence(
            owner_user_id=owner_user_id,
            evidence_id="evidence-specialist-runtime",
            task_id=task_id,
            step_id=None,
            tool_call_id=audit.id,
            kind="tool_observation",
            source="InspectOrderPoolState",
            summary="A safe public pool observation.",
            payload={"sourceFingerprint": "runtime-pool-source"},
        )
        claim = EvidenceClaim(
            claim_id="order_pool_saturated",
            value=True,
            quality="direct",
            causal_role="mechanism",
            supports=("pool_lifecycle_failure",),
            refutes=(),
            evidence_ids=(evidence.id,),
            target_component="order-api",
            observed_at=now,
            time_scope="incident_window",
        )
        specialist = SpecialistResult.create(
            role="runtime",
            terminal_status="completed",
            evidence_status="complete",
            analysis_status="complete",
            analysis_error_code=None,
            analysis_attempt_count=1,
            soft_deadline_exceeded=False,
            hard_deadline_exceeded=False,
            expected_tool_count=1,
            tested_hypotheses=("pool_lifecycle_failure",),
            evidence_ids=(evidence.id,),
            fact_candidates=(claim,),
            proposed_assessments=(),
            unresolved_questions=(),
            completed_steps=("runtime-1",),
            model_call_count=2,
            duration_ms=10,
        )
        specialist_payload: dict[str, object] = {
            "role": specialist.role,
            "resultChecksum": specialist.result_checksum,
            "evidenceIds": list(specialist.evidence_ids),
            "modelCallCount": specialist.model_call_count,
        }
        first_role_checkpoint = await repositories.diagnostics.save_checkpoint(
            owner_user_id=owner_user_id,
            checkpoint_record_id="checkpoint-specialist-runtime",
            task_id=task_id,
            thread_id=f"aiops:{task_id}",
            checkpoint_ns="specialist/runtime",
            checkpoint_id="runtime-completed",
            checkpoint_payload=specialist_payload,
            metadata={"node": "runtime_specialist"},
        )
        replayed_role_checkpoint = await repositories.diagnostics.save_checkpoint(
            owner_user_id=owner_user_id,
            checkpoint_record_id="checkpoint-specialist-runtime",
            task_id=task_id,
            thread_id=f"aiops:{task_id}",
            checkpoint_ns="specialist/runtime",
            checkpoint_id="runtime-completed",
            checkpoint_payload=specialist_payload,
            metadata={"node": "runtime_specialist"},
        )

        context = SpecialistAggregationContext(
            owner_user_id=owner_user_id,
            task_id=task_id,
            graph_version=graph_version,
            assignments={"runtime": assignment},
            evidence_by_id={evidence.id: evidence},
            completed_tool_audit_by_id={audit.id: audit},
        )
        aggregation = aggregate_specialist_results((specialist,), context=context)
        aggregation_payload = aggregation.to_checkpoint_payload()
        first_aggregation_checkpoint = await repositories.diagnostics.save_checkpoint(
            owner_user_id=owner_user_id,
            checkpoint_record_id="checkpoint-specialist-aggregation",
            task_id=task_id,
            thread_id=f"aiops:{task_id}",
            checkpoint_ns="specialist/aggregation",
            checkpoint_id="aggregation-completed",
            checkpoint_payload=aggregation_payload,
            metadata={"node": "evidence_aggregator"},
        )
        replayed_aggregation_checkpoint = await repositories.diagnostics.save_checkpoint(
            owner_user_id=owner_user_id,
            checkpoint_record_id="checkpoint-specialist-aggregation",
            task_id=task_id,
            thread_id=f"aiops:{task_id}",
            checkpoint_ns="specialist/aggregation",
            checkpoint_id="aggregation-completed",
            checkpoint_payload=aggregation_payload,
            metadata={"node": "evidence_aggregator"},
        )

        persisted_evidence = await repositories.diagnostics.list_evidence(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        persisted_audits = await repositories.diagnostics.list_tool_call_audits(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        replayed = aggregate_specialist_results(
            (specialist,),
            context=SpecialistAggregationContext(
                owner_user_id=owner_user_id,
                task_id=task_id,
                graph_version=graph_version,
                assignments={"runtime": assignment},
                evidence_by_id={item.id: item for item in persisted_evidence},
                completed_tool_audit_by_id={item.id: item for item in persisted_audits},
            ),
        )
        checkpoints = await repositories.diagnostics.list_checkpoints(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
    finally:
        await engine.dispose()

    assert first_role_checkpoint.id == replayed_role_checkpoint.id
    assert first_aggregation_checkpoint.id == replayed_aggregation_checkpoint.id
    assert replayed.aggregation_checksum == aggregation.aggregation_checksum
    assert replayed.evidence == (evidence.id,)
    assert len(persisted_evidence) == 1
    assert len(checkpoints) == 2


@pytest.mark.asyncio
async def test_conflicting_completed_checkpoint_fails_closed(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repositories = create_sqlalchemy_memory_repositories(
        create_memory_session_factory(engine)
    )
    try:
        await repositories.diagnostics.create_task(
            owner_user_id="checkpoint-conflict-user",
            task_id="diagnostic-checkpoint-conflict",
            status="running",
            query="Investigate checkpoint conflict",
            input_payload={},
        )
        arguments: dict[str, Any] = {
            "owner_user_id": "checkpoint-conflict-user",
            "checkpoint_record_id": "checkpoint-conflicting-result",
            "task_id": "diagnostic-checkpoint-conflict",
            "thread_id": "aiops:diagnostic-checkpoint-conflict",
            "checkpoint_ns": "specialist/runtime",
            "checkpoint_id": "runtime-completed",
            "metadata": {"node": "runtime_specialist"},
        }
        await repositories.diagnostics.save_checkpoint(
            **arguments,
            checkpoint_payload={"resultChecksum": "a" * 64},
        )

        with pytest.raises(ValueError, match="checkpoint_content_conflict"):
            await repositories.diagnostics.save_checkpoint(
                **arguments,
                checkpoint_payload={"resultChecksum": "b" * 64},
            )
    finally:
        await engine.dispose()
