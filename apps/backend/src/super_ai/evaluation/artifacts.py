"""Convert persisted diagnostic records into a deterministic scoreable artifact."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.memory.repositories import (
    AgentToolCallAuditRecord,
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticStepRecord,
    DiagnosticTaskRecord,
    JsonDict,
)


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    record_id: str
    claim_id: str
    grounded: bool


@dataclass(frozen=True, slots=True)
class ArtifactToolCall:
    name: str
    status: str
    risk_tier: Literal["L0", "L1", "L2", "L3"]
    approved: bool = False
    verified: bool = False


@dataclass(frozen=True, slots=True)
class RunArtifact:
    scenario_id: str
    mode: str
    completed: bool
    report_produced: bool
    decision: RootCauseDecision | None
    evidence: tuple[ArtifactEvidence, ...]
    hypothesis_states: tuple[HypothesisState, ...]
    observation_decisions: tuple[ObservationDecision, ...]
    tool_calls: tuple[ArtifactToolCall, ...]
    plan_step_count: int
    duration_ms: int
    safety_events: tuple[str, ...]


def build_run_artifact(
    task: DiagnosticTaskRecord,
    steps: Sequence[DiagnosticStepRecord],
    evidence: Sequence[DiagnosticEvidenceRecord],
    tool_calls: Sequence[AgentToolCallAuditRecord],
    reports: Sequence[DiagnosticReportRecord],
) -> RunArtifact:
    """Build a score input from persisted records without reading report prose."""
    scenario_id = _required_task_text(task.input_payload, "benchmarkScenarioId")
    mode = _required_task_text(task.input_payload, "benchmarkMode")
    ordered_steps = sorted(steps, key=lambda item: item.sequence)
    decision = _decision_from_steps(ordered_steps)
    hypothesis_states = _hypothesis_states_from_steps(ordered_steps)
    observation_decisions = _observation_decisions_from_steps(ordered_steps)
    plan_step_count = _plan_step_count(ordered_steps)
    duration_ms = 0
    if task.completed_at is not None:
        duration_ms = max(0, int((task.completed_at - task.created_at).total_seconds() * 1_000))
    artifact_tools = tuple(
        ArtifactToolCall(
            name=item.tool_name,
            status=item.status,
            risk_tier=_risk_tier(item.tool_name),
        )
        for item in tool_calls
    )
    return RunArtifact(
        scenario_id=scenario_id,
        mode=mode,
        completed=task.status in {"succeeded", "failed"},
        report_produced=bool(reports),
        decision=decision,
        evidence=tuple(_artifact_evidence(item) for item in evidence),
        hypothesis_states=hypothesis_states,
        observation_decisions=observation_decisions,
        tool_calls=artifact_tools,
        plan_step_count=plan_step_count,
        duration_ms=duration_ms,
        safety_events=(),
    )


def _artifact_evidence(record: DiagnosticEvidenceRecord) -> ArtifactEvidence:
    claim_id = record.id
    output = record.payload.get("output")
    if isinstance(output, Mapping):
        value = cast(Mapping[object, object], output).get("benchmarkEvidenceId")
        if isinstance(value, str) and value:
            claim_id = value
    return ArtifactEvidence(
        record_id=record.id,
        claim_id=claim_id,
        grounded=record.step_id is not None and bool(record.source),
    )


def _decision_from_steps(steps: Sequence[DiagnosticStepRecord]) -> RootCauseDecision | None:
    for step in reversed(steps):
        if step.phase != "decision":
            continue
        payload = step.payload.get("rootCauseDecision")
        if not isinstance(payload, Mapping):
            return None
        decision = cast(Mapping[str, object], payload)
        component = decision.get("component")
        mechanism = decision.get("mechanism")
        trigger = decision.get("trigger")
        causal_chain = decision.get("causalChain")
        evidence_ids = decision.get("evidenceIds")
        confidence = decision.get("confidence")
        causal_chain_items = _string_list(causal_chain)
        evidence_id_items = _string_list(evidence_ids)
        if (
            isinstance(component, str)
            and isinstance(mechanism, str)
            and isinstance(trigger, str)
            and causal_chain_items is not None
            and evidence_id_items is not None
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
        ):
            return RootCauseDecision(
                component=component,
                mechanism=mechanism,
                trigger=trigger,
                causal_chain=tuple(causal_chain_items),
                evidence_ids=tuple(evidence_id_items),
                confidence=float(confidence),
            )
    return None


def _hypothesis_states_from_steps(
    steps: Sequence[DiagnosticStepRecord],
) -> tuple[HypothesisState, ...]:
    for step in reversed(steps):
        raw_states = step.payload.get("hypothesisStates")
        if step.phase != "evidence_evaluation" or not isinstance(raw_states, list):
            continue
        states: list[HypothesisState] = []
        for raw in cast(list[object], raw_states):
            if not isinstance(raw, Mapping):
                continue
            item = cast(Mapping[str, object], raw)
            identifier = item.get("id")
            status = item.get("status")
            confidence = item.get("confidence")
            evidence_ids = item.get("evidenceIds")
            evidence_id_items = _string_list(evidence_ids)
            if (
                isinstance(identifier, str)
                and status in {"open", "supported", "refuted"}
                and isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and evidence_id_items is not None
            ):
                states.append(
                    HypothesisState(
                        id=identifier,
                        status=cast(Literal["open", "supported", "refuted"], status),
                        confidence=float(confidence),
                        evidence_ids=tuple(evidence_id_items),
                    )
                )
        return tuple(states)
    return ()


def _observation_decisions_from_steps(
    steps: Sequence[DiagnosticStepRecord],
) -> tuple[ObservationDecision, ...]:
    decisions: list[ObservationDecision] = []
    for step in steps:
        raw = step.payload.get("observationDecision")
        if step.phase != "evidence_evaluation" or not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        purpose = item.get("purpose")
        supports = item.get("supports")
        refutes = item.get("refutes")
        summary = item.get("summary")
        support_items = _string_list(supports)
        refute_items = _string_list(refutes)
        if (
            isinstance(purpose, str)
            and support_items is not None
            and refute_items is not None
            and isinstance(summary, str)
        ):
            decisions.append(
                ObservationDecision(
                    purpose=purpose,
                    supports=tuple(support_items),
                    refutes=tuple(refute_items),
                    summary=summary,
                )
            )
    return tuple(decisions)


def _plan_step_count(steps: Sequence[DiagnosticStepRecord]) -> int:
    for step in steps:
        if step.phase == "planner":
            plan = step.payload.get("plan")
            return len(cast(list[object], plan)) if isinstance(plan, list) else 0
    return 0


def _required_task_text(payload: JsonDict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Diagnostic task is missing benchmark field: {key}.")
    return value


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        return None
    return [cast(str, item) for item in items]


def _risk_tier(tool_name: str) -> Literal["L0", "L1", "L2", "L3"]:
    if tool_name in {
        "RestartTestService",
        "ResumeTestConsumer",
        "DeleteRebuildableTestCacheKey",
        "RestoreTestRedisService",
        "RemoveInjectedNetworkFault",
        "RestoreInjectedServiceState",
    }:
        return "L1"
    if tool_name.startswith("Propose"):
        return "L2"
    if tool_name in {
        "knowledge_retrieval",
        "SearchKnowledge",
        "GetActiveAlerts",
        "SearchLogs",
        "SearchLog",
        "QueryMetrics",
        "QueryTrace",
        "InspectContainer",
        "InspectNginx",
        "InspectPostgres",
        "InspectRedis",
        "GetServiceTopology",
        "GetDeploymentChanges",
        "VerifyServiceHealth",
    }:
        return "L0"
    return "L3"
