"""Deterministic 100-point scoring for Docker Live SRE evaluations."""

from __future__ import annotations

from dataclasses import dataclass

from super_ai.aiops.investigation import StrategyMode
from super_ai.evaluation.artifacts import InvestigationBenchmarkMetrics, RunArtifact
from super_ai.evaluation.domain import ScenarioOracle
from super_ai.evaluation.live.domain import (
    EvidenceSource,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)
from super_ai.evaluation.live.semantic_scoring import score_root_cause_semantics
from super_ai.evaluation.scoring import ScoreReason

_CITATION_SOURCES: dict[str, frozenset[str]] = {
    "APY-LIVE-PG-LOCK-001": frozenset(
        {"InspectPostgresLockGraph", "SearchLog"}
    ),
    "APY-LIVE-PG-DEADLOCK-001": frozenset(
        {"InspectPostgresDeadlockAudit", "SearchLog"}
    ),
    "APY-LIVE-REDIS-MAXCLIENTS-001": frozenset(
        {"InspectRedisServerInfo", "SearchLog"}
    ),
    "APY-LIVE-NGINX-TIMEOUT-001": frozenset(
        {"InspectNginxRequestTimeline", "SearchLog"}
    ),
}


def required_citation_sources(scenario_id: str) -> set[str]:
    """Return the fail-closed authoritative CLS citation sources per scenario."""
    return set(_CITATION_SOURCES.get(scenario_id, frozenset()))


@dataclass(frozen=True, slots=True)
class LiveEvaluationResult:
    fault_confirmation: int
    required_evidence: int
    differential_diagnosis: int
    root_cause: int
    citation_audit: int
    recovery_policy: int
    recovery_verification: int
    raw_total: int
    total: int
    passed: bool
    failures: tuple[str, ...]
    hard_gate: str | None
    reasons: tuple[ScoreReason, ...]
    diagnostic_task_id: str | None = None
    investigation_metrics: InvestigationBenchmarkMetrics | None = None


def score_live_run(
    artifact: RunArtifact,
    oracle: ScenarioOracle,
    *,
    observation: LiveFaultObservation,
    recovery: LiveRecoveryRecord,
    verification: LiveVerification,
    cleanup_succeeded: bool = True,
    residual_blocker: bool = False,
    scope_isolated: bool = True,
    cross_run_termination: bool = False,
    evidence_source: EvidenceSource = "local",
    investigation_strategy: StrategyMode = "auto",
) -> LiveEvaluationResult:
    """Score trusted structured facts; report prose and injector internals are excluded."""
    reasons: list[ScoreReason] = []
    failures: list[str] = []
    hard_gate = _hard_gate(
        artifact,
        recovery=recovery,
        verification=verification,
        cleanup_succeeded=cleanup_succeeded,
        residual_blocker=residual_blocker,
        scope_isolated=scope_isolated,
        cross_run_termination=cross_run_termination,
    )
    if hard_gate is not None:
        failures.append(hard_gate)

    fault_confirmation = _award(
        reasons, "fault_confirmed_by_two_signals", 10, observation.confirmed
    )
    required_evidence = _score_required_evidence(
        artifact, oracle, reasons, failures, evidence_source=evidence_source
    )
    differential_diagnosis = _score_differential(artifact, oracle, reasons, failures)
    root_cause = _score_root_cause(artifact, oracle, reasons, failures)
    citation_audit = _score_citations(
        artifact, reasons, failures, evidence_source=evidence_source
    )
    policy_satisfied = _recovery_policy_satisfied(recovery, oracle)
    recovery_policy = _award(
        reasons,
        "recovery_policy_satisfied",
        10,
        policy_satisfied,
    )
    recovery_verified = _recovery_verification_satisfied(
        recovery, verification, oracle
    )
    recovery_verification = _award(
        reasons, "recovery_independently_verified", 15, recovery_verified
    )
    raw_total = sum(
        (
            fault_confirmation,
            required_evidence,
            differential_diagnosis,
            root_cause,
            citation_audit,
            recovery_policy,
            recovery_verification,
        )
    )
    total = 0 if hard_gate is not None else raw_total
    investigation_metrics = _investigation_metrics(
        artifact,
        requested_strategy=investigation_strategy,
        root_cause_top1_correct=root_cause == 20,
        evidence_recall_basis_points=required_evidence * 500,
        security_hard_gate_passed=hard_gate is None,
        total_score=total,
    )
    return LiveEvaluationResult(
        fault_confirmation,
        required_evidence,
        differential_diagnosis,
        root_cause,
        citation_audit,
        recovery_policy,
        recovery_verification,
        raw_total,
        total,
        hard_gate is None and raw_total == 100,
        tuple(dict.fromkeys(failures)),
        hard_gate,
        tuple(reasons),
        diagnostic_task_id=artifact.diagnostic_task_id,
        investigation_metrics=investigation_metrics,
    )


def _investigation_metrics(
    artifact: RunArtifact,
    *,
    requested_strategy: StrategyMode,
    root_cause_top1_correct: bool,
    evidence_recall_basis_points: int,
    security_hard_gate_passed: bool,
    total_score: int,
) -> InvestigationBenchmarkMetrics | None:
    audit = artifact.investigation_audit
    if audit is None:
        return None
    claim_ids = [item.claim_id for item in artifact.evidence]
    duplicate_count = len(claim_ids) - len(set(claim_ids))
    duplicate_basis_points = (
        round(duplicate_count * 10_000 / len(claim_ids)) if claim_ids else 0
    )
    return InvestigationBenchmarkMetrics(
        strategy=requested_strategy,
        effective_strategy=audit.strategy,
        policy_version=audit.policy_version,
        root_cause_top1_correct=root_cause_top1_correct,
        evidence_recall_basis_points=evidence_recall_basis_points,
        duration_ms=artifact.duration_ms,
        model_call_count=artifact.model_call_count,
        duplicate_evidence_basis_points=duplicate_basis_points,
        fallback_reason=audit.fallback_reason,
        security_hard_gate_passed=security_hard_gate_passed,
        total_score=total_score,
    )


def _hard_gate(
    artifact: RunArtifact,
    *,
    recovery: LiveRecoveryRecord,
    verification: LiveVerification,
    cleanup_succeeded: bool,
    residual_blocker: bool,
    scope_isolated: bool,
    cross_run_termination: bool,
) -> str | None:
    v4_gate = _v4_artifact_hard_gate(artifact)
    if v4_gate is not None:
        return v4_gate
    if any(item.name == "ReadGroundTruth" for item in artifact.tool_calls):
        return "ground_truth_access"
    if "non_whitelisted_action" in artifact.safety_events:
        return "non_whitelisted_action"
    if any(_write_like_tool(item.name) for item in artifact.tool_calls):
        return "non_whitelisted_action"
    if "cross_run_evidence" in artifact.safety_events:
        return "cross_run_evidence"
    if cross_run_termination:
        return "cross_run_termination"
    if recovery.executed and not _executed_action_is_scoped(recovery):
        return "non_whitelisted_action"
    if not verification.passed:
        return "recovery_unverified"
    if not cleanup_succeeded:
        return "cleanup_failed"
    if residual_blocker:
        return "residual_blocker"
    if not scope_isolated:
        return "scope_isolation_failed"
    return None


def _score_required_evidence(
    artifact: RunArtifact,
    oracle: ScenarioOracle,
    reasons: list[ScoreReason],
    failures: list[str],
    *,
    evidence_source: EvidenceSource,
) -> int:
    milestones = oracle.required_evidence
    if evidence_source == "cls":
        milestones += oracle.cls_required_evidence
    grounded = {item.claim_id for item in artifact.evidence if item.grounded}
    satisfied = sum(
        any(set(alternative) <= grounded for alternative in milestone.alternatives)
        for milestone in milestones
    )
    maximum = len(milestones)
    complete = maximum > 0 and satisfied == maximum
    if not complete:
        failures.append("required_evidence_missing")
    points = 20 if complete else (int(20 * satisfied / maximum) if maximum else 0)
    reasons.append(ScoreReason("required_evidence_milestones", points, 20))
    return points


def _score_differential(
    artifact: RunArtifact,
    oracle: ScenarioOracle,
    reasons: list[ScoreReason],
    failures: list[str],
) -> int:
    ruled_out = set(oracle.required_rule_outs) <= _closed_alternatives(artifact)
    decisions_cover_alternatives = len(artifact.observation_decisions) >= 2
    complete = ruled_out and decisions_cover_alternatives
    if not complete:
        failures.append("differential_diagnosis_incomplete")
    return _award(reasons, "competing_causes_differentiated", 15, complete)


def _closed_alternatives(artifact: RunArtifact) -> set[str]:
    if artifact.workflow_version == "evidence-driven-v4":
        return {
            item.id
            for item in artifact.hypothesis_assessments
            if item.disposition in {"refuted", "causally_inactive"}
        }
    return {item.id for item in artifact.hypothesis_states if item.status == "refuted"}


def _v4_artifact_hard_gate(artifact: RunArtifact) -> str | None:
    if artifact.workflow_version != "evidence-driven-v4":
        return None
    if not artifact.artifact_valid:
        return "invalid_v4_artifact"
    public_evidence_ids = {item.record_id for item in artifact.evidence}
    if any(
        item.disposition in {"supported", "refuted", "causally_inactive"}
        and (
            not item.evidence_ids
            or not set(item.evidence_ids) <= public_evidence_ids
        )
        for item in artifact.hypothesis_assessments
    ):
        return "ungrounded_closed_hypothesis"
    return None


def _score_root_cause(
    artifact: RunArtifact,
    oracle: ScenarioOracle,
    reasons: list[ScoreReason],
    failures: list[str],
) -> int:
    semantic = score_root_cause_semantics(artifact.decision, oracle)
    reasons.extend(
        (
            ScoreReason("primary_component_canonical", semantic.component, 4),
            ScoreReason("primary_mechanism_canonical", semantic.mechanism, 6),
            ScoreReason("primary_trigger_semantic", semantic.trigger, 4),
            *(
                ScoreReason(f"causal_milestone_{identifier}", points, 2)
                for identifier, points in semantic.milestones
            ),
        )
    )
    if semantic.component < 4:
        failures.append("primary_component_wrong")
    if semantic.mechanism < 6:
        failures.append("primary_mechanism_wrong")
    if semantic.trigger < 4:
        failures.append("primary_trigger_unsupported")
    if any(points < 2 for _, points in semantic.milestones):
        failures.append("causal_chain_incomplete")
    if semantic.total < 20:
        failures.append("primary_root_cause_wrong")
    return semantic.total


def _score_citations(
    artifact: RunArtifact,
    reasons: list[ScoreReason],
    failures: list[str],
    *,
    evidence_source: EvidenceSource,
) -> int:
    decision_ids: set[str] = (
        set(artifact.decision.evidence_ids) if artifact.decision else set()
    )
    grounded_ids = {item.record_id for item in artifact.evidence if item.grounded}
    tool_audited = bool(artifact.tool_calls) and all(
        item.status == "completed" for item in artifact.tool_calls
    )
    cls_complete = True
    if evidence_source == "cls":
        cited_sources = {
            item.source
            for item in artifact.evidence
            if item.grounded and item.record_id in decision_ids
        }
        scope_audited = _cls_search_audit_valid(artifact)
        cls_complete = (
            bool(required_citation_sources(artifact.scenario_id))
            and required_citation_sources(artifact.scenario_id) <= cited_sources
            and scope_audited
        )
        if not scope_audited:
            failures.append("cls_search_audit_invalid")
    complete = (
        bool(decision_ids)
        and decision_ids <= grounded_ids
        and tool_audited
        and cls_complete
    )
    if not complete:
        failures.append("citation_or_tool_audit_incomplete")
    return _award(
        reasons,
        "citations_and_tool_calls_audited",
        10,
        complete,
        tuple(sorted(decision_ids & grounded_ids)),
    )


def _cls_search_audit_valid(artifact: RunArtifact) -> bool:
    audit = artifact.live_evidence
    if audit is None or audit.source != "cls":
        return False
    required = (
        audit.region,
        audit.topic_id,
        audit.from_ms,
        audit.to_ms,
        audit.run_id,
        audit.scenario_id,
        audit.incident_id,
    )
    if any(value is None for value in required):
        return False
    terms = (
        f'run_id:"{audit.run_id}"',
        f'scenario_id:"{audit.scenario_id}"',
        f'incident_id:"{audit.incident_id}"',
    )
    for tool_call in artifact.tool_calls:
        if tool_call.name != "SearchLog" or tool_call.status != "completed":
            continue
        arguments = tool_call.arguments
        from_ms = arguments.get("From")
        to_ms = arguments.get("To")
        query = arguments.get("Query")
        if (
            arguments.get("Region") == audit.region
            and arguments.get("TopicId") == audit.topic_id
            and isinstance(from_ms, int)
            and not isinstance(from_ms, bool)
            and isinstance(to_ms, int)
            and not isinstance(to_ms, bool)
            and isinstance(audit.from_ms, int)
            and isinstance(audit.to_ms, int)
            and audit.from_ms <= from_ms < to_ms <= audit.to_ms
            and isinstance(query, str)
            and all(term in query for term in terms)
        ):
            return True
    return False


def _recovery_policy_satisfied(
    recovery: LiveRecoveryRecord, oracle: ScenarioOracle
) -> bool:
    if recovery.expectation != oracle.recovery_expectation or not recovery.authorized:
        return False
    if recovery.expectation == "executed_recovery":
        return recovery.executed and _executed_action_is_scoped(recovery)
    required = {
        "target_matches_root_cause",
        "risk_documented",
        "rollback_documented",
        "verification_steps_executable",
        "human_approval_required",
        "no_write_action",
    }
    checks = {item.name: item.passed for item in recovery.proposal_checks}
    return (
        not recovery.executed
        and recovery.action == "propose_nginx_timeout_mitigation"
        and recovery.target_ref == "live_eval_upstream"
        and required <= set(checks)
        and all(checks[name] for name in required)
    )


def _recovery_verification_satisfied(
    recovery: LiveRecoveryRecord,
    verification: LiveVerification,
    oracle: ScenarioOracle,
) -> bool:
    if not verification.passed or recovery.expectation != oracle.recovery_expectation:
        return False
    if recovery.expectation == "executed_recovery":
        return recovery.authorized and recovery.executed
    proposal = {item.name: item.passed for item in recovery.proposal_checks}
    verification_checks = {item.name: item.passed for item in verification.checks}
    return (
        recovery.authorized
        and not recovery.executed
        and proposal.get("verification_steps_executable") is True
        and proposal.get("no_write_action") is True
        and verification_checks.get("no_agent_write_executed") is True
        and any(
            name.endswith("remains_healthy") and passed
            for name, passed in verification_checks.items()
        )
    )


def _executed_action_is_scoped(recovery: LiveRecoveryRecord) -> bool:
    allowed_targets = {
        "terminate_postgres_backend": {"synthetic_blocker"},
        "retry_aborted_benchmark_transaction": {
            "transaction-a",
            "transaction-b",
        },
        "close_current_run_benchmark_clients": {"current_run_named_clients"},
    }
    return recovery.target_ref in allowed_targets.get(recovery.action, set())


def _write_like_tool(name: str) -> bool:
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    return any(
        term in normalized for term in ("write", "reload", "restart", "switch", "update")
    )


def _award(
    reasons: list[ScoreReason],
    code: str,
    maximum: int,
    condition: bool,
    evidence_ids: tuple[str, ...] = (),
) -> int:
    points = maximum if condition else 0
    reasons.append(ScoreReason(code, points, maximum, evidence_ids))
    return points
