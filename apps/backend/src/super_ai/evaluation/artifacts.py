"""Convert persisted diagnostic records into a deterministic scoreable artifact."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.aiops.adjudication import AssessmentSource, Disposition
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
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactToolCall:
    name: str
    status: str
    risk_tier: Literal["L0", "L1", "L2", "L3"]
    approved: bool = False
    verified: bool = False
    arguments: JsonDict = field(default_factory=_empty_json_dict)
    audit_id: str | None = None


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
class ValidationAudit:
    model: str | None
    origin: str | None
    error_category: str | None
    error_codes: tuple[str, ...]
    error_phase: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class ArtifactHypothesisAssessment:
    id: str
    disposition: Disposition
    evidence_ids: tuple[str, ...]
    reason_code: str | None
    assessment_source: AssessmentSource | None


@dataclass(frozen=True, slots=True)
class ModelCallAudit:
    role: str
    attempt: int
    duration_ms: int
    cache_hit: bool
    safe_error_code: str | None


@dataclass(frozen=True, slots=True)
class ValidatorRoutingAudit:
    required: bool
    skipped: bool
    reason_codes: tuple[str, ...]
    skip_reason: str | None


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
    validation_audit: ValidationAudit | None = None
    workflow_version: str | None = None
    graph_version: str | None = None
    hypothesis_assessments: tuple[ArtifactHypothesisAssessment, ...] = ()
    artifact_valid: bool = True
    artifact_errors: tuple[str, ...] = ()
    model_call_count: int = 0
    model_call_audits: tuple[ModelCallAudit, ...] = ()
    validator_routing: ValidatorRoutingAudit | None = None
    resume_count: int = 0


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
    workflow_version = _workflow_version(ordered_steps)
    graph_version = _graph_version(ordered_steps, workflow_version=workflow_version)
    decision = _decision_from_steps(ordered_steps)
    hypothesis_states = _hypothesis_states_from_steps(ordered_steps)
    hypothesis_assessments, artifact_errors = _hypothesis_assessments_from_steps(
        ordered_steps,
        workflow_version=workflow_version,
        legacy_states=hypothesis_states,
    )
    model_call_count, model_call_audits = _model_call_observability(ordered_steps)
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
            audit_id=item.id,
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
        validation_audit=_validation_audit_from_steps(ordered_steps),
        workflow_version=workflow_version,
        graph_version=graph_version,
        hypothesis_assessments=hypothesis_assessments,
        artifact_valid=not artifact_errors,
        artifact_errors=artifact_errors,
        model_call_count=model_call_count,
        model_call_audits=model_call_audits,
        validator_routing=_validator_routing_from_steps(ordered_steps),
        resume_count=sum(1 for step in ordered_steps if step.phase == "execution_resume"),
    )


def _workflow_version(steps: Sequence[DiagnosticStepRecord]) -> str | None:
    versions = [
        value
        for step in steps
        if isinstance((value := step.payload.get("workflowVersion")), str)
    ]
    return versions[-1] if versions else None


def _graph_version(
    steps: Sequence[DiagnosticStepRecord], *, workflow_version: str | None
) -> str | None:
    versions = [
        value
        for step in steps
        if isinstance((value := step.payload.get("graphVersion")), str)
        and value in {"aiops-diagnostic-v2", "aiops-diagnostic-v3"}
    ]
    if versions:
        return versions[-1]
    return "aiops-diagnostic-v2" if workflow_version == "evidence-driven-v4" else None


def _hypothesis_assessments_from_steps(
    steps: Sequence[DiagnosticStepRecord],
    *,
    workflow_version: str | None,
    legacy_states: Sequence[HypothesisState],
) -> tuple[tuple[ArtifactHypothesisAssessment, ...], tuple[str, ...]]:
    if workflow_version != "evidence-driven-v4":
        legacy_dispositions = {
            "open": "unresolved",
            "supported": "supported",
            "refuted": "refuted",
        }
        return (
            tuple(
                ArtifactHypothesisAssessment(
                    id=item.id,
                    disposition=cast(Disposition, legacy_dispositions[item.status]),
                    evidence_ids=item.evidence_ids,
                    reason_code=None,
                    assessment_source=None,
                )
                for item in legacy_states
            ),
            (),
        )

    raw_assessments: object | None = None
    for step in reversed(steps):
        candidate = step.payload.get("hypothesisAssessments")
        if candidate is not None:
            raw_assessments = candidate
            break
    if not isinstance(raw_assessments, list):
        return (), ("missing_hypothesis_assessments",)

    assessments: list[ArtifactHypothesisAssessment] = []
    errors: list[str] = []
    allowed_dispositions = {
        "supported",
        "refuted",
        "causally_inactive",
        "unresolved",
    }
    allowed_sources = {"deterministic", "llm_adjudicated"}
    for raw in cast(list[object], raw_assessments):
        if not isinstance(raw, Mapping):
            errors.append("invalid_hypothesis_assessment")
            continue
        item = cast(Mapping[str, object], raw)
        identifier = item.get("id") or item.get("hypothesisId")
        disposition = item.get("disposition")
        evidence_ids = _string_list(item.get("evidenceIds"))
        reason_code = item.get("reasonCode")
        source = item.get("assessmentSource")
        if disposition not in allowed_dispositions:
            errors.append("invalid_hypothesis_disposition")
            continue
        if (
            not isinstance(identifier, str)
            or not identifier
            or evidence_ids is None
            or not isinstance(reason_code, str)
            or not reason_code
            or source not in allowed_sources
        ):
            errors.append("invalid_hypothesis_assessment")
            continue
        assessments.append(
            ArtifactHypothesisAssessment(
                id=identifier,
                disposition=cast(Disposition, disposition),
                evidence_ids=tuple(evidence_ids),
                reason_code=reason_code,
                assessment_source=cast(AssessmentSource, source),
            )
        )
    return tuple(assessments), tuple(dict.fromkeys(errors))


_MODEL_CALL_ROLES = frozenset(
    {"planner", "adjudicator", "replanner", "validator", "report"}
)


def _model_call_observability(
    steps: Sequence[DiagnosticStepRecord],
) -> tuple[int, tuple[ModelCallAudit, ...]]:
    count = 0
    audits: list[ModelCallAudit] = []
    seen: set[tuple[object, ...]] = set()
    for step in steps:
        count_value = step.payload.get("modelCallCount")
        if (
            isinstance(count_value, int)
            and not isinstance(count_value, bool)
            and 0 <= count_value <= 8
        ):
            count = max(count, count_value)
        raw_audits = step.payload.get("modelCallAudits")
        if not isinstance(raw_audits, list):
            continue
        for raw in cast(list[object], raw_audits):
            if not isinstance(raw, Mapping):
                continue
            item = cast(Mapping[str, object], raw)
            role = item.get("role")
            attempt = item.get("attempt")
            duration_ms = item.get("durationMs")
            cache_hit = item.get("cacheHit")
            error_code = item.get("safeErrorCode")
            if (
                role not in _MODEL_CALL_ROLES
                or not isinstance(attempt, int)
                or isinstance(attempt, bool)
                or not 1 <= attempt <= 8
                or not isinstance(duration_ms, int)
                or isinstance(duration_ms, bool)
                or duration_ms < 0
                or not isinstance(cache_hit, bool)
                or (error_code is not None and not isinstance(error_code, str))
            ):
                continue
            identity = (role, attempt, duration_ms, cache_hit, error_code)
            if identity in seen:
                continue
            seen.add(identity)
            audits.append(
                ModelCallAudit(
                    role=cast(str, role),
                    attempt=attempt,
                    duration_ms=duration_ms,
                    cache_hit=cache_hit,
                    safe_error_code=error_code,
                )
            )
    return count, tuple(audits)


def _validator_routing_from_steps(
    steps: Sequence[DiagnosticStepRecord],
) -> ValidatorRoutingAudit | None:
    routing = next(
        (step for step in reversed(steps) if step.phase == "validator_router"),
        None,
    )
    if routing is None:
        return None
    raw_codes = routing.payload.get("validationReasonCodes")
    reason_codes = (
        tuple(
            item
            for item in cast(list[object], raw_codes)
            if isinstance(item, str)
        )[:6]
        if isinstance(raw_codes, list)
        else ()
    )
    skip_reason = routing.payload.get("validationSkipReason")
    return ValidatorRoutingAudit(
        required=routing.payload.get("validationRequired") is True,
        skipped=routing.payload.get("validationSkipped") is True,
        reason_codes=reason_codes,
        skip_reason=skip_reason if isinstance(skip_reason, str) else None,
    )


_VALIDATION_ORIGINS = frozenset(
    {"none", "llm_confirmed", "deterministic_grounded_fallback"}
)
_VALIDATION_ERROR_CATEGORIES = frozenset(
    {
        "candidate_missing",
        "deterministic_gap",
        "model_call_failed",
        "invalid_model_output",
        "model_rejected",
        "retry_exhausted",
    }
)
_VALIDATION_ERROR_PHASES = frozenset(
    {"structured_invoker_setup", "model_invoke", "structured_parse"}
)
_VALIDATION_ERROR_CODES = frozenset(
    {
        "timeout",
        "connection",
        "authentication",
        "permission_denied",
        "rate_limit",
        "provider_4xx",
        "provider_5xx",
        "structured_output_unsupported",
        "unknown",
        "invalid_json",
        "structured_envelope_mismatch",
        "missing_required_field",
        "invalid_enum",
        "wrong_container_type",
        "extra_field",
        "unknown_evidence_id",
        "invalid_json_or_schema",
    }
)
_SAFE_MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _validation_audit_from_steps(
    steps: Sequence[DiagnosticStepRecord],
) -> ValidationAudit | None:
    validation = next(
        (step for step in reversed(steps) if step.phase == "decision_validation"),
        None,
    )
    if validation is None:
        return None
    payload = validation.payload
    model_value = payload.get("validationModel")
    model = (
        model_value
        if isinstance(model_value, str) and _SAFE_MODEL_NAME.fullmatch(model_value)
        else None
    )
    origin = _allowlisted_text(payload.get("validationOrigin"), _VALIDATION_ORIGINS)
    error_category = _allowlisted_text(
        payload.get("validationErrorCategory"), _VALIDATION_ERROR_CATEGORIES
    )
    error_phase = _allowlisted_text(
        payload.get("validationErrorPhase"), _VALIDATION_ERROR_PHASES
    )
    raw_codes = payload.get("validationErrorCodes")
    codes: list[str] = []
    if isinstance(raw_codes, list):
        for item in cast(list[object], raw_codes):
            if isinstance(item, str) and item in _VALIDATION_ERROR_CODES and item not in codes:
                codes.append(item)
            if len(codes) == 6:
                break
    attempts_value = payload.get("validationAttempts")
    attempts = (
        attempts_value
        if isinstance(attempts_value, int)
        and not isinstance(attempts_value, bool)
        and 0 <= attempts_value <= 2
        else 0
    )
    return ValidationAudit(
        model=model,
        origin=origin,
        error_category=error_category,
        error_codes=tuple(codes),
        error_phase=error_phase,
        attempts=attempts,
    )


def _allowlisted_text(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


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
        tool_call_id=record.tool_call_id,
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
    adjudicator_projection = next(
        (
            step
            for step in reversed(steps)
            if step.phase == "hypothesis_adjudicator"
            and isinstance(step.payload.get("observationDecisions"), list)
        ),
        None,
    )
    for step in steps:
        raw_items: list[Mapping[str, object]] = []
        legacy = step.payload.get("observationDecision")
        if step.phase == "evidence_evaluation" and isinstance(legacy, Mapping):
            raw_items.append(cast(Mapping[str, object], legacy))
        v4_items = step.payload.get("observationDecisions")
        use_v4_items = (
            step is adjudicator_projection
            if adjudicator_projection is not None
            else step.phase == "fact_adapter"
        )
        if use_v4_items and isinstance(v4_items, list):
            raw_items.extend(
                cast(Mapping[str, object], item)
                for item in cast(list[object], v4_items)
                if isinstance(item, Mapping)
            )
        if not raw_items:
            continue
        for item in raw_items:
            purpose = item.get("purpose")
            supports = item.get("supports")
            refutes = item.get("refutes")
            summary = item.get("summary")
            causal_role = item.get("causalRole", "context")
            causal_role_origin = item.get("causalRoleOrigin")
            reported_causal_role = item.get("reportedCausalRole")
            causal_role_corrected = item.get("causalRoleCorrected", False)
            raw_evidence_ids = item.get("evidenceIds")
            evidence_ids = (
                [] if raw_evidence_ids is None else _string_list(raw_evidence_ids)
            )
            support_items = _string_list(supports)
            refute_items = _string_list(refutes)
            if (
                isinstance(purpose, str)
                and support_items is not None
                and refute_items is not None
                and isinstance(summary, str)
                and evidence_ids is not None
                and causal_role in {"trigger", "mechanism", "impact", "context"}
                and causal_role_origin
                in {
                    None,
                    "model",
                    "plan_contract",
                    "trusted_evidence_rule",
                    "coverage_repair",
                }
                and reported_causal_role
                in {None, "trigger", "mechanism", "impact", "context"}
                and isinstance(causal_role_corrected, bool)
            ):
                decisions.append(
                    ObservationDecision(
                        purpose=purpose,
                        supports=tuple(support_items),
                        refutes=tuple(refute_items),
                        summary=summary,
                        evidence_ids=tuple(evidence_ids),
                        causal_role=cast(
                            Literal["trigger", "mechanism", "impact", "context"],
                            causal_role,
                        ),
                        causal_role_origin=cast(
                            Literal[
                                "model",
                                "plan_contract",
                                "trusted_evidence_rule",
                                "coverage_repair",
                            ]
                            | None,
                            causal_role_origin,
                        ),
                        reported_causal_role=cast(
                            Literal["trigger", "mechanism", "impact", "context"]
                            | None,
                            reported_causal_role,
                        ),
                        causal_role_corrected=causal_role_corrected,
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


ToolObservationRole = Literal[
    "diagnostic_observation",
    "knowledge_context",
    "recovery_or_verification",
    "unknown",
]

_L1_RECOVERY_TOOLS = frozenset(
    {
        "RestartTestService",
        "ResumeTestConsumer",
        "DeleteRebuildableTestCacheKey",
        "RestoreTestRedisService",
        "RemoveInjectedNetworkFault",
        "RestoreInjectedServiceState",
    }
)
_KNOWLEDGE_CONTEXT_TOOLS = frozenset(
    {"knowledge_retrieval", "SearchKnowledge", "GetActiveAlerts"}
)
_RECOVERY_OR_VERIFICATION_TOOLS = _L1_RECOVERY_TOOLS | {"VerifyServiceHealth"}
_DIAGNOSTIC_OBSERVATION_TOOLS = frozenset(
    {
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
    }
)


def tool_observation_role(tool: ArtifactToolCall) -> ToolObservationRole:
    """Classify whether a completed tool call requires Evidence Evaluation."""
    if tool.name in _KNOWLEDGE_CONTEXT_TOOLS:
        return "knowledge_context"
    if tool.name in _RECOVERY_OR_VERIFICATION_TOOLS or tool.name.startswith("Propose"):
        return "recovery_or_verification"
    if tool.name in _DIAGNOSTIC_OBSERVATION_TOOLS:
        return "diagnostic_observation"
    return "unknown"


def _risk_tier(tool_name: str) -> Literal["L0", "L1", "L2", "L3"]:
    if tool_name in _L1_RECOVERY_TOOLS:
        return "L1"
    if tool_name.startswith("Propose"):
        return "L2"
    if tool_name in (
        _KNOWLEDGE_CONTEXT_TOOLS
        | _DIAGNOSTIC_OBSERVATION_TOOLS
        | _RECOVERY_OR_VERIFICATION_TOOLS
    ):
        return "L0"
    return "L3"
