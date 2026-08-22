"""Source-scoped Investigator dispatch and shared diagnostic tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Generic, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel

from super_ai.aiops.causal_intents import allowed_causal_intents
from super_ai.aiops.decision_validation import (
    DecisionValidationErrorCategory,
    ValidationErrorCode,
    ValidationErrorPhase,
    ValidationHttpStatusClass,
    invoke_bounded_structured_role,
)
from super_ai.aiops.execution import ExecutionCoordinator, ExecutionIdentity
from super_ai.aiops.investigation import (
    EvidenceClaim,
    EvidencePacket,
    InvestigatorCapability,
    InvestigatorType,
    JsonValue,
)
from super_ai.aiops.specialists import (
    PublicAssessmentSignal,
    SharedRunContext,
    SpecialistAnalysisErrorCode,
    SpecialistAnalysisStatus,
    SpecialistAssignment,
    SpecialistEvidenceAnalysisOutput,
    SpecialistEvidenceStatus,
    SpecialistLocalPlanOutput,
    SpecialistPlanStep,
    SpecialistResult,
    derive_specialist_terminal_status,
)
from super_ai.llm import ChatModel
from super_ai.llm.config import StructuredOutputMethod
from super_ai.mcp.tool_arguments import tool_step_fingerprint
from super_ai.memory.repositories import JsonDict

DispatchInvestigatorType = Literal["runtime", "log"]

_DISPATCH_ORDER: tuple[DispatchInvestigatorType, ...] = ("runtime", "log")
_STEP_LIMITS: Mapping[DispatchInvestigatorType, int] = {"runtime": 3, "log": 2}
_PRIVATE_TOKENS = (
    "groundtruth",
    "oracle",
    "primarycause",
    "scorerules",
    "scenarioid",
    "runid",
)


@dataclass(frozen=True, slots=True)
class InvestigationDispatch:
    task_id: str
    owner_user_id: str
    dispatch_id: str
    dispatch_key: str
    investigator_type: DispatchInvestigatorType
    objective: str
    tests_hypotheses: tuple[str, ...]
    missing_causal_roles: tuple[str, ...]
    steps: tuple[dict[str, object], ...]
    allowed_tools: frozenset[str]
    existing_evidence_ids: tuple[str, ...]
    deadline_ms: int
    model_call_budget: int

    def __post_init__(self) -> None:
        text_fields = (
            self.task_id,
            self.owner_user_id,
            self.dispatch_id,
            self.dispatch_key,
            self.objective,
        )
        if any(not value.strip() for value in text_fields):
            raise ValueError("Investigation dispatch identity is required.")
        _reject_private_text((*text_fields, *self.tests_hypotheses))
        if self.investigator_type not in _DISPATCH_ORDER:
            raise ValueError("Investigation dispatch type is invalid.")
        if len(self.dispatch_key) != 64 or any(
            character not in "0123456789abcdef" for character in self.dispatch_key
        ):
            raise ValueError("Investigation dispatch key must be a SHA-256 digest.")
        if not self.steps or len(self.steps) > _STEP_LIMITS[self.investigator_type]:
            raise ValueError("Investigation dispatch step count is invalid.")
        if self.deadline_ms <= 0:
            raise ValueError("Investigation dispatch deadline must be positive.")
        if self.model_call_budget not in {0, 1}:
            raise ValueError("Investigation dispatch model call budget is invalid.")
        if not self.allowed_tools:
            raise ValueError("Investigation dispatch requires allowed tools.")
        for step in self.steps:
            if str(step.get("tool") or "") not in self.allowed_tools:
                raise ValueError("Investigation dispatch contains an unauthorized tool.")


@dataclass(frozen=True, slots=True)
class DiagnosticToolExecutionRequest:
    owner_user_id: str
    task_id: str
    graph_version: str
    plan_step: Mapping[str, object]
    logical_iteration: int
    allowed_tools: frozenset[str]

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.owner_user_id, self.task_id, self.graph_version)
        ):
            raise ValueError("Diagnostic tool execution identity is required.")
        if self.logical_iteration < 0:
            raise ValueError("Diagnostic tool logical iteration cannot be negative.")


@dataclass(frozen=True, slots=True)
class PreparedDiagnosticToolExecution:
    plan_step_id: str
    tool_name: str
    canonical_arguments: JsonDict
    arguments_fingerprint: str
    tool_call_id: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class DiagnosticToolExecutionResult:
    status: Literal["completed", "failed"]
    evidence_id: str
    tool_call_id: str
    safe_output: object
    safe_summary: str
    events: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise ValueError("Diagnostic tool result status is invalid.")
        if not self.evidence_id.strip() or not self.tool_call_id.strip():
            raise ValueError("Diagnostic tool result requires stable IDs.")
        if not self.safe_summary.strip():
            raise ValueError("Diagnostic tool result requires a safe summary.")


class DiagnosticToolRuntime(Protocol):
    async def execute_prepared(
        self,
        request: DiagnosticToolExecutionRequest,
        prepared: PreparedDiagnosticToolExecution,
    ) -> DiagnosticToolExecutionResult: ...


class InvestigationPacketStore(Protocol):
    async def load(
        self, *, owner_user_id: str, task_id: str, dispatch_key: str
    ) -> EvidencePacket | None: ...

    async def save(
        self, *, dispatch_key: str, packet: EvidencePacket
    ) -> EvidencePacket: ...


class InMemoryInvestigationPacketStore:
    """Small test/local store; durable checkpoint storage is wired by the graph runtime."""

    def __init__(self) -> None:
        self._packets: dict[tuple[str, str, str], EvidencePacket] = {}
        self._lock = asyncio.Lock()

    async def load(
        self, *, owner_user_id: str, task_id: str, dispatch_key: str
    ) -> EvidencePacket | None:
        async with self._lock:
            packet = self._packets.get((owner_user_id, task_id, dispatch_key))
            if packet is None or packet.status in {"failed", "timeout"}:
                return None
            return packet

    async def save(
        self, *, dispatch_key: str, packet: EvidencePacket
    ) -> EvidencePacket:
        key = (packet.owner_user_id, packet.task_id, dispatch_key)
        async with self._lock:
            existing = self._packets.get(key)
            if existing is not None:
                if existing.status in {"failed", "timeout"}:
                    self._packets[key] = packet
                    return packet
                if existing != packet:
                    raise ValueError("Completed dispatch packet conflicts with stored output.")
                return existing
            self._packets[key] = packet
            return packet


def build_investigation_dispatches(
    *,
    task_id: str,
    owner_user_id: str,
    plan: Sequence[Mapping[str, object]],
    capabilities: Mapping[InvestigatorType, InvestigatorCapability],
    selected_investigators: Sequence[InvestigatorType],
    policy_version: str,
    evidence_snapshot_hash: str,
    existing_evidence_ids: Sequence[str],
    deadline_ms: int,
    model_call_budget: int,
    missing_causal_roles: Sequence[str] = (),
) -> tuple[InvestigationDispatch, ...]:
    """Build stable bounded Dispatches only from registry-normalized plan steps."""
    if len(evidence_snapshot_hash) != 64 or any(
        character not in "0123456789abcdef"
        for character in evidence_snapshot_hash
    ):
        raise ValueError("Evidence snapshot hash must be a SHA-256 digest.")
    selected = set(selected_investigators)
    dispatches: list[InvestigationDispatch] = []
    remaining_model_call_budget = max(model_call_budget, 0)
    for investigator_type in _DISPATCH_ORDER:
        if investigator_type not in selected:
            continue
        capability = capabilities.get(investigator_type)
        if capability is None or not capability.available:
            continue
        allowed_tools = capability.allowed_tools
        eligible = [
            dict(step)
            for step in plan
            if step.get("sourceDomain") == investigator_type
            and step.get("sourceDomainStatus") == "trusted_registry"
            and str(step.get("tool") or "") in allowed_tools
        ]
        eligible.sort(
            key=lambda step: (
                str(step.get("id") or ""),
                str(step.get("tool") or ""),
                tool_step_fingerprint(
                    str(step.get("tool") or ""),
                    _canonical_arguments(step.get("arguments")),
                ),
            )
        )
        steps = tuple(eligible[: _STEP_LIMITS[investigator_type]])
        if not steps:
            continue
        objective = f"Investigate {investigator_type} evidence for the current incident."
        objective_hash = hashlib.sha256(objective.encode("utf-8")).hexdigest()
        dispatch_key = hashlib.sha256(
            "\x1f".join(
                (
                    task_id,
                    policy_version,
                    investigator_type,
                    objective_hash,
                    evidence_snapshot_hash,
                )
            ).encode("utf-8")
        ).hexdigest()
        dispatch_model_call_budget = min(remaining_model_call_budget, 1)
        remaining_model_call_budget -= dispatch_model_call_budget
        dispatches.append(
            InvestigationDispatch(
                task_id=task_id,
                owner_user_id=owner_user_id,
                dispatch_id=f"dispatch_{dispatch_key[:48]}",
                dispatch_key=dispatch_key,
                investigator_type=investigator_type,
                objective=objective,
                tests_hypotheses=tuple(
                    sorted(
                        {
                            str(item)
                            for step in steps
                            for item in _string_sequence(step.get("testsHypotheses"))
                        }
                    )
                ),
                missing_causal_roles=tuple(sorted(set(missing_causal_roles))),
                steps=steps,
                allowed_tools=allowed_tools,
                existing_evidence_ids=tuple(sorted(set(existing_evidence_ids))),
                deadline_ms=deadline_ms,
                model_call_budget=dispatch_model_call_budget,
            )
        )
    return tuple(dispatches)


async def execute_diagnostic_tool(
    request: DiagnosticToolExecutionRequest, *, runtime: DiagnosticToolRuntime
) -> DiagnosticToolExecutionResult:
    """Apply one shared authorization/identity boundary before tool execution."""
    tool_name = str(request.plan_step.get("tool") or "")
    if not tool_name or tool_name not in request.allowed_tools:
        raise ValueError("Diagnostic tool is not authorized for this execution.")
    arguments = _canonical_arguments(request.plan_step.get("arguments"))
    fingerprint = tool_step_fingerprint(tool_name, arguments)
    plan_step_id = str(
        request.plan_step.get("id") or f"step_{request.logical_iteration + 1}"
    )
    prepared = PreparedDiagnosticToolExecution(
        plan_step_id=plan_step_id,
        tool_name=tool_name,
        canonical_arguments=arguments,
        arguments_fingerprint=fingerprint,
        tool_call_id=_stable_id(
            "tool", request.task_id, plan_step_id, tool_name, fingerprint
        ),
        evidence_id=_stable_id(
            "evidence", request.task_id, plan_step_id, tool_name, fingerprint
        ),
    )
    result = await runtime.execute_prepared(request, prepared)
    if (
        result.tool_call_id != prepared.tool_call_id
        or result.evidence_id != prepared.evidence_id
    ):
        raise ValueError("Diagnostic tool runtime returned unstable identities.")
    return result


class InvestigatorExecutor:
    def __init__(
        self,
        *,
        runtime: DiagnosticToolRuntime,
        packet_store: InvestigationPacketStore,
        collector_concurrency: int = 4,
    ) -> None:
        if collector_concurrency <= 0:
            raise ValueError("Collector concurrency must be positive.")
        self._runtime = runtime
        self._packet_store = packet_store
        self._collector_semaphore = asyncio.Semaphore(collector_concurrency)
        self._inflight_lock = asyncio.Lock()
        self._inflight: dict[tuple[str, str, str], asyncio.Task[EvidencePacket]] = {}

    async def execute(self, dispatch: InvestigationDispatch) -> EvidencePacket:
        cached = await self._packet_store.load(
            owner_user_id=dispatch.owner_user_id,
            task_id=dispatch.task_id,
            dispatch_key=dispatch.dispatch_key,
        )
        if cached is not None:
            return cached
        inflight_key = (
            dispatch.owner_user_id,
            dispatch.task_id,
            dispatch.dispatch_key,
        )
        async with self._inflight_lock:
            task = self._inflight.get(inflight_key)
            if task is None:
                task = asyncio.create_task(self._execute_with_collector_limit(dispatch))
                self._inflight[inflight_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._inflight_lock:
                    if self._inflight.get(inflight_key) is task:
                        self._inflight.pop(inflight_key, None)

    async def _execute_with_collector_limit(
        self, dispatch: InvestigationDispatch
    ) -> EvidencePacket:
        async with self._collector_semaphore:
            cached = await self._packet_store.load(
                owner_user_id=dispatch.owner_user_id,
                task_id=dispatch.task_id,
                dispatch_key=dispatch.dispatch_key,
            )
            if cached is not None:
                return cached
            return await self._execute_uncached(dispatch)

    async def _execute_uncached(
        self, dispatch: InvestigationDispatch
    ) -> EvidencePacket:
        results: list[tuple[Mapping[str, object], DiagnosticToolExecutionResult]] = []
        terminal_limitation: str | None = None
        for index, step in enumerate(dispatch.steps):
            try:
                result = await execute_diagnostic_tool(
                    DiagnosticToolExecutionRequest(
                        owner_user_id=dispatch.owner_user_id,
                        task_id=dispatch.task_id,
                        graph_version="aiops-diagnostic-v3",
                        plan_step=step,
                        logical_iteration=index,
                        allowed_tools=dispatch.allowed_tools,
                    ),
                    runtime=self._runtime,
                )
            except TimeoutError:
                terminal_limitation = "investigator_timeout"
                break
            except Exception:
                terminal_limitation = "investigator_execution_failed"
                break
            results.append((step, result))

        completed = [(step, result) for step, result in results if result.status == "completed"]
        failed = [(step, result) for step, result in results if result.status == "failed"]
        claims = tuple(
            _claim_from_result(dispatch.investigator_type, step, result)
            for step, result in completed
        )
        status: Literal["completed", "inconclusive", "failed", "timeout"]
        if completed and not failed and terminal_limitation is None:
            status = "completed"
        elif completed:
            status = "inconclusive"
        elif terminal_limitation == "investigator_timeout":
            status = "timeout"
        else:
            status = "failed"
        packet = EvidencePacket(
            task_id=dispatch.task_id,
            owner_user_id=dispatch.owner_user_id,
            dispatch_id=dispatch.dispatch_id,
            investigator_type=dispatch.investigator_type,
            status=status,
            claims=claims,
            limitations=(
                tuple(result.safe_summary for _, result in failed)
                + ((terminal_limitation,) if terminal_limitation is not None else ())
            ),
            tool_call_ids=tuple(result.tool_call_id for _, result in results),
            model_calls_used=0,
        )
        return await self._packet_store.save(
            dispatch_key=dispatch.dispatch_key,
            packet=packet,
        )


_RoleOutput = TypeVar("_RoleOutput", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class SpecialistRoleInvocation(Generic[_RoleOutput]):
    value: _RoleOutput | None
    error_category: DecisionValidationErrorCategory | None
    error_code: ValidationErrorCode | None
    attempt_count: int
    error_phase: ValidationErrorPhase | None
    retryable: bool | None
    http_status_class: ValidationHttpStatusClass | None


class SpecialistExecutor:
    """Run one source-scoped Specialist with two bounded model roles."""

    def __init__(
        self,
        *,
        runtime: DiagnosticToolRuntime,
        model: ChatModel,
        structured_output_method: StructuredOutputMethod,
        execution_coordinator: ExecutionCoordinator,
        now: Callable[[], datetime] | None = None,
        evidence_analysis_attempt_timeout_seconds: float = 30.0,
        retry_scheduling_margin_seconds: float = 1.0,
    ) -> None:
        self._runtime = runtime
        self._model = model
        self._structured_output_method: StructuredOutputMethod = (
            structured_output_method
        )
        self._execution_coordinator = execution_coordinator
        self._now = now or (lambda: datetime.now(timezone.utc))
        if evidence_analysis_attempt_timeout_seconds <= 0:
            raise ValueError("Evidence Analysis attempt timeout must be positive.")
        if retry_scheduling_margin_seconds <= 0:
            raise ValueError("Specialist retry scheduling margin must be positive.")
        self._evidence_analysis_attempt_timeout_seconds = (
            evidence_analysis_attempt_timeout_seconds
        )
        self._retry_scheduling_margin_seconds = retry_scheduling_margin_seconds

    async def execute(
        self,
        context: SharedRunContext,
        assignment: SpecialistAssignment,
    ) -> SpecialistResult:
        started_at = self._now()
        self._validate_assignment(context, assignment)
        if self._hard_expired(context, assignment):
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="timeout",
                analysis_error_code="specialist_hard_deadline_expired",
                hard_deadline_exceeded=True,
                unresolved_questions=("specialist_hard_deadline_expired",),
            )
        if self._soft_expired(context, assignment):
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="timeout",
                analysis_error_code="specialist_soft_deadline_expired",
                soft_deadline_exceeded=True,
                unresolved_questions=("specialist_soft_deadline_expired",),
            )

        try:
            plan_invocation = await asyncio.wait_for(
                self._run_role(
                    context=context,
                    assignment=assignment,
                    role_name="local_plan",
                    logical_iteration=0,
                    schema=SpecialistLocalPlanOutput,
                    prompt=self._local_plan_prompt(context, assignment),
                    correction_prompt=(
                        "Return only the required Local Plan schema with at most three steps."
                    ),
                ),
                timeout=self._remaining_hard_seconds(context, assignment),
            )
        except TimeoutError:
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="timeout",
                analysis_error_code="specialist_hard_deadline_expired",
                analysis_attempt_count=1,
                hard_deadline_exceeded=True,
                unresolved_questions=("specialist_hard_deadline_expired",),
                model_call_count=1,
            )
        local_plan = plan_invocation.value
        if local_plan is None:
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="skipped",
                unresolved_questions=(
                    plan_invocation.error_category or "specialist_local_plan_failed",
                ),
                model_call_count=1,
            )

        try:
            steps = self._validate_plan(context, assignment, local_plan)
        except ValueError:
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="skipped",
                unresolved_questions=("specialist_plan_scope_rejected",),
                model_call_count=1,
            )

        evidence_ids: list[str] = []
        completed_steps: list[str] = []
        tool_claims: list[EvidenceClaim] = []
        unresolved: list[str] = []
        for index, step in enumerate(steps):
            if self._hard_expired(context, assignment):
                unresolved.append("specialist_hard_deadline_expired")
                break
            if self._soft_expired(context, assignment):
                unresolved.append("specialist_soft_deadline_expired")
                break
            plan_step = {
                "id": step.step_id,
                "tool": step.tool_name,
                "arguments": _plain_json_mapping(
                    assignment.trusted_arguments_by_tool[step.tool_name]
                ),
                "testsHypotheses": list(step.tested_hypotheses),
                "causalIntent": step.causal_intent,
            }
            try:
                tool_result = await asyncio.wait_for(
                    execute_diagnostic_tool(
                        DiagnosticToolExecutionRequest(
                            owner_user_id=context.owner_user_id,
                            task_id=context.task_id,
                            graph_version=context.graph_version,
                            plan_step=plan_step,
                            logical_iteration=index,
                            allowed_tools=assignment.allowed_tools,
                        ),
                        runtime=self._runtime,
                    ),
                    timeout=self._remaining_hard_seconds(context, assignment),
                )
            except TimeoutError:
                unresolved.append("specialist_tool_timeout")
                break
            except Exception:
                unresolved.append("specialist_tool_failed")
                break
            if tool_result.status != "completed":
                unresolved.append("specialist_tool_failed")
                break
            evidence_ids.append(tool_result.evidence_id)
            completed_steps.append(step.step_id)
            tool_claims.append(_claim_from_result(assignment.role, plan_step, tool_result))

        if self._hard_expired(context, assignment):
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="timeout",
                analysis_error_code="specialist_hard_deadline_expired",
                hard_deadline_exceeded=True,
                expected_tool_count=len(steps),
                evidence_ids=tuple(evidence_ids),
                fact_candidates=tuple(tool_claims),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=1,
            )
        if self._soft_expired(context, assignment):
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="timeout",
                analysis_error_code="specialist_soft_deadline_expired",
                soft_deadline_exceeded=True,
                expected_tool_count=len(steps),
                evidence_ids=tuple(evidence_ids),
                fact_candidates=tuple(tool_claims),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=1,
            )

        if not evidence_ids and unresolved:
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="skipped",
                expected_tool_count=len(steps),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=1,
            )

        if min(assignment.model_call_budget, context.global_model_budget) < 2:
            unresolved.append("specialist_model_budget_exhausted")
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="skipped",
                analysis_error_code="specialist_model_budget_exhausted",
                expected_tool_count=len(steps),
                evidence_ids=tuple(evidence_ids),
                fact_candidates=tuple(tool_claims),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=1,
            )

        try:
            analysis_invocation = await asyncio.wait_for(
                self._run_role(
                    context=context,
                    assignment=assignment,
                    role_name="evidence_analysis",
                    logical_iteration=1,
                    schema=SpecialistEvidenceAnalysisOutput,
                    prompt=self._analysis_prompt(
                        assignment,
                        evidence_ids=tuple(evidence_ids),
                        completed_steps=tuple(completed_steps),
                        fact_candidates=tuple(tool_claims),
                    ),
                    correction_prompt=(
                        "Return only the required Evidence Analysis schema using "
                        "owned Evidence IDs."
                    ),
                    retry_guard=lambda: self._remaining_hard_seconds(
                        context, assignment
                    )
                    >= self._evidence_analysis_attempt_timeout_seconds
                    + self._retry_scheduling_margin_seconds,
                    attempt_timeout_seconds=(
                        self._evidence_analysis_attempt_timeout_seconds
                    ),
                ),
                timeout=self._remaining_hard_seconds(context, assignment),
            )
        except TimeoutError:
            unresolved.append("specialist_hard_deadline_expired")
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="timeout",
                analysis_error_code="specialist_hard_deadline_expired",
                analysis_attempt_count=1,
                hard_deadline_exceeded=True,
                expected_tool_count=len(steps),
                evidence_ids=tuple(evidence_ids),
                fact_candidates=tuple(tool_claims),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=2,
            )
        analysis = analysis_invocation.value
        if analysis is None:
            analysis_status, analysis_error_code = _specialist_analysis_failure(
                analysis_invocation
            )
            unresolved.append(
                analysis_error_code or "specialist_evidence_analysis_failed"
            )
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status=analysis_status,
                analysis_error_code=analysis_error_code,
                analysis_attempt_count=analysis_invocation.attempt_count,
                expected_tool_count=len(steps),
                evidence_ids=tuple(evidence_ids),
                fact_candidates=tuple(tool_claims),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=2,
            )
        try:
            facts, assessments = self._validate_analysis(
                assignment,
                analysis,
                evidence_ids=frozenset(evidence_ids),
            )
        except ValueError:
            unresolved.append("specialist_analysis_scope_rejected")
            return self._result(
                assignment=assignment,
                started_at=started_at,
                analysis_status="failed",
                analysis_error_code="scope_rejected",
                analysis_attempt_count=analysis_invocation.attempt_count,
                expected_tool_count=len(steps),
                evidence_ids=tuple(evidence_ids),
                fact_candidates=tuple(tool_claims),
                unresolved_questions=tuple(unresolved),
                completed_steps=tuple(completed_steps),
                model_call_count=2,
            )
        unresolved.extend(analysis.unresolved_questions)
        return self._result(
            assignment=assignment,
            started_at=started_at,
            analysis_status="complete",
            analysis_attempt_count=analysis_invocation.attempt_count,
            expected_tool_count=len(steps),
            evidence_ids=tuple(evidence_ids),
            fact_candidates=facts or tuple(tool_claims),
            proposed_assessments=assessments,
            unresolved_questions=tuple(unresolved),
            completed_steps=tuple(completed_steps),
            model_call_count=2,
        )

    async def _run_role(
        self,
        *,
        context: SharedRunContext,
        assignment: SpecialistAssignment,
        role_name: str,
        logical_iteration: int,
        schema: type[_RoleOutput],
        prompt: str,
        correction_prompt: str,
        retry_guard: Callable[[], bool] | None = None,
        attempt_timeout_seconds: float | None = None,
    ) -> SpecialistRoleInvocation[_RoleOutput]:
        effective_prompt = self._structured_role_prompt(prompt, schema=schema)
        prompt_fingerprint = hashlib.sha256(
            effective_prompt.encode("utf-8")
        ).hexdigest()

        async def operation() -> JsonDict:
            outcome = await invoke_bounded_structured_role(
                model=self._model,
                schema=schema,
                prompt=effective_prompt,
                correction_prompt=correction_prompt,
                role=role_name,
                structured_output_method=self._structured_output_method,
                retry_guard=retry_guard,
                attempt_timeout_seconds=attempt_timeout_seconds,
            )
            return {
                "status": "completed" if outcome.value is not None else "failed",
                "value": (
                    outcome.value.model_dump(mode="json")
                    if outcome.value is not None
                    else None
                ),
                "attempts": outcome.attempts,
                "errorCategory": outcome.error_category,
                "errorCode": outcome.error_code,
                "errorPhase": outcome.error_phase,
                "retryable": outcome.retryable,
                "httpStatusClass": outcome.http_status_class,
            }

        execution = await self._execution_coordinator.run_once(
            ExecutionIdentity(
                task_id=context.task_id,
                graph_version=context.graph_version,
                node_name=f"specialist_{assignment.role}_{role_name}",
                logical_iteration=logical_iteration,
                input_payload={
                    "role": assignment.role,
                    "roleName": role_name,
                    "promptFingerprint": prompt_fingerprint,
                    "structuredOutputMethod": self._structured_output_method,
                    "attemptTimeoutSeconds": attempt_timeout_seconds,
                },
            ),
            operation,
        )
        payload = execution.output
        value = payload.get("value")
        error_category = payload.get("errorCategory")
        error_code = payload.get("errorCode")
        error_phase = payload.get("errorPhase")
        http_status_class = payload.get("httpStatusClass")
        attempts = payload.get("attempts")
        retryable = payload.get("retryable")
        safe_invocation = SpecialistRoleInvocation[_RoleOutput](
            value=None,
            error_category=cast(
                DecisionValidationErrorCategory | None,
                error_category
                if error_category
                in {
                    "candidate_missing",
                    "deterministic_gap",
                    "model_call_failed",
                    "invalid_model_output",
                    "model_rejected",
                    "retry_exhausted",
                }
                else None,
            ),
            error_code=cast(
                ValidationErrorCode | None,
                error_code
                if error_code
                in {
                    "timeout",
                    "connection",
                    "authentication",
                    "permission_denied",
                    "rate_limit",
                    "provider_4xx",
                    "provider_5xx",
                    "structured_output_unsupported",
                    "model_call_budget_exhausted",
                    "hard_deadline_exceeded",
                    "retry_skipped_insufficient_deadline",
                    "unknown",
                }
                else None,
            ),
            attempt_count=(
                attempts
                if isinstance(attempts, int)
                and not isinstance(attempts, bool)
                and 0 <= attempts <= 2
                else 0
            ),
            error_phase=cast(
                ValidationErrorPhase | None,
                error_phase
                if error_phase
                in {"structured_invoker_setup", "model_invoke", "structured_parse"}
                else None,
            ),
            retryable=retryable if isinstance(retryable, bool) else None,
            http_status_class=cast(
                ValidationHttpStatusClass | None,
                http_status_class if http_status_class in {"4xx", "5xx"} else None,
            ),
        )
        if payload.get("status") != "completed" or value is None:
            return safe_invocation
        try:
            return replace(safe_invocation, value=schema.model_validate(value))
        except ValueError:
            return replace(
                safe_invocation,
                error_category="invalid_model_output",
                error_phase="structured_parse",
            )

    def _structured_role_prompt(
        self,
        prompt: str,
        *,
        schema: type[BaseModel],
    ) -> str:
        if self._structured_output_method != "json_mode":
            return prompt
        contract = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            f"{prompt}\n\nReturn only one valid JSON object matching this JSON Schema:\n"
            f"{contract}"
        )

    @staticmethod
    def _validate_assignment(
        context: SharedRunContext,
        assignment: SpecialistAssignment,
    ) -> None:
        if assignment.allowed_tools != context.allowed_tools_by_specialist[assignment.role]:
            raise ValueError("Specialist assignment tools do not match shared context.")
        if assignment.trusted_arguments_by_tool != context.trusted_arguments_by_specialist[
            assignment.role
        ]:
            raise ValueError("Specialist assignment bindings do not match shared context.")
        if not set(assignment.hypotheses_to_test).issubset(context.public_hypotheses):
            raise ValueError("Specialist assignment contains an unknown hypothesis.")

    @staticmethod
    def _validate_plan(
        context: SharedRunContext,
        assignment: SpecialistAssignment,
        output: SpecialistLocalPlanOutput,
    ) -> tuple[SpecialistPlanStep, ...]:
        if len(output.steps) > assignment.maximum_tool_steps:
            raise ValueError("Specialist plan exceeds its tool-step budget.")
        steps = tuple(item.to_contract() for item in output.steps)
        assigned_hypotheses = set(assignment.hypotheses_to_test)
        assigned_roles = set(assignment.required_causal_roles)
        normalized_steps: list[SpecialistPlanStep] = []
        for step in steps:
            if step.tool_name not in assignment.allowed_tools:
                raise ValueError("Specialist plan contains an unauthorized tool.")
            if not set(step.tested_hypotheses).issubset(assigned_hypotheses):
                raise ValueError("Specialist plan contains an unknown hypothesis.")
            allowed_roles = (
                set(allowed_causal_intents(step.tool_name)) & assigned_roles
            )
            if not allowed_roles:
                raise ValueError("Specialist plan contains an unassigned causal role.")
            if step.causal_intent not in allowed_roles:
                if len(allowed_roles) != 1:
                    raise ValueError("Specialist plan contains an invalid tool causal role.")
                step = replace(step, causal_intent=next(iter(allowed_roles)))
            trusted = assignment.trusted_arguments_by_tool[step.tool_name]
            if _plain_json_mapping(step.proposed_arguments) != _plain_json_mapping(trusted):
                raise ValueError("Specialist plan altered code-owned tool arguments.")
            context_trusted = context.trusted_arguments_by_specialist[assignment.role][
                step.tool_name
            ]
            if _plain_json_mapping(trusted) != _plain_json_mapping(context_trusted):
                raise ValueError("Specialist plan binding differs from shared context.")
            normalized_steps.append(step)
        return tuple(normalized_steps)

    @staticmethod
    def _validate_analysis(
        assignment: SpecialistAssignment,
        output: SpecialistEvidenceAnalysisOutput,
        *,
        evidence_ids: frozenset[str],
    ) -> tuple[tuple[EvidenceClaim, ...], tuple[PublicAssessmentSignal, ...]]:
        assigned_hypotheses = set(assignment.hypotheses_to_test)
        if not set(output.tested_hypotheses).issubset(assigned_hypotheses):
            raise ValueError("Specialist analysis contains an unknown hypothesis.")
        facts = tuple(item.to_contract() for item in output.fact_candidates)
        assessments = tuple(item.to_contract() for item in output.proposed_assessments)
        for fact in facts:
            if not set(fact.evidence_ids).issubset(evidence_ids):
                raise ValueError("Specialist analysis references foreign Evidence.")
            if not set(fact.supports).issubset(assigned_hypotheses):
                raise ValueError("Specialist fact supports an unknown hypothesis.")
            if not set(fact.refutes).issubset(assigned_hypotheses):
                raise ValueError("Specialist fact refutes an unknown hypothesis.")
        for assessment in assessments:
            if assessment.hypothesis_id not in assigned_hypotheses:
                raise ValueError("Specialist assessment contains an unknown hypothesis.")
            if not set(assessment.evidence_ids).issubset(evidence_ids):
                raise ValueError("Specialist assessment references foreign Evidence.")
        return facts, assessments

    @staticmethod
    def _local_plan_prompt(
        context: SharedRunContext,
        assignment: SpecialistAssignment,
    ) -> str:
        payload = {
            "objective": assignment.objective,
            "incident": _plain_json_mapping(context.public_incident_input),
            "hypotheses": list(assignment.hypotheses_to_test),
            "causalRoles": list(assignment.required_causal_roles),
            "maximumSteps": assignment.maximum_tool_steps,
            "allowedTools": sorted(assignment.allowed_tools),
            "allowedCausalIntentsByTool": {
                tool: sorted(
                    set(allowed_causal_intents(tool))
                    & set(assignment.required_causal_roles)
                )
                for tool in sorted(assignment.allowed_tools)
            },
            "trustedArguments": {
                tool: _plain_json_mapping(arguments)
                for tool, arguments in assignment.trusted_arguments_by_tool.items()
            },
        }
        return (
            "Create a bounded public Specialist Local Plan. "
            "Return no more than maximumSteps. Use only listed hypotheses, "
            "causalRoles, and allowedTools. causal_intent must be one of the "
            "allowedCausalIntentsByTool values for that tool. proposed_arguments "
            "must exactly equal "
            "the trustedArguments object for that tool; do not add, remove, or "
            "modify any argument.\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    @staticmethod
    def _analysis_prompt(
        assignment: SpecialistAssignment,
        *,
        evidence_ids: tuple[str, ...],
        completed_steps: tuple[str, ...],
        fact_candidates: tuple[EvidenceClaim, ...],
    ) -> str:
        payload = {
            "hypotheses": list(assignment.hypotheses_to_test),
            "ownedEvidenceIds": list(evidence_ids),
            "completedSteps": list(completed_steps),
            "safeEvidence": [
                {
                    "claimId": claim.claim_id,
                    "value": _json_value_for_prompt(claim.value),
                    "quality": claim.quality,
                    "causalRole": claim.causal_role,
                    "supports": list(claim.supports),
                    "refutes": list(claim.refutes),
                    "evidenceIds": list(claim.evidence_ids),
                    "targetComponent": claim.target_component,
                    "timeScope": claim.time_scope,
                }
                for claim in fact_candidates
            ],
        }
        return (
            "Analyze only these public Specialist evidence summaries. Return exactly "
            "tested_hypotheses, fact_candidates, proposed_assessments, and "
            "unresolved_questions as one JSON object with no wrapper or extra fields. "
            "Every Evidence ID must occur in ownedEvidenceIds and every hypothesis "
            "must occur in hypotheses. unresolved_questions is optional advice and "
            "does not mean evidence collection failed. Valid shape example: "
            '{"tested_hypotheses":["hypothesis-a"],"fact_candidates":[],'
            '"proposed_assessments":[],"unresolved_questions":[]}\n'
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    def _soft_expired(
        self,
        context: SharedRunContext,
        assignment: SpecialistAssignment,
    ) -> bool:
        now = self._now()
        return now >= min(context.global_soft_deadline_at, assignment.soft_deadline_at)

    def _hard_expired(
        self,
        context: SharedRunContext,
        assignment: SpecialistAssignment,
    ) -> bool:
        now = self._now()
        return now >= min(context.global_hard_deadline_at, assignment.hard_deadline_at)

    def _remaining_hard_seconds(
        self,
        context: SharedRunContext,
        assignment: SpecialistAssignment,
    ) -> float:
        deadline = min(context.global_hard_deadline_at, assignment.hard_deadline_at)
        return max(0.001, (deadline - self._now()).total_seconds())

    def _result(
        self,
        *,
        assignment: SpecialistAssignment,
        started_at: datetime,
        analysis_status: SpecialistAnalysisStatus,
        analysis_error_code: SpecialistAnalysisErrorCode | None = None,
        analysis_attempt_count: int = 0,
        soft_deadline_exceeded: bool = False,
        hard_deadline_exceeded: bool = False,
        expected_tool_count: int = 0,
        evidence_ids: tuple[str, ...] = (),
        fact_candidates: tuple[EvidenceClaim, ...] = (),
        proposed_assessments: tuple[PublicAssessmentSignal, ...] = (),
        unresolved_questions: tuple[str, ...] = (),
        completed_steps: tuple[str, ...] = (),
        model_call_count: int = 0,
    ) -> SpecialistResult:
        duration = max(0, int((self._now() - started_at).total_seconds() * 1000))
        evidence_status = _specialist_evidence_status(
            evidence_ids=evidence_ids,
            completed_tool_count=len(completed_steps),
            expected_tool_count=expected_tool_count,
        )
        return SpecialistResult.create(
            role=assignment.role,
            terminal_status=derive_specialist_terminal_status(
                evidence_status,
                analysis_status,
            ),
            evidence_status=evidence_status,
            analysis_status=analysis_status,
            analysis_error_code=analysis_error_code,
            analysis_attempt_count=analysis_attempt_count,
            soft_deadline_exceeded=soft_deadline_exceeded,
            hard_deadline_exceeded=hard_deadline_exceeded,
            expected_tool_count=expected_tool_count,
            tested_hypotheses=assignment.hypotheses_to_test,
            evidence_ids=evidence_ids,
            fact_candidates=fact_candidates,
            proposed_assessments=proposed_assessments,
            unresolved_questions=unresolved_questions,
            completed_steps=completed_steps,
            model_call_count=model_call_count,
            duration_ms=duration,
        )


def _specialist_evidence_status(
    *,
    evidence_ids: tuple[str, ...],
    completed_tool_count: int,
    expected_tool_count: int,
) -> SpecialistEvidenceStatus:
    if not evidence_ids:
        return "none"
    if expected_tool_count > 0 and completed_tool_count == expected_tool_count:
        return "complete"
    return "partial"


def _specialist_analysis_failure(
    invocation: SpecialistRoleInvocation[_RoleOutput],
) -> tuple[SpecialistAnalysisStatus, SpecialistAnalysisErrorCode | None]:
    if invocation.error_code == "retry_skipped_insufficient_deadline":
        return "degraded", "retry_skipped_insufficient_deadline"
    if invocation.error_category == "retry_exhausted":
        return "degraded", "retry_exhausted"
    if invocation.error_code in {"timeout", "hard_deadline_exceeded"}:
        return "timeout", "provider_timeout"
    if invocation.http_status_class == "4xx" or invocation.error_code in {
        "authentication",
        "permission_denied",
        "rate_limit",
        "structured_output_unsupported",
    }:
        return "failed", "provider_4xx"
    if invocation.http_status_class == "5xx" or invocation.error_code in {
        "connection",
        "provider_5xx",
        "unknown",
    }:
        return "failed", "provider_5xx"
    if invocation.error_phase == "structured_parse":
        return "degraded", "schema_validation_failed"
    return "failed", None


def _claim_from_result(
    investigator_type: DispatchInvestigatorType,
    step: Mapping[str, object],
    result: DiagnosticToolExecutionResult,
) -> EvidenceClaim:
    causal_role_value = step.get("causalIntent")
    causal_role = (
        str(causal_role_value)
        if causal_role_value in {"trigger", "mechanism", "impact"}
        else None
    )
    time_scope_value = step.get("timeScope")
    time_scope = (
        cast(Literal["incident_window", "current", "historical"], time_scope_value)
        if time_scope_value in {"incident_window", "current", "historical"}
        else "incident_window"
    )
    tool_name = str(step.get("tool") or investigator_type)
    return EvidenceClaim(
        claim_id=f"{tool_name}.observation",
        value=cast(JsonValue, result.safe_output),
        quality="direct",
        causal_role=causal_role,
        supports=(),
        refutes=(),
        evidence_ids=(result.evidence_id,),
        target_component=str(step.get("targetComponent") or tool_name),
        observed_at=datetime.now(timezone.utc),
        time_scope=time_scope,
    )


def _canonical_arguments(value: object) -> JsonDict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Diagnostic tool arguments must be an object.")
    raw = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise ValueError("Diagnostic tool argument keys must be strings.")
    try:
        serialized = json.dumps(
            dict(cast(Mapping[str, object], raw)),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        parsed = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError("Diagnostic tool arguments must be JSON-compatible.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Diagnostic tool arguments must be an object.")
    return cast(JsonDict, parsed)


def _plain_json_mapping(value: Mapping[str, object]) -> JsonDict:
    """Copy a frozen public mapping into canonical JSON-compatible data."""
    return _canonical_arguments(value)


def _json_value_for_prompt(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _json_value_for_prompt(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_json_value_for_prompt(item) for item in value]
    return value


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in cast(Sequence[object], value) if str(item))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:48]}"


def _reject_private_text(values: Sequence[str]) -> None:
    for value in values:
        normalized = "".join(character for character in value.casefold() if character.isalnum())
        if any(token in normalized for token in _PRIVATE_TOKENS):
            raise ValueError("Investigation dispatch contains evaluator-private data.")
