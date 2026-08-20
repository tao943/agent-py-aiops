"""Source-scoped Investigator dispatch and shared diagnostic tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from super_ai.aiops.investigation import (
    EvidenceClaim,
    EvidencePacket,
    InvestigatorCapability,
    InvestigatorType,
    JsonValue,
)
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
