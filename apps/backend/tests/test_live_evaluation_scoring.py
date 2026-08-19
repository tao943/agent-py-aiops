from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TypedDict

import pytest

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.evaluation import ArtifactEvidence, ArtifactToolCall, RunArtifact
from super_ai.evaluation.artifacts import (
    ArtifactHypothesisAssessment,
    LiveEvidenceAudit,
    LiveRecoveryAudit,
)
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)
from super_ai.evaluation.live.scenarios import load_live_oracle
from super_ai.evaluation.live.scoring import required_citation_sources, score_live_run
from super_ai.evaluation.live.semantic_scoring import score_root_cause_semantics

SCENARIO = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "live"
    / "APY-LIVE-PG-LOCK-001"
)


def passing_artifact() -> RunArtifact:
    oracle = load_live_oracle(SCENARIO)
    return RunArtifact(
        scenario_id="APY-LIVE-PG-LOCK-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            oracle.primary_cause.component,
            oracle.primary_cause.mechanism,
            oracle.primary_cause.trigger,
            oracle.causal_chain,
            ("ev-session", "ev-graph"),
            0.95,
        ),
        evidence=(
            ArtifactEvidence("ev-session", "postgres-wait-event-lock", True),
            ArtifactEvidence("ev-graph", "postgres-blocking-pid-edge", True),
        ),
        hypothesis_states=(
            HypothesisState("postgres_lock_blocking", "supported", 0.95, ("ev-graph",)),
            HypothesisState("postgres_slow_query_without_lock", "refuted", 0.1, ("ev-session",)),
            HypothesisState("postgres_connectivity_failure", "refuted", 0.05, ("ev-session",)),
        ),
        observation_decisions=(
            ObservationDecision(
                "Compare lock and latency",
                ("postgres_lock_blocking",),
                ("postgres_slow_query_without_lock",),
                "lock edge exists",
            ),
            ObservationDecision(
                "Check reachability",
                ("postgres_lock_blocking",),
                ("postgres_connectivity_failure",),
                "database reachable",
            ),
        ),
        tool_calls=(
            ArtifactToolCall("InspectPostgresSessions", "completed", "L0"),
            ArtifactToolCall("InspectPostgresLockGraph", "completed", "L0"),
            ArtifactToolCall("VerifyServiceHealth", "completed", "L0"),
        ),
        plan_step_count=2,
        duration_ms=100,
        safety_events=(),
        diagnostic_task_id="diagnostic-live-1",
        live_recovery=LiveRecoveryAudit(
            "terminate_postgres_backend", "synthetic_blocker", True, True, True, "authorized"
        ),
    )


def passing_cls_artifact() -> RunArtifact:
    artifact = passing_artifact()
    decision = artifact.decision
    assert decision is not None
    query = (
        'run_id:"run-1" AND scenario_id:"APY-LIVE-PG-LOCK-001" '
        'AND incident_id:"APY-LIVE-PG-LOCK-001-run-1"'
    )
    return replace(
        artifact,
        decision=replace(decision, evidence_ids=decision.evidence_ids + ("ev-cls",)),
        evidence=tuple(
            replace(item, source="InspectPostgresSessions")
            if item.record_id == "ev-session"
            else replace(item, source="InspectPostgresLockGraph")
            for item in artifact.evidence
        )
        + (ArtifactEvidence("ev-cls", "cls-live-request-timeout", True, "SearchLog"),),
        tool_calls=artifact.tool_calls
        + (
            ArtifactToolCall(
                "SearchLog",
                "completed",
                "L0",
                arguments={
                    "Region": "ap-guangzhou",
                    "TopicId": "topic-live",
                    "From": 1_000,
                    "To": 10_000,
                    "Query": query,
                    "Limit": 20,
                },
            ),
        ),
        live_evidence=LiveEvidenceAudit(
            source="cls",
            region="ap-guangzhou",
            topic_id="topic-live",
            from_ms=1_000,
            to_ms=10_000,
            run_id="run-1",
            scenario_id="APY-LIVE-PG-LOCK-001",
            incident_id="APY-LIVE-PG-LOCK-001-run-1",
            expected_log_count=3,
            indexed_log_count=3,
            attempts=1,
        ),
    )


OBSERVATION = LiveFaultObservation(
    "APY-LIVE-PG-LOCK-001",
    (LiveCheck("waiter_has_lock_event", True), LiveCheck("blocker_edge_confirmed", True)),
)
NGINX_SCENARIO = SCENARIO.parent / "APY-LIVE-NGINX-TIMEOUT-001"
REDIS_SCENARIO = SCENARIO.parent / "APY-LIVE-REDIS-MAXCLIENTS-001"


def test_redis_public_fact_projection_satisfies_semantic_contract() -> None:
    trigger = (
        "Current-run benchmark clients filled Redis connection capacity, and connected "
        "clients reached the configured maxclients limit of 16."
    )
    decision = RootCauseDecision(
        "live-eval-redis",
        "benchmark_clients_exhausted_maxclients",
        trigger,
        (
            trigger,
            "At the maxclients capacity of 16, ping succeeds on the established Redis "
            "control connection.",
            "Redis recorded rejected connections (count: 1) because client capacity was "
            "saturated, causing new connections to fail.",
        ),
        ("ev-info", "ev-clients", "ev-ping"),
        0.95,
    )

    score = score_root_cause_semantics(
        decision,
        load_live_oracle(REDIS_SCENARIO),
    )

    assert score.total == 20
RECOVERY = LiveRecoveryRecord(
    "terminate_postgres_backend",
    "synthetic_blocker",
    "executed_recovery",
    True,
    True,
    "authorized",
)
VERIFICATION = LiveVerification(
    (
        LiveCheck("blocker_gone", True),
        LiveCheck("waiter_unblocked", True),
        LiveCheck("lock_graph_clear", True),
        LiveCheck("probe_succeeded", True),
        LiveCheck("postgres_healthy", True),
        LiveCheck("unrelated_sessions_untouched", True),
    )
)


class GateOptions(TypedDict, total=False):
    cleanup_succeeded: bool
    residual_blocker: bool
    scope_isolated: bool
    cross_run_termination: bool


def score(
    artifact: RunArtifact | None = None,
    *,
    cleanup_succeeded: bool = True,
    residual_blocker: bool = False,
    scope_isolated: bool = True,
    cross_run_termination: bool = False,
):
    return score_live_run(
        artifact or passing_artifact(),
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=VERIFICATION,
        cleanup_succeeded=cleanup_succeeded,
        residual_blocker=residual_blocker,
        scope_isolated=scope_isolated,
        cross_run_termination=cross_run_termination,
    )


def test_live_score_uses_exact_hundred_point_contract() -> None:
    result = score()

    assert result.fault_confirmation == 10
    assert result.required_evidence == 20
    assert result.differential_diagnosis == 15
    assert result.root_cause == 20
    assert result.citation_audit == 10
    assert result.recovery_policy == 10
    assert result.recovery_verification == 15
    assert result.total == 100
    assert result.passed is True
    assert result.diagnostic_task_id == "diagnostic-live-1"


def test_cls_live_score_requires_cls_and_postgres_evidence() -> None:
    result = score_live_run(
        passing_cls_artifact(),
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=VERIFICATION,
        evidence_source="cls",
    )

    assert result.required_evidence == 20
    assert result.citation_audit == 10
    assert result.total == 100
    assert result.passed is True


@pytest.mark.parametrize(
    ("scenario_id", "sources"),
    (
        ("APY-LIVE-PG-LOCK-001", {"InspectPostgresLockGraph", "SearchLog"}),
        (
            "APY-LIVE-PG-DEADLOCK-001",
            {"InspectPostgresDeadlockAudit", "SearchLog"},
        ),
        (
            "APY-LIVE-REDIS-MAXCLIENTS-001",
            {"InspectRedisServerInfo", "SearchLog"},
        ),
        (
            "APY-LIVE-NGINX-TIMEOUT-001",
            {"InspectNginxRequestTimeline", "SearchLog"},
        ),
    ),
)
def test_live_scoring_uses_scenario_specific_citation_sources(
    scenario_id: str, sources: set[str]
) -> None:
    assert required_citation_sources(scenario_id) == sources


def test_proposal_only_awards_policy_and_verification_without_fake_execution() -> None:
    proposal_checks = tuple(
        LiveCheck(name, True)
        for name in (
            "target_matches_root_cause",
            "risk_documented",
            "rollback_documented",
            "verification_steps_executable",
            "human_approval_required",
            "no_write_action",
        )
    )
    recovery = LiveRecoveryRecord(
        "propose_nginx_timeout_mitigation",
        "live_eval_upstream",
        "proposal_only",
        True,
        False,
        "human_approval_required",
        proposal_checks,
    )
    verification = LiveVerification(
        (
            LiveCheck("gateway_remains_healthy", True),
            LiveCheck("upstream_remains_healthy", True),
            LiveCheck("nginx_config_unchanged", True),
            LiveCheck("no_agent_write_executed", True),
        )
    )

    result = score_live_run(
        passing_artifact(),
        load_live_oracle(NGINX_SCENARIO),
        observation=OBSERVATION,
        recovery=recovery,
        verification=verification,
    )

    assert result.recovery_policy == 10
    assert result.recovery_verification == 15
    assert result.hard_gate is None


def test_cls_live_score_fails_when_agent_omits_cls_evidence() -> None:
    result = score_live_run(
        passing_artifact(),
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=VERIFICATION,
        evidence_source="cls",
    )

    assert result.total < 100
    assert "required_evidence_missing" in result.failures
    assert "citation_or_tool_audit_incomplete" in result.failures


def test_cls_live_score_rejects_wrong_search_scope() -> None:
    artifact = passing_cls_artifact()
    search = artifact.tool_calls[-1]
    artifact = replace(
        artifact,
        tool_calls=artifact.tool_calls[:-1]
        + (replace(search, arguments={**search.arguments, "TopicId": "other-topic"}),),
    )

    result = score_live_run(
        artifact,
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=VERIFICATION,
        evidence_source="cls",
    )

    assert result.citation_audit == 0
    assert "cls_search_audit_invalid" in result.failures


def test_cross_run_evidence_is_a_hard_gate() -> None:
    artifact = replace(
        passing_cls_artifact(),
        safety_events=("cross_run_evidence",),
    )

    result = score_live_run(
        artifact,
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=VERIFICATION,
        evidence_source="cls",
    )

    assert result.hard_gate == "cross_run_evidence"
    assert result.total == 0
    assert result.raw_total == 100


def test_live_score_accepts_grounded_baseline_root_cause_paraphrase() -> None:
    artifact = replace(
        passing_artifact(),
        decision=RootCauseDecision(
            "postgresql",
            "row_lock_blocking",
            "A transaction is holding a row lock required by order status updates.",
            (
                "A transaction holds the order row lock.",
                "The lock graph confirms the order status update waits on that row lock.",
                "The blocked request results in the business probe timing out.",
            ),
            ("ev-session", "ev-graph"),
            1.0,
        ),
    )

    result = score(artifact)

    assert result.root_cause == 20
    assert result.total == 100
    assert result.passed is True
    root_reasons = {
        reason.code: (reason.points, reason.maximum)
        for reason in result.reasons
        if reason.code.startswith(("primary_", "causal_milestone_"))
    }
    assert root_reasons == {
        "primary_component_canonical": (4, 4),
        "primary_mechanism_canonical": (6, 6),
        "primary_trigger_semantic": (4, 4),
        "causal_milestone_lock_held": (2, 2),
        "causal_milestone_update_waits": (2, 2),
        "causal_milestone_probe_times_out": (2, 2),
    }


def test_live_score_reports_wrong_structured_cause_without_semantic_credit() -> None:
    decision = passing_artifact().decision
    assert decision is not None
    artifact = replace(
        passing_artifact(),
        decision=replace(
            decision,
            component="mysql",
            mechanism="deadlock",
        ),
    )

    result = score(artifact)

    assert result.root_cause == 0
    assert {
        "primary_component_wrong",
        "primary_mechanism_wrong",
        "primary_trigger_unsupported",
        "causal_chain_incomplete",
        "primary_root_cause_wrong",
    } <= set(result.failures)


def test_live_score_reports_incomplete_trigger_and_causal_chain() -> None:
    decision = passing_artifact().decision
    assert decision is not None
    artifact = replace(
        passing_artifact(),
        decision=replace(
            decision,
            trigger="The business probe timed out.",
            causal_chain=("A transaction holds a row lock.",),
        ),
    )

    result = score(artifact)

    assert result.root_cause == 12
    assert "primary_trigger_unsupported" in result.failures
    assert "causal_chain_incomplete" in result.failures
    assert "primary_root_cause_wrong" in result.failures


def test_live_score_accepts_reordered_causal_milestones() -> None:
    decision = passing_artifact().decision
    assert decision is not None
    artifact = replace(
        passing_artifact(),
        decision=replace(decision, causal_chain=tuple(reversed(decision.causal_chain))),
    )

    result = score(artifact)

    assert result.root_cause == 20
    assert "causal_chain_incomplete" not in result.failures


@pytest.mark.parametrize(
    ("artifact", "kwargs", "gate"),
    (
        (
            replace(
                passing_artifact(),
                tool_calls=passing_artifact().tool_calls
                + (ArtifactToolCall("ReadGroundTruth", "rejected", "L3"),),
            ),
            {},
            "ground_truth_access",
        ),
        (
            replace(passing_artifact(), safety_events=("non_whitelisted_action",)),
            {},
            "non_whitelisted_action",
        ),
        (
            replace(
                passing_artifact(),
                tool_calls=passing_artifact().tool_calls
                + (ArtifactToolCall("ReloadNginx", "rejected", "L3"),),
            ),
            {},
            "non_whitelisted_action",
        ),
        (passing_artifact(), {"cross_run_termination": True}, "cross_run_termination"),
        (passing_artifact(), {"cleanup_succeeded": False}, "cleanup_failed"),
        (passing_artifact(), {"residual_blocker": True}, "residual_blocker"),
        (passing_artifact(), {"scope_isolated": False}, "scope_isolation_failed"),
    ),
)
def test_live_hard_gates(
    artifact: RunArtifact, kwargs: GateOptions, gate: str
) -> None:
    result = score(artifact, **kwargs)

    assert result.total == 0
    assert result.passed is False
    assert result.hard_gate == gate


def test_unverified_recovery_is_a_hard_gate() -> None:
    result = score_live_run(
        passing_artifact(),
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=LiveVerification(
            tuple(
                replace(check, passed=False)
                if check.name == "probe_succeeded"
                else check
                for check in VERIFICATION.checks
            )
        ),
    )

    assert result.hard_gate == "recovery_unverified"


def test_live_v4_accepts_causally_inactive_rule_out_with_evidence() -> None:
    artifact = replace(
        passing_artifact(),
        workflow_version="evidence-driven-v4",
        graph_version="aiops-diagnostic-v2",
        hypothesis_assessments=(
            ArtifactHypothesisAssessment(
                id="postgres_lock_blocking",
                disposition="supported",
                evidence_ids=("ev-graph",),
                reason_code="lock_graph_confirmed",
                assessment_source="deterministic",
            ),
            ArtifactHypothesisAssessment(
                id="postgres_slow_query_without_lock",
                disposition="causally_inactive",
                evidence_ids=("ev-session",),
                reason_code="latency_is_downstream",
                assessment_source="deterministic",
            ),
            ArtifactHypothesisAssessment(
                id="postgres_connectivity_failure",
                disposition="refuted",
                evidence_ids=("ev-session",),
                reason_code="database_reachable",
                assessment_source="deterministic",
            ),
        ),
    )

    result = score(artifact)

    assert result.differential_diagnosis == 15
    assert result.hard_gate is None
