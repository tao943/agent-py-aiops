from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict

from super_ai.aiops.decision_validation import invoke_bounded_structured_role
from super_ai.aiops.execution import ExecutionIdentity, ExecutionResult
from super_ai.aiops.investigation_runtime import (
    DiagnosticToolExecutionRequest,
    DiagnosticToolExecutionResult,
    PreparedDiagnosticToolExecution,
    SpecialistExecutor,
)
from super_ai.aiops.specialists import (
    SharedRunContext,
    SpecialistAssignment,
    SpecialistEvidenceAnalysisOutput,
    SpecialistLocalPlanOutput,
)
from super_ai.memory.repositories import JsonDict


class TinyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class QueueInvoker:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.prompts: list[object] = []

    async def ainvoke(self, input: object) -> object:
        self.prompts.append(input)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StructuredQueueModel(QueueInvoker):
    def with_structured_output(
        self,
        _schema: type[object],
        **_kwargs: Any,
    ) -> QueueInvoker:
        return self


@pytest.mark.asyncio
async def test_bounded_role_accepts_valid_structured_output() -> None:
    model = StructuredQueueModel(
        [{"parsed": TinyOutput(value="ok"), "parsing_error": None}]
    )

    outcome = await invoke_bounded_structured_role(
        model=cast(Any, model),
        schema=TinyOutput,
        prompt="public prompt",
        correction_prompt="format correction",
        role="local_plan",
    )

    assert outcome.value == TinyOutput(value="ok")
    assert outcome.error_category is None
    assert outcome.attempts == 1
    assert outcome.audits[0].role == "local_plan"
    assert outcome.audits[0].error_category is None


@pytest.mark.asyncio
async def test_bounded_role_allows_one_format_only_correction() -> None:
    model = StructuredQueueModel(
        [
            {"parsed": None, "parsing_error": ValueError("private output")},
            {"parsed": TinyOutput(value="corrected"), "parsing_error": None},
        ]
    )

    outcome = await invoke_bounded_structured_role(
        model=cast(Any, model),
        schema=TinyOutput,
        prompt="public prompt",
        correction_prompt="format correction",
        role="evidence_analysis",
    )

    assert outcome.value == TinyOutput(value="corrected")
    assert outcome.attempts == 2
    assert model.prompts == ["public prompt", "public prompt\n\nformat correction"]
    assert all("private output" not in str(audit) for audit in outcome.audits)


@pytest.mark.asyncio
async def test_bounded_role_reports_retry_exhaustion_without_raw_output() -> None:
    model = StructuredQueueModel(
        [
            {"parsed": None, "parsing_error": ValueError("secret-one")},
            {"parsed": None, "parsing_error": ValueError("secret-two")},
        ]
    )

    outcome = await invoke_bounded_structured_role(
        model=cast(Any, model),
        schema=TinyOutput,
        prompt="public prompt",
        correction_prompt="format correction",
        role="local_plan",
    )

    assert outcome.value is None
    assert outcome.error_category == "retry_exhausted"
    assert outcome.attempts == 2
    assert "secret" not in str(outcome)


class RecordingRuntime:
    def __init__(self) -> None:
        self.prepared: list[PreparedDiagnosticToolExecution] = []
        self.active = 0
        self.maximum_active = 0

    async def execute_prepared(
        self,
        request: DiagnosticToolExecutionRequest,
        prepared: PreparedDiagnosticToolExecution,
    ) -> DiagnosticToolExecutionResult:
        del request
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.prepared.append(prepared)
        self.active -= 1
        return DiagnosticToolExecutionResult(
            status="completed",
            evidence_id=prepared.evidence_id,
            tool_call_id=prepared.tool_call_id,
            safe_output={"tool": prepared.tool_name, "healthy": True},
            safe_summary=f"{prepared.tool_name} returned public evidence.",
            events=(),
        )


class FailFirstToolRuntime(RecordingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    async def execute_prepared(
        self,
        request: DiagnosticToolExecutionRequest,
        prepared: PreparedDiagnosticToolExecution,
    ) -> DiagnosticToolExecutionResult:
        if not self.failed_once:
            self.failed_once = True
            raise ConnectionError("private runtime detail")
        return await super().execute_prepared(request, prepared)


class BlockingRoleModel(StructuredQueueModel):
    def __init__(self) -> None:
        super().__init__([])
        self.cancelled = False

    async def ainvoke(self, input: object) -> object:
        self.prompts.append(input)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("The blocking model must be cancelled.")


class InMemoryCoordinator:
    def __init__(self) -> None:
        self.outputs: dict[str, JsonDict] = {}
        self.operations = 0

    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[JsonDict]],
        *,
        outcome_known_on_error: bool = True,
    ) -> ExecutionResult:
        del outcome_known_on_error
        cached = self.outputs.get(identity.execution_key)
        if cached is not None:
            return ExecutionResult(cached, True, 1)
        self.operations += 1
        output = await operation()
        self.outputs[identity.execution_key] = output
        return ExecutionResult(output, False, 1)


class SpecialistRoleModel:
    def __init__(
        self,
        runtime: RecordingRuntime,
        *,
        altered_log_scope: bool = False,
        require_schema_prompt: bool = False,
        require_evidence_prompt: bool = False,
    ) -> None:
        self.runtime = runtime
        self.altered_log_scope = altered_log_scope
        self.require_schema_prompt = require_schema_prompt
        self.require_evidence_prompt = require_evidence_prompt
        self.prompts: list[str] = []
        self.schema: type[BaseModel] | None = None
        self.structured_output_methods: list[object] = []

    def with_structured_output(
        self,
        schema: type[BaseModel],
        **kwargs: Any,
    ) -> SpecialistRoleModel:
        self.schema = schema
        self.structured_output_methods.append(kwargs.get("method"))
        return self

    async def ainvoke(self, input: object) -> object:
        self.prompts.append(str(input))
        if self.schema is SpecialistLocalPlanOutput:
            if self.require_schema_prompt:
                assert '"step_id"' in str(input)
                assert '"proposed_arguments"' in str(input)
            if self.altered_log_scope:
                steps: list[dict[str, object]] = [
                    {
                        "step_id": "log-1",
                        "tool_name": "SearchLog",
                        "tested_hypotheses": ["lifecycle_failure"],
                        "causal_intent": "trigger",
                        "proposed_arguments": {"Region": "other"},
                    }
                ]
            else:
                steps = [
                    {
                        "step_id": "runtime-1",
                        "tool_name": "InspectOrderPoolState",
                        "tested_hypotheses": ["lifecycle_failure"],
                        "causal_intent": "mechanism",
                        "proposed_arguments": {},
                    },
                    {
                        "step_id": "runtime-2",
                        "tool_name": "VerifyOrderDatabaseReachability",
                        "tested_hypotheses": ["database_unreachable"],
                        "causal_intent": "impact",
                        "proposed_arguments": {},
                    },
                ]
            parsed: BaseModel = SpecialistLocalPlanOutput.model_validate(
                {"steps": steps}
            )
        else:
            if self.require_evidence_prompt:
                assert '"safeEvidence"' in str(input)
                assert '"healthy": true' in str(input)
                assert "evidence_" in str(input)
            evidence_ids = [item.evidence_id for item in self.runtime.prepared]
            parsed = SpecialistEvidenceAnalysisOutput.model_validate(
                {
                    "tested_hypotheses": [
                        "lifecycle_failure",
                        "database_unreachable",
                    ],
                    "fact_candidates": [],
                    "proposed_assessments": [],
                    "unresolved_questions": [],
                }
            )
            assert evidence_ids
        return {"parsed": parsed, "parsing_error": None}


def _context(now: datetime) -> SharedRunContext:
    return SharedRunContext(
        owner_user_id="owner-safe",
        task_id="task-safe",
        graph_version="evidence-driven-v4",
        public_incident_input={"service": "order-api"},
        public_hypotheses=("lifecycle_failure", "database_unreachable"),
        decision_vocabulary={"components": ("order-api", "postgresql")},
        allowed_tools_by_specialist={
            "runtime": frozenset(
                {"InspectOrderPoolState", "VerifyOrderDatabaseReachability"}
            ),
            "log": frozenset({"SearchLog"}),
        },
        trusted_arguments_by_specialist={
            "runtime": {
                "InspectOrderPoolState": {},
                "VerifyOrderDatabaseReachability": {},
            },
            "log": {
                "SearchLog": {
                    "Region": "ap-guangzhou",
                    "TopicId": "topic-safe",
                    "From": 10,
                    "To": 20,
                    "Query": 'incident_id:"safe"',
                    "Limit": 20,
                }
            },
        },
        global_soft_deadline_at=now + timedelta(minutes=4),
        global_hard_deadline_at=now + timedelta(minutes=6),
        global_model_budget=8,
    )


def _assignment(now: datetime, *, role: str = "runtime") -> SpecialistAssignment:
    if role == "runtime":
        tools = frozenset(
            {"InspectOrderPoolState", "VerifyOrderDatabaseReachability"}
        )
        bindings: Mapping[str, Mapping[str, object]] = {
            "InspectOrderPoolState": {},
            "VerifyOrderDatabaseReachability": {},
        }
    else:
        tools = frozenset({"SearchLog"})
        bindings = {
            "SearchLog": {
                "Region": "ap-guangzhou",
                "TopicId": "topic-safe",
                "From": 10,
                "To": 20,
                "Query": 'incident_id:"safe"',
                "Limit": 20,
            }
        }
    return SpecialistAssignment(
        role=cast(Any, role),
        objective="Test public incident hypotheses.",
        hypotheses_to_test=("lifecycle_failure", "database_unreachable"),
        required_causal_roles=("trigger", "mechanism", "impact"),
        allowed_tools=tools,
        trusted_arguments_by_tool=cast(Any, bindings),
        maximum_tool_steps=3,
        model_call_budget=2,
        soft_deadline_at=now + timedelta(minutes=2),
        hard_deadline_at=now + timedelta(minutes=3),
    )


@pytest.mark.asyncio
async def test_specialist_runs_two_model_roles_and_serial_tools() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    runtime = RecordingRuntime()
    coordinator = InMemoryCoordinator()
    model = SpecialistRoleModel(
        runtime,
        require_schema_prompt=True,
        require_evidence_prompt=True,
    )
    executor = SpecialistExecutor(
        runtime=runtime,
        model=cast(Any, model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )

    result = await executor.execute(_context(now), _assignment(now))

    assert result.terminal_status == "completed"
    assert result.model_call_count == 2
    assert [item.tool_name for item in runtime.prepared] == [
        "InspectOrderPoolState",
        "VerifyOrderDatabaseReachability",
    ]
    assert runtime.maximum_active == 1
    assert len(result.evidence_ids) == 2
    assert coordinator.operations == 2
    assert model.structured_output_methods == ["json_mode", "json_mode"]


@pytest.mark.asyncio
async def test_log_specialist_rejects_scope_changes_before_tool_call() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    runtime = RecordingRuntime()
    model = SpecialistRoleModel(runtime, altered_log_scope=True)
    executor = SpecialistExecutor(
        runtime=runtime,
        model=cast(Any, model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, InMemoryCoordinator()),
        now=lambda: now,
    )

    result = await executor.execute(_context(now), _assignment(now, role="log"))

    assert result.terminal_status == "failed"
    assert result.unresolved_questions == ("specialist_plan_scope_rejected",)
    assert runtime.prepared == []
    assert result.model_call_count == 1


def test_local_plan_prompt_states_code_owned_scope_constraints() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)

    prompt = SpecialistExecutor._local_plan_prompt(  # pyright: ignore[reportPrivateUsage]
        _context(now),
        replace(_assignment(now, role="log"), maximum_tool_steps=1),
    )

    assert '"maximumSteps": 1' in prompt
    assert '"causalRoles": ["trigger", "mechanism", "impact"]' in prompt
    assert "proposed_arguments must exactly equal" in prompt
    assert "do not add, remove, or modify any argument" in prompt


def test_local_plan_uses_code_owned_causal_intent_bindings() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    assignment = _assignment(now)
    output = SpecialistLocalPlanOutput.model_validate(
        {
            "steps": [
                {
                    "step_id": "runtime-1",
                    "tool_name": "InspectOrderPoolState",
                    "tested_hypotheses": ["lifecycle_failure"],
                    "causal_intent": "impact",
                    "proposed_arguments": {},
                },
                {
                    "step_id": "runtime-2",
                    "tool_name": "VerifyOrderDatabaseReachability",
                    "tested_hypotheses": ["database_unreachable"],
                    "causal_intent": "trigger",
                    "proposed_arguments": {},
                },
            ]
        }
    )

    steps = SpecialistExecutor._validate_plan(  # pyright: ignore[reportPrivateUsage]
        _context(now), assignment, output
    )
    prompt = SpecialistExecutor._local_plan_prompt(  # pyright: ignore[reportPrivateUsage]
        _context(now), assignment
    )

    assert [item.causal_intent for item in steps] == ["mechanism", "impact"]
    assert '"allowedCausalIntentsByTool"' in prompt
    assert '"InspectOrderPoolState": ["mechanism"]' in prompt


@pytest.mark.asyncio
async def test_soft_deadline_prevents_new_local_plan() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    runtime = RecordingRuntime()
    model = SpecialistRoleModel(runtime)
    executor = SpecialistExecutor(
        runtime=runtime,
        model=cast(Any, model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, InMemoryCoordinator()),
        now=lambda: now + timedelta(minutes=2, seconds=1),
    )

    result = await executor.execute(_context(now), _assignment(now))

    assert result.terminal_status == "timeout"
    assert result.model_call_count == 0
    assert model.prompts == []


@pytest.mark.asyncio
async def test_completed_model_roles_are_reused_after_worker_restart() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    coordinator = InMemoryCoordinator()
    first_runtime = RecordingRuntime()
    first_model = SpecialistRoleModel(first_runtime)
    first = SpecialistExecutor(
        runtime=first_runtime,
        model=cast(Any, first_model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )
    await first.execute(_context(now), _assignment(now))

    second_runtime = RecordingRuntime()
    second_model = SpecialistRoleModel(second_runtime)
    second = SpecialistExecutor(
        runtime=second_runtime,
        model=cast(Any, second_model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )
    replayed = await second.execute(_context(now), _assignment(now))

    assert replayed.terminal_status == "completed"
    assert second_model.prompts == []
    assert coordinator.operations == 2


@pytest.mark.asyncio
async def test_structured_output_method_is_part_of_checkpoint_identity() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    coordinator = InMemoryCoordinator()
    first_runtime = RecordingRuntime()
    first_model = SpecialistRoleModel(first_runtime)
    first = SpecialistExecutor(
        runtime=first_runtime,
        model=cast(Any, first_model),
        structured_output_method="function_calling",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )
    await first.execute(_context(now), _assignment(now))

    second_runtime = RecordingRuntime()
    second_model = SpecialistRoleModel(second_runtime)
    second = SpecialistExecutor(
        runtime=second_runtime,
        model=cast(Any, second_model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )
    result = await second.execute(_context(now), _assignment(now))

    assert result.terminal_status == "completed"
    assert second_model.structured_output_methods == ["json_mode", "json_mode"]
    assert coordinator.operations == 4


@pytest.mark.asyncio
async def test_worker_restart_reuses_plan_and_resumes_before_analysis() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    coordinator = InMemoryCoordinator()
    runtime = FailFirstToolRuntime()
    first_model = SpecialistRoleModel(runtime)
    first = SpecialistExecutor(
        runtime=runtime,
        model=cast(Any, first_model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )

    failed = await first.execute(_context(now), _assignment(now))

    assert failed.terminal_status == "failed"
    assert len(first_model.prompts) == 1
    second_model = SpecialistRoleModel(runtime)
    second = SpecialistExecutor(
        runtime=runtime,
        model=cast(Any, second_model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, coordinator),
        now=lambda: now,
    )

    resumed = await second.execute(_context(now), _assignment(now))

    assert resumed.terminal_status == "completed"
    assert len(second_model.prompts) == 1
    assert len(resumed.evidence_ids) == 2
    assert coordinator.operations == 2


@pytest.mark.asyncio
async def test_hard_deadline_cancels_an_inflight_local_plan() -> None:
    now = datetime.now(timezone.utc)
    model = BlockingRoleModel()
    assignment = SpecialistAssignment(
        role="runtime",
        objective="Test public incident hypotheses.",
        hypotheses_to_test=("lifecycle_failure", "database_unreachable"),
        required_causal_roles=("trigger", "mechanism", "impact"),
        allowed_tools=frozenset(
            {"InspectOrderPoolState", "VerifyOrderDatabaseReachability"}
        ),
        trusted_arguments_by_tool={
            "InspectOrderPoolState": {},
            "VerifyOrderDatabaseReachability": {},
        },
        maximum_tool_steps=3,
        model_call_budget=2,
        soft_deadline_at=now + timedelta(milliseconds=50),
        hard_deadline_at=now + timedelta(milliseconds=100),
    )
    executor = SpecialistExecutor(
        runtime=RecordingRuntime(),
        model=cast(Any, model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, InMemoryCoordinator()),
    )

    result = await asyncio.wait_for(
        executor.execute(_context(now), assignment),
        timeout=0.5,
    )

    assert result.terminal_status == "timeout"
    assert result.unresolved_questions == ("specialist_hard_deadline_expired",)
    assert result.model_call_count == 1
    assert model.cancelled is True


@pytest.mark.asyncio
async def test_assignment_model_budget_prevents_evidence_analysis() -> None:
    now = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    runtime = RecordingRuntime()
    model = SpecialistRoleModel(runtime)
    executor = SpecialistExecutor(
        runtime=runtime,
        model=cast(Any, model),
        structured_output_method="json_mode",
        execution_coordinator=cast(Any, InMemoryCoordinator()),
        now=lambda: now,
    )

    result = await executor.execute(
        _context(now),
        replace(_assignment(now), model_call_budget=1),
    )

    assert result.terminal_status == "inconclusive"
    assert result.model_call_count == 1
    assert len(model.prompts) == 1
    assert result.unresolved_questions == ("specialist_model_budget_exhausted",)
    assert len(result.evidence_ids) == 2
