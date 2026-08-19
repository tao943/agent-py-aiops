from __future__ import annotations

from dataclasses import replace

import pytest

from super_ai.aiops.adjudication import (
    DiagnosticFact,
    Disposition,
    HypothesisAssessment,
    apply_deterministic_transition,
)
from super_ai.aiops.trusted_patterns import resolve_trusted_patterns

_HYPOTHESES = (
    "nginx_gateway_pressure",
    "nginx_route_mismatch",
    "nginx_upstream_response_timeout",
    "nginx_upstream_unavailable",
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
            "ProbeLiveEvalGateway.status",
            200,
            "ev-gateway",
            "ProbeLiveEvalGateway",
        ),
        _fact(
            "ProbeLiveEvalGateway.healthy",
            True,
            "ev-gateway",
            "ProbeLiveEvalGateway",
        ),
        _fact(
            "ProbeLiveEvalGateway.latencyMs",
            gateway_latency_ms,
            "ev-gateway",
            "ProbeLiveEvalGateway",
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


@pytest.mark.parametrize(
    ("key", "replacement"),
    (
        ("InspectNginxRequestTimeline.upstreamConnectSucceeded", False),
        ("ReadNginxTimeoutSummary.readDeadlineElapsed", False),
        ("ProbeLiveEvalUpstream.healthy", False),
        ("ProbeLiveEvalGateway.healthy", False),
        ("ProbeLiveEvalGateway.latencyMs", 2_000),
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
