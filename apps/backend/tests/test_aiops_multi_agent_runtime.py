from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

import pytest

from super_ai.aiops.investigation import InvestigatorCapability
from super_ai.aiops.investigation_runtime import (
    DiagnosticToolExecutionRequest,
    DiagnosticToolExecutionResult,
    InMemoryInvestigationPacketStore,
    InvestigatorExecutor,
    PreparedDiagnosticToolExecution,
    build_investigation_dispatches,
    execute_diagnostic_tool,
)


class RecordingToolRuntime:
    def __init__(self) -> None:
        self.prepared: list[PreparedDiagnosticToolExecution] = []

    async def execute_prepared(
        self,
        request: DiagnosticToolExecutionRequest,
        prepared: PreparedDiagnosticToolExecution,
    ) -> DiagnosticToolExecutionResult:
        del request
        self.prepared.append(prepared)
        return DiagnosticToolExecutionResult(
            status="completed",
            evidence_id=prepared.evidence_id,
            tool_call_id=prepared.tool_call_id,
            safe_output={"healthy": True},
            safe_summary="The target responded successfully.",
            events=(),
        )


class FailOnceToolRuntime(RecordingToolRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def execute_prepared(
        self,
        request: DiagnosticToolExecutionRequest,
        prepared: PreparedDiagnosticToolExecution,
    ) -> DiagnosticToolExecutionResult:
        self.attempts += 1
        self.prepared.append(prepared)
        if self.attempts == 1:
            return DiagnosticToolExecutionResult(
                status="failed",
                evidence_id=prepared.evidence_id,
                tool_call_id=prepared.tool_call_id,
                safe_output={"error": "tool_unavailable"},
                safe_summary="tool_unavailable",
                events=(),
            )
        return await super().execute_prepared(request, prepared)


class TimeoutOnceToolRuntime(RecordingToolRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def execute_prepared(
        self,
        request: DiagnosticToolExecutionRequest,
        prepared: PreparedDiagnosticToolExecution,
    ) -> DiagnosticToolExecutionResult:
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("private timeout detail")
        return await super().execute_prepared(request, prepared)


def _capabilities() -> Mapping[str, InvestigatorCapability]:
    return {
        "knowledge": InvestigatorCapability(
            "knowledge", True, frozenset({"knowledge_retrieval"})
        ),
        "runtime": InvestigatorCapability(
            "runtime", True, frozenset({"InspectPostgresSessions"})
        ),
        "log": InvestigatorCapability("log", True, frozenset({"SearchLog"})),
        "change": InvestigatorCapability(
            "change",
            False,
            frozenset(),
            "deployment_change_source_not_configured",
        ),
    }


def _plan() -> list[dict[str, object]]:
    return [
        {
            "id": "runtime-1",
            "tool": "InspectPostgresSessions",
            "arguments": {"database": "orders", "limit": 10},
            "purpose": "Inspect active database sessions.",
            "sourceDomain": "runtime",
            "sourceDomainStatus": "trusted_registry",
            "testsHypotheses": ["database_pressure"],
            "causalIntent": "mechanism",
            "targetComponent": "postgres",
        },
        {
            "id": "log-1",
            "tool": "SearchLog",
            "arguments": {"Query": "deadlock", "Limit": 20},
            "purpose": "Search incident-window logs.",
            "sourceDomain": "log",
            "sourceDomainStatus": "trusted_registry",
            "testsHypotheses": ["database_deadlock"],
            "causalIntent": "trigger",
            "targetComponent": "order-service",
        },
    ]


def test_dispatches_are_stable_source_scoped_and_bounded() -> None:
    first = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=_plan(),
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("runtime", "log"),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="a" * 64,
        existing_evidence_ids=("evidence-alert",),
        deadline_ms=30_000,
        model_call_budget=0,
    )
    second = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=list(reversed(_plan())),
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("log", "runtime"),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="a" * 64,
        existing_evidence_ids=("evidence-alert",),
        deadline_ms=30_000,
        model_call_budget=0,
    )

    assert first == second
    assert tuple(item.investigator_type for item in first) == ("runtime", "log")
    assert first[0].allowed_tools == frozenset({"InspectPostgresSessions"})
    assert first[1].allowed_tools == frozenset({"SearchLog"})
    assert all(item.model_call_budget == 0 for item in first)
    assert all(len(item.dispatch_key) == 64 for item in first)


def test_dispatch_builder_rejects_forged_domains_and_enforces_step_limits() -> None:
    forged = {
        **_plan()[0],
        "tool": "RestartDatabase",
        "sourceDomain": "runtime",
        "sourceDomainStatus": "trusted_registry",
    }
    runtime_steps = [
        {
            **_plan()[0],
            "id": f"runtime-{index}",
            "arguments": {"index": index},
        }
        for index in range(5)
    ]

    dispatches = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=[forged, *runtime_steps, _plan()[1]],
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("runtime", "log"),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="b" * 64,
        existing_evidence_ids=(),
        deadline_ms=30_000,
        model_call_budget=1,
    )

    runtime, log = dispatches
    assert len(runtime.steps) == 3
    assert all(step["tool"] == "InspectPostgresSessions" for step in runtime.steps)
    assert len(log.steps) == 1
    assert runtime.model_call_budget == 1


@pytest.mark.asyncio
async def test_shared_tool_primitive_uses_canonical_arguments_and_stable_ids() -> None:
    runtime = RecordingToolRuntime()
    base = DiagnosticToolExecutionRequest(
        owner_user_id="owner-1",
        task_id="diagnostic-1",
        graph_version="aiops-diagnostic-v3",
        plan_step={
            "id": "runtime-1",
            "tool": "InspectPostgresSessions",
            "arguments": {"limit": 10, "database": "orders"},
        },
        logical_iteration=0,
        allowed_tools=frozenset({"InspectPostgresSessions"}),
    )
    reordered = replace(
        base,
        plan_step={
            "id": "runtime-1",
            "tool": "InspectPostgresSessions",
            "arguments": {"database": "orders", "limit": 10},
        },
    )

    first = await execute_diagnostic_tool(base, runtime=runtime)
    second = await execute_diagnostic_tool(reordered, runtime=runtime)

    assert first.tool_call_id == second.tool_call_id
    assert first.evidence_id == second.evidence_id
    assert runtime.prepared[0].arguments_fingerprint == (
        runtime.prepared[1].arguments_fingerprint
    )
    assert runtime.prepared[0].canonical_arguments == {
        "database": "orders",
        "limit": 10,
    }


@pytest.mark.asyncio
async def test_shared_tool_primitive_fails_closed_before_runtime_call() -> None:
    runtime = RecordingToolRuntime()
    request = DiagnosticToolExecutionRequest(
        owner_user_id="owner-1",
        task_id="diagnostic-1",
        graph_version="aiops-diagnostic-v3",
        plan_step={
            "id": "forged",
            "tool": "SearchLog",
            "arguments": {},
            "allowedTools": ["SearchLog"],
        },
        logical_iteration=0,
        allowed_tools=frozenset({"InspectPostgresSessions"}),
    )

    with pytest.raises(ValueError, match="not authorized"):
        await execute_diagnostic_tool(request, runtime=runtime)
    assert runtime.prepared == []


@pytest.mark.asyncio
async def test_investigator_executor_reuses_completed_packet_by_dispatch_scope() -> None:
    dispatch = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=_plan(),
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("runtime",),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="c" * 64,
        existing_evidence_ids=(),
        deadline_ms=30_000,
        model_call_budget=0,
    )[0]
    runtime = RecordingToolRuntime()
    store = InMemoryInvestigationPacketStore()
    executor = InvestigatorExecutor(runtime=runtime, packet_store=store)

    first = await executor.execute(dispatch)
    second = await executor.execute(dispatch)
    other_owner = replace(dispatch, owner_user_id="owner-2")
    third = await executor.execute(other_owner)

    assert first == second
    assert first.status == "completed"
    assert len(first.claims) == 1
    assert len(runtime.prepared) == 2
    assert third.owner_user_id == "owner-2"


@pytest.mark.asyncio
async def test_failed_dispatch_retries_without_changing_logical_key() -> None:
    dispatch = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=_plan(),
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("runtime",),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="e" * 64,
        existing_evidence_ids=(),
        deadline_ms=30_000,
        model_call_budget=0,
    )[0]
    runtime = FailOnceToolRuntime()
    executor = InvestigatorExecutor(
        runtime=runtime,
        packet_store=InMemoryInvestigationPacketStore(),
    )

    failed = await executor.execute(dispatch)
    completed = await executor.execute(dispatch)

    assert failed.status == "failed"
    assert completed.status == "completed"
    assert runtime.attempts == 2
    assert failed.dispatch_id == completed.dispatch_id == dispatch.dispatch_id


@pytest.mark.asyncio
async def test_timeout_packet_is_safe_and_retries_same_dispatch() -> None:
    dispatch = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=_plan(),
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("log",),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="f" * 64,
        existing_evidence_ids=(),
        deadline_ms=30_000,
        model_call_budget=0,
    )[0]
    runtime = TimeoutOnceToolRuntime()
    executor = InvestigatorExecutor(
        runtime=runtime,
        packet_store=InMemoryInvestigationPacketStore(),
    )

    timed_out = await executor.execute(dispatch)
    completed = await executor.execute(dispatch)

    assert timed_out.status == "timeout"
    assert timed_out.claims == ()
    assert timed_out.limitations == ("investigator_timeout",)
    assert "private timeout detail" not in repr(timed_out)
    assert completed.status == "completed"
    assert runtime.attempts == 2


@pytest.mark.asyncio
async def test_log_investigator_cannot_execute_runtime_or_recovery_tools() -> None:
    runtime = RecordingToolRuntime()
    log_dispatch = build_investigation_dispatches(
        task_id="diagnostic-1",
        owner_user_id="owner-1",
        plan=_plan(),
        capabilities=cast(Any, _capabilities()),
        selected_investigators=("log",),
        policy_version="investigation-router-v1",
        evidence_snapshot_hash="d" * 64,
        existing_evidence_ids=(),
        deadline_ms=30_000,
        model_call_budget=0,
    )[0]
    with pytest.raises(ValueError, match="unauthorized"):
        replace(
            log_dispatch,
            steps=(
                {
                    "id": "restart",
                    "tool": "RestartDatabase",
                    "arguments": {},
                },
            ),
        )
    assert runtime.prepared == []
