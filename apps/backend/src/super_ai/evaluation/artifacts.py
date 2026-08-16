"""Convert persisted diagnostic records into a deterministic scoreable artifact."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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


def _empty_json_dict() -> JsonDict:
    return {}


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    record_id: str
    claim_id: str
    grounded: bool
    source: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactToolCall:
    name: str
    status: str
    risk_tier: Literal["L0", "L1", "L2", "L3"]
    approved: bool = False
    verified: bool = False
    arguments: JsonDict = field(default_factory=_empty_json_dict)


@dataclass(frozen=True, slots=True)
class LiveEvidenceAudit:
    """Trusted non-secret identity and readiness scope for Live evidence."""

    source: str
    region: str | None = None
    topic_id: str | None = None
    from_ms: int | None = None
    to_ms: int | None = None
    run_id: str | None = None
    scenario_id: str | None = None
    incident_id: str | None = None
    expected_log_count: int | None = None
    indexed_log_count: int | None = None
    attempts: int | None = None


@dataclass(frozen=True, slots=True)
class LiveRecoveryAudit:
    action: str
    target_ref: str
    approved: bool
    executed: bool
    verified: bool
    authorization_code: str


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
    diagnostic_task_id: str | None = None
    live_recovery: LiveRecoveryAudit | None = None
    live_evidence: LiveEvidenceAudit | None = None


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
            arguments=dict(item.arguments),
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
        diagnostic_task_id=task.id,
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
        source=record.source,
    )


def _decision_from_steps(steps: Sequence[DiagnosticStepRecord]) -> RootCauseDecision | None:
    decision_index = next(
        (index for index in range(len(steps) - 1, -1, -1) if steps[index].phase == "decision"),
        None,
    )
    if decision_index is None:
        return None
    workflow_versions = [
        step.payload.get("workflowVersion")
        for step in steps
        if step.phase == "planner"
    ]
    workflow_version = workflow_versions[-1] if workflow_versions else None
    if workflow_version in {"evidence-driven-v2", "evidence-driven-v3"}:
        validations = [
            step
            for step in steps[decision_index + 1 :]
            if step.phase == "decision_validation"
        ]
        if not validations or validations[-1].payload.get("status") != "valid":
            return None
        if workflow_version == "evidence-driven-v3" and validations[-1].payload.get(
            "validationOrigin"
        ) not in {"llm_confirmed", "deterministic_grounded_fallback"}:
            return None

    payload = steps[decision_index].payload.get("rootCauseDecision")
    if not isinstance(payload, Mapping):
        return None
    decision = cast(Mapping[str, object], payload)
    component = decision.get("component")
    mechanism = decision.get("mechanism")
    trigger = decision.get("trigger")
    causal_chain_items = _string_list(decision.get("causalChain"))
    evidence_id_items = _string_list(decision.get("evidenceIds"))
    confidence = decision.get("confidence")
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
    return sum(1 for step in steps if step.phase == "executor")


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
        "GetDatabaseMetrics",
        "InspectContainer",
        "GetGatewayMetrics",
        "GetRedisConnectionMetrics",
        "GetServiceMetrics",
        "InspectClientRetryPolicy",
        "InspectDatabasePool",
        "InspectGatewayErrors",
        "InspectGatewayRequestTimeline",
        "InspectHostLimits",
        "InspectHttpAttempts",
        "InspectNginx",
        "InspectPostgres",
        "InspectPostgresErrors",
        "InspectPostgresWaitGraph",
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "InspectRateLimitTimeline",
        "InspectRedis",
        "InspectRedisClientPool",
        "InspectRedisServer",
        "InspectTrafficAndDependencyHealth",
        "InspectTransactionResourceOrder",
        "ListRedisClients",
        "ProbeUpstreamHealth",
        "GetServiceTopology",
        "GetDeploymentChanges",
        "VerifyServiceHealth",
    }:
        return "L0"
    return "L3"
