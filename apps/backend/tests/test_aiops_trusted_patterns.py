from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.aiops import adjudication as adjudication_module
from super_ai.aiops.adjudication import (
    DiagnosticFact,
    Disposition,
    HypothesisAssessment,
    TrustedEvidenceProvenance,
    apply_deterministic_transition,
)
from super_ai.aiops.trusted_patterns import resolve_trusted_patterns
from super_ai.evaluation.live.scenarios import load_live_oracle
from super_ai.evaluation.live.semantic_scoring import score_root_cause_semantics

_NGINX_SCENARIO = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "live"
    / "APY-LIVE-NGINX-TIMEOUT-001"
)
_ORDER_POOL_SCENARIO = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "live"
    / "APY-LIVE-ORDER-POOL-LEAK-001"
)

_HYPOTHESES = (
    "nginx_gateway_pressure",
    "nginx_route_mismatch",
    "nginx_upstream_response_timeout",
    "nginx_upstream_unavailable",
)

_ORDER_POOL_HYPOTHESES = (
    "order_connection_lifecycle_failure",
    "order_traffic_capacity_exceeded",
    "order_slow_statement",
    "order_database_lock_wait",
    "order_database_unreachable",
)


def _assessments() -> tuple[HypothesisAssessment, ...]:
    return tuple(
        HypothesisAssessment(
            hypothesis_id=hypothesis_id,
            disposition="unresolved",
            evidence_ids=(),
            reason_code="awaiting_public_evidence",
            assessment_source="deterministic",
        )
        for hypothesis_id in _HYPOTHESES
    )


def _fact(
    key: str,
    value: object,
    evidence_id: str,
    source_tool: str,
) -> DiagnosticFact:
    return DiagnosticFact(
        key=key,
        value=value,
        evidence_id=evidence_id,
        source_tool=source_tool,
        quality="direct",
    )


def _facts(*, gateway_latency_ms: int = 19) -> tuple[DiagnosticFact, ...]:
    return (
        _fact(
            "InspectNginxRequestTimeline.gatewayStatus",
            504,
            "ev-timeline",
            "InspectNginxRequestTimeline",
        ),
        _fact(
            "InspectNginxRequestTimeline.requestDurationMs",
            913,
            "ev-timeline",
            "InspectNginxRequestTimeline",
        ),
        _fact(
            "InspectNginxRequestTimeline.upstreamConnectSucceeded",
            True,
            "ev-timeline",
            "InspectNginxRequestTimeline",
        ),
        _fact(
            "ReadNginxTimeoutSummary.gatewayTimeoutObserved",
            True,
            "ev-summary",
            "ReadNginxTimeoutSummary",
        ),
        _fact(
            "ReadNginxTimeoutSummary.readDeadlineElapsed",
            True,
            "ev-summary",
            "ReadNginxTimeoutSummary",
        ),
        _fact(
            "ProbeLiveEvalUpstream.status",
            200,
            "ev-upstream",
            "ProbeLiveEvalUpstream",
        ),
        _fact(
            "ProbeLiveEvalUpstream.healthy",
            True,
            "ev-upstream",
            "ProbeLiveEvalUpstream",
        ),
        _fact(
            "ProbeLiveEvalUpstream.gatewayStatus",
            200,
            "ev-gateway",
            "ProbeLiveEvalUpstream",
        ),
        _fact(
            "ProbeLiveEvalUpstream.gatewayHealthy",
            True,
            "ev-gateway",
            "ProbeLiveEvalUpstream",
        ),
        _fact(
            "ProbeLiveEvalUpstream.gatewayLatencyMs",
            gateway_latency_ms,
            "ev-gateway",
            "ProbeLiveEvalUpstream",
        ),
        _fact(
            "SearchLog.records.event",
            ("request_received", "upstream_timeout"),
            "ev-cls",
            "SearchLog",
        ),
    )


def _evidence_ids() -> frozenset[str]:
    return frozenset(fact.evidence_id for fact in _facts())


def _order_pool_assessments() -> tuple[HypothesisAssessment, ...]:
    return tuple(
        HypothesisAssessment(
            hypothesis_id=hypothesis_id,
            disposition="unresolved",
            evidence_ids=(),
            reason_code="awaiting_public_evidence",
            assessment_source="deterministic",
        )
        for hypothesis_id in _ORDER_POOL_HYPOTHESES
    )


def _order_pool_facts() -> tuple[DiagnosticFact, ...]:
    return (
        _fact(
            "SearchLog.records.event",
            (
                "request_received",
                "connection_checkout",
                "order_update_failed",
                "pool_acquire_timeout",
            ),
            "ev-order-cls",
            "SearchLog",
        ),
        _fact(
            "InspectOrderPoolState.poolAtCapacity",
            True,
            "ev-order-pool",
            "InspectOrderPoolState",
        ),
        _fact(
            "InspectOrderPoolState.freeConnections",
            0,
            "ev-order-pool",
            "InspectOrderPoolState",
        ),
        _fact(
            "InspectOrderPoolState.waiterObserved",
            True,
            "ev-order-pool",
            "InspectOrderPoolState",
        ),
        _fact(
            "InspectOrderDatabaseSessions.runScopedSessionsPresent",
            True,
            "ev-order-sessions",
            "InspectOrderDatabaseSessions",
        ),
        _fact(
            "InspectOrderDatabaseSessions.databaseReachable",
            True,
            "ev-order-sessions",
            "InspectOrderDatabaseSessions",
        ),
        _fact(
            "InspectOrderDatabaseSessions.lockWaitObserved",
            False,
            "ev-order-sessions",
            "InspectOrderDatabaseSessions",
        ),
        _fact(
            "VerifyOrderDatabaseReachability.databaseReachable",
            True,
            "ev-order-health",
            "VerifyOrderDatabaseReachability",
        ),
        _fact(
            "VerifyOrderDatabaseReachability.businessProbeTimedOut",
            True,
            "ev-order-health",
            "VerifyOrderDatabaseReachability",
        ),
    )


def _order_pool_evidence_ids() -> frozenset[str]:
    return frozenset(fact.evidence_id for fact in _order_pool_facts())


def _order_pool_provenance(
    facts: tuple[DiagnosticFact, ...] | None = None,
) -> dict[str, TrustedEvidenceProvenance]:
    selected = facts or _order_pool_facts()
    by_evidence = {fact.evidence_id: fact for fact in selected}
    return {
        evidence_id: TrustedEvidenceProvenance(
            evidence_id=evidence_id,
            owner_user_id="owner-order-pool",
            task_id="task-order-pool",
            source_fingerprint=f"source:{fact.source_tool}",
            source_domain=("log" if fact.source_tool == "SearchLog" else "runtime"),
            tool_name=fact.source_tool,
        )
        for evidence_id, fact in by_evidence.items()
    }


def test_trusted_evidence_provenance_is_a_public_immutable_contract() -> None:
    provenance_type = getattr(
        adjudication_module,
        "TrustedEvidenceProvenance",
        None,
    )

    assert provenance_type is not None
    assert provenance_type.__dataclass_params__.frozen is True


def test_trusted_pattern_resolver_requires_explicit_evidence_provenance() -> None:
    signature = inspect.signature(resolve_trusted_patterns)

    assert "evidence_provenance" in signature.parameters


def test_order_pool_pattern_closes_lifecycle_and_rules_out_database_causes() -> None:
    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=_order_pool_facts(),
        trusted_evidence_ids=_order_pool_evidence_ids(),
        evidence_provenance=_order_pool_provenance(),
    )

    by_id = {item.hypothesis_id: item for item in result.assessments}
    assert by_id["order_connection_lifecycle_failure"].disposition == "supported"
    assert by_id["order_database_unreachable"].disposition == "refuted"
    assert by_id["order_database_lock_wait"].disposition == "refuted"
    assert by_id["order_traffic_capacity_exceeded"].disposition != "supported"
    assert by_id["order_slow_statement"].disposition != "supported"
    assert [item["causalRole"] for item in result.observations] == [
        "trigger",
        "mechanism",
        "impact",
    ]
    assert result.matched_pattern_ids == (
        "order_connection_checkout_without_checkin",
    )


def test_order_pool_pattern_projection_satisfies_live_semantic_contract() -> None:
    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=_order_pool_facts(),
        trusted_evidence_ids=_order_pool_evidence_ids(),
        evidence_provenance=_order_pool_provenance(),
    )
    by_role = {
        str(item["causalRole"]): str(item["summary"])
        for item in result.observations
    }
    decision = RootCauseDecision(
        "order-api",
        "exception_path_connection_not_released",
        by_role["trigger"],
        (by_role["trigger"], by_role["mechanism"], by_role["impact"]),
        tuple(sorted(_order_pool_evidence_ids())),
        0.95,
    )

    score = score_root_cause_semantics(
        decision,
        load_live_oracle(_ORDER_POOL_SCENARIO),
    )

    assert score.total == 20


def test_order_pool_pattern_ignores_completed_checkout_before_leaking_checkout() -> None:
    facts = tuple(
        replace(
            fact,
            value=(
                "connection_checkout",
                "connection_checkin",
                "connection_checkout",
                "order_update_failed",
                "pool_acquire_timeout",
            ),
        )
        if fact.key == "SearchLog.records.event"
        else fact
        for fact in _order_pool_facts()
    )

    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=facts,
        trusted_evidence_ids=frozenset(fact.evidence_id for fact in facts),
        evidence_provenance=_order_pool_provenance(facts),
    )

    assert result.matched_pattern_ids == (
        "order_connection_checkout_without_checkin",
    )


@pytest.mark.parametrize(
    ("key", "replacement"),
    (
        (
            "SearchLog.records.event",
            ("request_received", "order_update_failed", "pool_acquire_timeout"),
        ),
        (
            "SearchLog.records.event",
            (
                "request_received",
                "connection_checkout",
                "connection_checkin",
                "order_update_failed",
                "pool_acquire_timeout",
            ),
        ),
        ("InspectOrderPoolState.poolAtCapacity", False),
        ("InspectOrderPoolState.freeConnections", 1),
        ("InspectOrderPoolState.waiterObserved", False),
        ("InspectOrderDatabaseSessions.runScopedSessionsPresent", False),
        ("InspectOrderDatabaseSessions.databaseReachable", False),
        ("InspectOrderDatabaseSessions.lockWaitObserved", True),
        ("VerifyOrderDatabaseReachability.databaseReachable", False),
        ("VerifyOrderDatabaseReachability.businessProbeTimedOut", False),
    ),
)
def test_order_pool_pattern_fails_closed_on_missing_or_conflicting_fact(
    key: str,
    replacement: object,
) -> None:
    facts = tuple(
        replace(fact, value=replacement) if fact.key == key else fact
        for fact in _order_pool_facts()
    )

    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=facts,
        trusted_evidence_ids=frozenset(fact.evidence_id for fact in facts),
        evidence_provenance=_order_pool_provenance(facts),
    )

    assert result.matched_pattern_ids == ()
    assert all(item.disposition == "unresolved" for item in result.assessments)


def test_order_pool_pattern_rejects_different_evidence_ids_from_one_source() -> None:
    provenance = _order_pool_provenance()
    pool = provenance["ev-order-pool"]
    health = provenance["ev-order-health"]
    provenance["ev-order-health"] = replace(
        health,
        source_fingerprint=pool.source_fingerprint,
    )

    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=_order_pool_facts(),
        trusted_evidence_ids=_order_pool_evidence_ids(),
        evidence_provenance=provenance,
    )

    assert result.matched_pattern_ids == ()
    assert all(item.disposition == "unresolved" for item in result.assessments)


@pytest.mark.parametrize("scope_field", ("owner_user_id", "task_id"))
def test_order_pool_pattern_rejects_foreign_evidence_scope(scope_field: str) -> None:
    provenance = _order_pool_provenance()
    current = provenance["ev-order-cls"]
    provenance["ev-order-cls"] = replace(
        current,
        **{scope_field: "foreign-scope"},
    )

    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=_order_pool_facts(),
        trusted_evidence_ids=_order_pool_evidence_ids(),
        evidence_provenance=provenance,
    )

    assert result.matched_pattern_ids == ()
    assert all(item.disposition == "unresolved" for item in result.assessments)


def test_order_pool_pattern_rejects_missing_evidence_provenance() -> None:
    provenance = _order_pool_provenance()
    provenance.pop("ev-order-sessions")

    result = resolve_trusted_patterns(
        assessments=_order_pool_assessments(),
        facts=_order_pool_facts(),
        trusted_evidence_ids=_order_pool_evidence_ids(),
        evidence_provenance=provenance,
    )

    assert result.matched_pattern_ids == ()
    assert all(item.disposition == "unresolved" for item in result.assessments)


def test_nginx_timeout_pattern_closes_each_hypothesis_with_direct_evidence() -> None:
    result = resolve_trusted_patterns(
        assessments=_assessments(),
        facts=_facts(),
        trusted_evidence_ids=_evidence_ids(),
    )

    by_id = {item.hypothesis_id: item for item in result.assessments}
    assert by_id["nginx_upstream_response_timeout"].disposition == "supported"
    assert by_id["nginx_route_mismatch"].disposition == "refuted"
    assert by_id["nginx_upstream_unavailable"].disposition == "refuted"
    assert by_id["nginx_gateway_pressure"].disposition == "causally_inactive"
    assert all(item.evidence_ids for item in by_id.values())
    assert [item["causalRole"] for item in result.observations] == [
        "trigger",
        "mechanism",
        "impact",
    ]
    assert result.matched_pattern_ids == ("nginx_upstream_read_timeout",)


def test_nginx_timeout_pattern_projects_explicit_causal_semantics() -> None:
    result = resolve_trusted_patterns(
        assessments=_assessments(),
        facts=_facts(),
        trusted_evidence_ids=_evidence_ids(),
    )

    by_role = {str(item["causalRole"]): str(item["summary"]) for item in result.observations}
    assert "upstream response delay exceeded the gateway read timeout" in by_role[
        "trigger"
    ].lower()
    assert "connection established to the upstream" in by_role["mechanism"].lower()
    assert "causes the nginx gateway to return http 504" in by_role["impact"].lower()


def test_nginx_timeout_pattern_projection_satisfies_live_semantic_contract() -> None:
    result = resolve_trusted_patterns(
        assessments=_assessments(),
        facts=_facts(),
        trusted_evidence_ids=_evidence_ids(),
    )
    by_role = {str(item["causalRole"]): str(item["summary"]) for item in result.observations}
    decision = RootCauseDecision(
        "live-eval-upstream",
        "upstream_response_exceeded_proxy_read_timeout",
        by_role["trigger"],
        (by_role["trigger"], by_role["mechanism"], by_role["impact"]),
        tuple(sorted(_evidence_ids())),
        0.95,
    )

    score = score_root_cause_semantics(decision, load_live_oracle(_NGINX_SCENARIO))

    assert score.total == 20


@pytest.mark.parametrize(
    ("key", "replacement"),
    (
        ("InspectNginxRequestTimeline.upstreamConnectSucceeded", False),
        ("ReadNginxTimeoutSummary.readDeadlineElapsed", False),
        ("ProbeLiveEvalUpstream.healthy", False),
        ("ProbeLiveEvalUpstream.gatewayHealthy", False),
        ("ProbeLiveEvalUpstream.gatewayLatencyMs", 2_000),
        ("SearchLog.records.event", ("request_received",)),
    ),
)
def test_nginx_timeout_pattern_fails_closed_on_missing_or_conflicting_fact(
    key: str,
    replacement: object,
) -> None:
    facts = tuple(
        replace(fact, value=replacement) if fact.key == key else fact
        for fact in _facts()
    )

    result = resolve_trusted_patterns(
        assessments=_assessments(),
        facts=facts,
        trusted_evidence_ids=frozenset(fact.evidence_id for fact in facts),
    )

    assert result.matched_pattern_ids == ()
    assert all(item.disposition == "unresolved" for item in result.assessments)


def test_nginx_timeout_pattern_ignores_foreign_task_evidence() -> None:
    facts = tuple(
        replace(fact, evidence_id="ev-foreign")
        if fact.key == "SearchLog.records.event"
        else fact
        for fact in _facts()
    )

    result = resolve_trusted_patterns(
        assessments=_assessments(),
        facts=facts,
        trusted_evidence_ids=_evidence_ids() - {"ev-cls"},
    )

    assert result.matched_pattern_ids == ()
    assert all(item.disposition == "unresolved" for item in result.assessments)


def test_nginx_timeout_pattern_does_not_depend_on_identity_or_fixture_duration() -> None:
    identity_facts = (
        *_facts(gateway_latency_ms=31),
        _fact("scenarioId", "APY-LIVE-NGINX-TIMEOUT-999", "ev-identity", "Alert"),
        _fact("runId", "arbitrary-run", "ev-identity", "Alert"),
    )
    varied = tuple(
        replace(fact, value=1_127)
        if fact.key == "InspectNginxRequestTimeline.requestDurationMs"
        else fact
        for fact in identity_facts
    )

    result = resolve_trusted_patterns(
        assessments=_assessments(),
        facts=varied,
        trusted_evidence_ids=frozenset(fact.evidence_id for fact in varied),
    )

    assert result.matched_pattern_ids == ("nginx_upstream_read_timeout",)


@pytest.mark.parametrize(
    ("first", "second"),
    (("supported", "refuted"), ("refuted", "supported")),
)
def test_deterministic_transition_conflict_is_order_independent_and_sticky(
    first: Disposition,
    second: Disposition,
) -> None:
    initial = _assessments()[2]
    first_result = apply_deterministic_transition(
        initial,
        disposition=first,
        evidence_ids=("ev-first",),
        reason_code=f"evidence_{first}",
    )
    conflicted = apply_deterministic_transition(
        first_result,
        disposition=second,
        evidence_ids=("ev-second",),
        reason_code=f"evidence_{second}",
    )
    repeated = apply_deterministic_transition(
        conflicted,
        disposition=first,
        evidence_ids=("ev-first",),
        reason_code=f"evidence_{first}",
    )

    assert conflicted.disposition == "unresolved"
    assert conflicted.has_high_quality_conflict is True
    assert conflicted.evidence_ids == ("ev-first", "ev-second")
    assert repeated == conflicted
