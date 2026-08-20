"""Code-owned compound evidence patterns for deterministic AIOps decisions."""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import cast

from super_ai.aiops.adjudication import (
    DiagnosticFact,
    Disposition,
    HypothesisAssessment,
    apply_deterministic_transition,
)

_PATTERN_ID = "nginx_upstream_read_timeout"
_GATEWAY_HEALTH_MAX_LATENCY_MS = 250
_REQUIRED_HYPOTHESES = frozenset(
    {
        "nginx_gateway_pressure",
        "nginx_route_mismatch",
        "nginx_upstream_response_timeout",
        "nginx_upstream_unavailable",
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
) -> TrustedPatternResolution:
    """Apply code-owned cross-tool patterns without scenario or Oracle input."""
    ordered = tuple(sorted(assessments, key=lambda item: item.hypothesis_id))
    by_id = {item.hypothesis_id: item for item in ordered}
    if not _REQUIRED_HYPOTHESES.issubset(by_id):
        return TrustedPatternResolution(ordered, (), ())
    public = tuple(
        fact
        for fact in facts
        if fact.public
        and fact.quality == "direct"
        and fact.evidence_id in trusted_evidence_ids
    )
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
        (_PATTERN_ID,),
    )


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
