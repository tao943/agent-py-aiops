"""Code-owned compound evidence patterns for deterministic AIOps decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import cast

from super_ai.aiops.adjudication import (
    DiagnosticFact,
    Disposition,
    HypothesisAssessment,
    TrustedEvidenceProvenance,
    apply_deterministic_transition,
)

_NGINX_PATTERN_ID = "nginx_upstream_read_timeout"
_ORDER_POOL_PATTERN_ID = "order_connection_checkout_without_checkin"
_GATEWAY_HEALTH_MAX_LATENCY_MS = 250
_REQUIRED_HYPOTHESES = frozenset(
    {
        "nginx_gateway_pressure",
        "nginx_route_mismatch",
        "nginx_upstream_response_timeout",
        "nginx_upstream_unavailable",
    }
)
_ORDER_POOL_REQUIRED_HYPOTHESES = frozenset(
    {
        "order_connection_lifecycle_failure",
        "order_traffic_capacity_exceeded",
        "order_slow_statement",
        "order_database_lock_wait",
        "order_database_unreachable",
    }
)


@dataclass(frozen=True, slots=True)
class TrustedPatternResolution:
    assessments: tuple[HypothesisAssessment, ...]
    observations: tuple[dict[str, object], ...]
    matched_pattern_ids: tuple[str, ...]


def resolve_trusted_patterns(
    *,
    assessments: Sequence[HypothesisAssessment],
    facts: Sequence[DiagnosticFact],
    trusted_evidence_ids: Set[str],
    evidence_provenance: Mapping[str, TrustedEvidenceProvenance] | None = None,
) -> TrustedPatternResolution:
    """Apply code-owned cross-tool patterns without scenario or Oracle input."""
    ordered = tuple(sorted(assessments, key=lambda item: item.hypothesis_id))
    by_id = {item.hypothesis_id: item for item in ordered}
    public = tuple(
        fact
        for fact in facts
        if fact.public
        and fact.quality == "direct"
        and fact.evidence_id in trusted_evidence_ids
    )
    if _REQUIRED_HYPOTHESES.issubset(by_id):
        return _resolve_nginx_timeout(ordered, by_id, public)
    if _ORDER_POOL_REQUIRED_HYPOTHESES.issubset(by_id):
        return _resolve_order_pool_lifecycle(
            ordered,
            by_id,
            public,
            evidence_provenance=evidence_provenance,
        )
    return TrustedPatternResolution(ordered, (), ())


def _resolve_nginx_timeout(
    ordered: tuple[HypothesisAssessment, ...],
    by_id: dict[str, HypothesisAssessment],
    public: Sequence[DiagnosticFact],
) -> TrustedPatternResolution:
    matched = _match_nginx_timeout(public)
    if matched is None:
        return TrustedPatternResolution(ordered, (), ())

    timeline, summary, upstream, gateway, cls = matched
    transitions: tuple[tuple[str, Disposition, tuple[str, ...], str], ...] = (
        (
            "nginx_upstream_response_timeout",
            "supported",
            (timeline, summary, upstream, gateway, cls),
            "trusted_upstream_read_timeout",
        ),
        (
            "nginx_route_mismatch",
            "refuted",
            (timeline, upstream, cls),
            "connected_route_reached_healthy_upstream",
        ),
        (
            "nginx_upstream_unavailable",
            "refuted",
            (timeline, upstream),
            "upstream_connects_and_is_healthy",
        ),
        (
            "nginx_gateway_pressure",
            "causally_inactive",
            (gateway, summary),
            "gateway_probe_healthy_during_read_timeout",
        ),
    )
    resolved = dict(by_id)
    for hypothesis_id, disposition, evidence_ids, reason_code in transitions:
        resolved[hypothesis_id] = apply_deterministic_transition(
            resolved[hypothesis_id],
            disposition=disposition,
            evidence_ids=evidence_ids,
            reason_code=reason_code,
        )
    common: dict[str, object] = {
        "supports": ["nginx_upstream_response_timeout"],
        "refutes": [],
        "assessmentSource": "deterministic",
        "causalRoleOrigin": "trusted_compound_pattern",
    }
    observations: tuple[dict[str, object], ...] = (
        {
            **common,
            "purpose": "Establish the incident-scoped upstream timeout trigger.",
            "summary": (
                "The upstream response delay exceeded the gateway read timeout while "
                "the incident log recorded an upstream timeout and both independent "
                "health probes remained healthy."
            ),
            "evidenceIds": [cls, upstream, gateway],
            "causalRole": "trigger",
        },
        {
            **common,
            "purpose": "Establish the gateway read-timeout mechanism.",
            "summary": (
                "The Nginx gateway confirms the connection established to the upstream; "
                "the upstream connect succeeds before waiting for the delayed response."
            ),
            "evidenceIds": [timeline, summary],
            "causalRole": "mechanism",
        },
        {
            **common,
            "purpose": "Establish the user-visible gateway impact.",
            "summary": (
                "The exceeded response deadline causes the Nginx gateway to return "
                "HTTP 504 gateway timeout while the upstream remained healthy."
            ),
            "evidenceIds": [timeline],
            "causalRole": "impact",
        },
    )
    return TrustedPatternResolution(
        tuple(resolved[item.hypothesis_id] for item in ordered),
        observations,
        (_NGINX_PATTERN_ID,),
    )


def _resolve_order_pool_lifecycle(
    ordered: tuple[HypothesisAssessment, ...],
    by_id: dict[str, HypothesisAssessment],
    public: Sequence[DiagnosticFact],
    *,
    evidence_provenance: Mapping[str, TrustedEvidenceProvenance] | None,
) -> TrustedPatternResolution:
    matched = _match_order_pool_lifecycle(public)
    if matched is None or not _order_pool_provenance_is_valid(
        public,
        matched,
        evidence_provenance=evidence_provenance,
    ):
        return TrustedPatternResolution(ordered, (), ())
    cls_id, pool_id, sessions_id, health_id = matched
    transitions: tuple[tuple[str, Disposition, tuple[str, ...], str], ...] = (
        (
            "order_connection_lifecycle_failure",
            "supported",
            (cls_id, pool_id, sessions_id, health_id),
            "trusted_checkout_without_checkin",
        ),
        (
            "order_database_unreachable",
            "refuted",
            (sessions_id, health_id),
            "database_reachable_during_pool_timeout",
        ),
        (
            "order_database_lock_wait",
            "refuted",
            (sessions_id,),
            "run_sessions_have_no_lock_wait",
        ),
        (
            "order_traffic_capacity_exceeded",
            "causally_inactive",
            (cls_id, pool_id),
            "failed_update_lifecycle_precedes_pool_timeout",
        ),
        (
            "order_slow_statement",
            "causally_inactive",
            (cls_id, sessions_id, health_id),
            "failed_lifecycle_with_reachable_unlocked_database",
        ),
    )
    resolved = dict(by_id)
    for hypothesis_id, disposition, evidence_ids, reason_code in transitions:
        resolved[hypothesis_id] = apply_deterministic_transition(
            resolved[hypothesis_id],
            disposition=disposition,
            evidence_ids=evidence_ids,
            reason_code=reason_code,
        )
    common: dict[str, object] = {
        "supports": ["order_connection_lifecycle_failure"],
        "refutes": [],
        "assessmentSource": "deterministic",
        "causalRoleOrigin": "trusted_compound_pattern",
    }
    observations: tuple[dict[str, object], ...] = (
        {
            **common,
            "purpose": "Establish the failed connection lifecycle trigger.",
            "summary": (
                "The exception path checks out a connection for the order update but "
                "omits the matching release after the update fails."
            ),
            "evidenceIds": [cls_id],
            "causalRole": "trigger",
        },
        {
            **common,
            "purpose": "Establish the exhausted connection-pool mechanism.",
            "summary": (
                "Because checked-out connections are not released, they accumulate and "
                "this causes the order-api connection pool to become saturated with no free "
                "connection; current-run PostgreSQL sessions remain present without a "
                "database lock wait."
            ),
            "evidenceIds": [cls_id, pool_id, sessions_id],
            "causalRole": "mechanism",
        },
        {
            **common,
            "purpose": "Establish the business request impact.",
            "summary": (
                "As a result, new order updates wait for acquisition; order updates time "
                "out while PostgreSQL remains reachable."
            ),
            "evidenceIds": [pool_id, health_id],
            "causalRole": "impact",
        },
    )
    return TrustedPatternResolution(
        tuple(resolved[item.hypothesis_id] for item in ordered),
        observations,
        (_ORDER_POOL_PATTERN_ID,),
    )


def _order_pool_provenance_is_valid(
    facts: Sequence[DiagnosticFact],
    evidence_ids: tuple[str, str, str, str],
    *,
    evidence_provenance: Mapping[str, TrustedEvidenceProvenance] | None,
) -> bool:
    if evidence_provenance is None:
        return False
    selected: list[TrustedEvidenceProvenance] = []
    for evidence_id in evidence_ids:
        provenance = evidence_provenance.get(evidence_id)
        source_tools = {
            fact.source_tool for fact in facts if fact.evidence_id == evidence_id
        }
        if (
            provenance is None
            or provenance.evidence_id != evidence_id
            or source_tools != {provenance.tool_name}
            or (
                provenance.source_domain == "log"
                and provenance.tool_name != "SearchLog"
            )
            or (
                provenance.source_domain == "runtime"
                and provenance.tool_name == "SearchLog"
            )
        ):
            return False
        selected.append(provenance)
    return (
        len({item.owner_user_id for item in selected}) == 1
        and len({item.task_id for item in selected}) == 1
        and len({item.source_fingerprint for item in selected}) == len(selected)
    )


def _match_order_pool_lifecycle(
    facts: Sequence[DiagnosticFact],
) -> tuple[str, str, str, str] | None:
    lifecycle = _one_ordered_lifecycle_fact(facts)
    pool_full = _one_fact(facts, "InspectOrderPoolState.poolAtCapacity", True)
    pool_free = _one_fact(facts, "InspectOrderPoolState.freeConnections", 0)
    pool_waiter = _one_fact(facts, "InspectOrderPoolState.waiterObserved", True)
    sessions = _one_fact(
        facts,
        "InspectOrderDatabaseSessions.runScopedSessionsPresent",
        True,
    )
    sessions_reachable = _one_fact(
        facts,
        "InspectOrderDatabaseSessions.databaseReachable",
        True,
    )
    no_lock_wait = _one_fact(
        facts,
        "InspectOrderDatabaseSessions.lockWaitObserved",
        False,
    )
    health_reachable = _one_fact(
        facts,
        "VerifyOrderDatabaseReachability.databaseReachable",
        True,
    )
    probe_timeout = _one_fact(
        facts,
        "VerifyOrderDatabaseReachability.businessProbeTimedOut",
        True,
    )
    required = (
        lifecycle,
        pool_full,
        pool_free,
        pool_waiter,
        sessions,
        sessions_reachable,
        no_lock_wait,
        health_reachable,
        probe_timeout,
    )
    if any(item is None for item in required):
        return None
    assert lifecycle is not None
    assert pool_full is not None
    assert pool_free is not None
    assert pool_waiter is not None
    assert sessions is not None
    assert sessions_reachable is not None
    assert no_lock_wait is not None
    assert health_reachable is not None
    assert probe_timeout is not None
    pool_ids = {pool_full.evidence_id, pool_free.evidence_id, pool_waiter.evidence_id}
    session_ids = {
        sessions.evidence_id,
        sessions_reachable.evidence_id,
        no_lock_wait.evidence_id,
    }
    health_ids = {health_reachable.evidence_id, probe_timeout.evidence_id}
    if len(pool_ids) != 1 or len(session_ids) != 1 or len(health_ids) != 1:
        return None
    evidence_ids = (
        lifecycle.evidence_id,
        next(iter(pool_ids)),
        next(iter(session_ids)),
        next(iter(health_ids)),
    )
    if len(set(evidence_ids)) != 4:
        return None
    return evidence_ids


def _one_ordered_lifecycle_fact(
    facts: Sequence[DiagnosticFact],
) -> DiagnosticFact | None:
    candidates = _facts_for_key(facts, "SearchLog.records.event")
    if len(candidates) != 1:
        return None
    fact = candidates[0]
    raw_value: object = fact.value
    if not isinstance(raw_value, tuple):
        return None
    events = cast(tuple[object, ...], raw_value)
    for timeout_position, event in enumerate(events):
        if event != "pool_acquire_timeout":
            continue
        for checkout_position in range(timeout_position - 1, -1, -1):
            if events[checkout_position] != "connection_checkout":
                continue
            lifecycle_window = events[checkout_position + 1 : timeout_position]
            if (
                "order_update_failed" in lifecycle_window
                and "connection_checkin" not in lifecycle_window
            ):
                return fact
    return None


def _match_nginx_timeout(
    facts: Sequence[DiagnosticFact],
) -> tuple[str, str, str, str, str] | None:
    timeline_status = _one_fact(facts, "InspectNginxRequestTimeline.gatewayStatus", 504)
    duration = _one_numeric_fact(
        facts,
        "InspectNginxRequestTimeline.requestDurationMs",
        minimum=1,
    )
    connected = _one_fact(
        facts,
        "InspectNginxRequestTimeline.upstreamConnectSucceeded",
        True,
    )
    timeout = _one_fact(
        facts,
        "ReadNginxTimeoutSummary.gatewayTimeoutObserved",
        True,
    )
    deadline = _one_fact(
        facts,
        "ReadNginxTimeoutSummary.readDeadlineElapsed",
        True,
    )
    upstream_status = _one_fact(facts, "ProbeLiveEvalUpstream.status", 200)
    upstream_healthy = _one_fact(facts, "ProbeLiveEvalUpstream.healthy", True)
    gateway_status = _one_fact(facts, "ProbeLiveEvalUpstream.gatewayStatus", 200)
    gateway_healthy = _one_fact(facts, "ProbeLiveEvalUpstream.gatewayHealthy", True)
    gateway_latency = _one_numeric_fact(
        facts,
        "ProbeLiveEvalUpstream.gatewayLatencyMs",
        minimum=0,
        maximum=_GATEWAY_HEALTH_MAX_LATENCY_MS,
    )
    cls_timeout = _one_contains_fact(
        facts,
        "SearchLog.records.event",
        "upstream_timeout",
    )
    required = (
        timeline_status,
        duration,
        connected,
        timeout,
        deadline,
        upstream_status,
        upstream_healthy,
        gateway_status,
        gateway_healthy,
        gateway_latency,
        cls_timeout,
    )
    if any(item is None for item in required):
        return None
    assert timeline_status is not None
    assert timeout is not None
    assert upstream_healthy is not None
    assert gateway_healthy is not None
    assert cls_timeout is not None
    return (
        timeline_status.evidence_id,
        timeout.evidence_id,
        upstream_healthy.evidence_id,
        gateway_healthy.evidence_id,
        cls_timeout.evidence_id,
    )


def _facts_for_key(
    facts: Sequence[DiagnosticFact], key: str
) -> tuple[DiagnosticFact, ...]:
    return tuple(fact for fact in facts if fact.key == key)


def _one_fact(
    facts: Sequence[DiagnosticFact], key: str, expected: object
) -> DiagnosticFact | None:
    candidates = _facts_for_key(facts, key)
    if not candidates or any(fact.value != expected for fact in candidates):
        return None
    return sorted(candidates, key=lambda item: item.evidence_id)[0]


def _one_numeric_fact(
    facts: Sequence[DiagnosticFact],
    key: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> DiagnosticFact | None:
    candidates = _facts_for_key(facts, key)
    if not candidates:
        return None
    for fact in candidates:
        if (
            not isinstance(fact.value, (int, float))
            or isinstance(fact.value, bool)
            or fact.value < minimum
            or (maximum is not None and fact.value > maximum)
        ):
            return None
    return sorted(candidates, key=lambda item: item.evidence_id)[0]


def _one_contains_fact(
    facts: Sequence[DiagnosticFact], key: str, expected: str
) -> DiagnosticFact | None:
    candidates = _facts_for_key(facts, key)
    if not candidates:
        return None
    matching: list[DiagnosticFact] = []
    for fact in candidates:
        value = fact.value
        if isinstance(value, str) and expected in value:
            matching.append(fact)
        elif isinstance(value, tuple):
            items = cast(tuple[object, ...], value)
            if expected in items:
                matching.append(fact)
    if not matching:
        return None
    return sorted(matching, key=lambda item: item.evidence_id)[0]
