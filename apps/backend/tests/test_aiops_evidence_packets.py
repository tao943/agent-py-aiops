from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from super_ai.aiops.evidence_aggregation import (
    AggregationContext,
    SpecialistAggregationContext,
    aggregate_evidence_packets,
    aggregate_specialist_results,
)
from super_ai.aiops.investigation import EvidenceClaim, EvidencePacket
from super_ai.aiops.specialists import (
    PublicAssessmentSignal,
    SpecialistAnalysisErrorCode,
    SpecialistAssignment,
    SpecialistResult,
)
from super_ai.memory.repositories import (
    DiagnosticEvidenceRecord,
    ToolCallAuditRecord,
)


def _claim(**overrides: object) -> EvidenceClaim:
    values: dict[str, object] = {
        "claim_id": "database_accepting_connections",
        "value": True,
        "quality": "direct",
        "causal_role": "mechanism",
        "supports": ("database_healthy",),
        "refutes": ("database_unavailable",),
        "evidence_ids": ("evidence-runtime",),
        "target_component": "postgres",
        "observed_at": datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
        "time_scope": "incident_window",
    }
    values.update(overrides)
    return EvidenceClaim(**cast(Any, values))


def _packet(**overrides: object) -> EvidencePacket:
    values: dict[str, object] = {
        "task_id": "diagnostic-1",
        "owner_user_id": "owner-1",
        "dispatch_id": "dispatch-runtime",
        "investigator_type": "runtime",
        "status": "completed",
        "claims": (_claim(),),
        "limitations": (),
        "tool_call_ids": ("call-runtime",),
        "model_calls_used": 0,
    }
    values.update(overrides)
    return EvidencePacket(**cast(Any, values))


def _context(**overrides: object) -> AggregationContext:
    values: dict[str, object] = {
        "owner_user_id": "owner-1",
        "task_id": "diagnostic-1",
        "investigator_by_dispatch": {
            "dispatch-knowledge": "knowledge",
            "dispatch-runtime": "runtime",
            "dispatch-log": "log",
        },
        "evidence_ids": frozenset(
            {"evidence-knowledge", "evidence-runtime", "evidence-log"}
        ),
        "completed_tool_call_ids": frozenset(
            {"call-knowledge", "call-runtime", "call-log"}
        ),
        "tool_name_by_call_id": {
            "call-knowledge": "knowledge_retrieval",
            "call-runtime": "InspectPostgresSessions",
            "call-log": "SearchLog",
        },
        "tool_call_id_by_evidence_id": {
            "evidence-knowledge": "call-knowledge",
            "evidence-runtime": "call-runtime",
            "evidence-log": "call-log",
        },
        "allowed_tools_by_investigator": {
            "knowledge": frozenset({"knowledge_retrieval"}),
            "runtime": frozenset({"InspectPostgresSessions"}),
            "log": frozenset({"SearchLog"}),
            "change": frozenset(),
        },
        "maximum_quality_by_evidence_id": {
            "evidence-knowledge": "reference",
            "evidence-runtime": "direct",
            "evidence-log": "direct",
        },
    }
    values.update(overrides)
    return AggregationContext(**cast(Any, values))


@pytest.mark.parametrize(
    "packet",
    (
        _packet(owner_user_id="owner-2"),
        _packet(task_id="diagnostic-2"),
        _packet(dispatch_id="unknown-dispatch"),
        _packet(investigator_type="log"),
    ),
)
def test_aggregator_rejects_cross_scope_or_unexpected_dispatch(
    packet: EvidencePacket,
) -> None:
    result = aggregate_evidence_packets((packet,), context=_context())

    assert result.accepted_packets == ()
    assert result.claims == ()
    assert result.rejected_dispatches == {
        packet.dispatch_id: "invalid_evidence_packet"
    }


@pytest.mark.parametrize(
    ("packet", "context"),
    (
        (
            _packet(claims=(_claim(evidence_ids=("evidence-unknown",)),)),
            _context(),
        ),
        (
            _packet(),
            _context(completed_tool_call_ids=frozenset({"call-log"})),
        ),
        (
            _packet(),
            _context(
                tool_name_by_call_id={"call-runtime": "RestartDatabase"},
                allowed_tools_by_investigator={
                    "runtime": frozenset({"InspectPostgresSessions"})
                },
            ),
        ),
        (
            _packet(
                dispatch_id="dispatch-knowledge",
                investigator_type="knowledge",
                claims=(
                    _claim(
                        quality="direct",
                        evidence_ids=("evidence-knowledge",),
                    ),
                ),
                tool_call_ids=("call-knowledge",),
            ),
            _context(),
        ),
    ),
)
def test_aggregator_rejects_unknown_or_untrusted_evidence(
    packet: EvidencePacket, context: AggregationContext
) -> None:
    result = aggregate_evidence_packets((packet,), context=context)

    assert result.accepted_packets == ()
    assert result.rejected_dispatches[packet.dispatch_id] == "invalid_evidence_packet"


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"causal_role": "recovery"}, "causal role"),
        ({"time_scope": "whenever"}, "time scope"),
        ({"quality": "verified"}, "quality"),
        ({"value": {"privateReasoning": "hidden"}}, "private"),
        ({"value": {"modelRawResponse": "hidden"}}, "private"),
        ({"value": {"prompt": "hidden"}}, "private"),
        ({"value": {"secret": "hidden"}}, "private"),
        ({"value": {"recoveryAction": "restart"}}, "recovery"),
        ({"value": float("nan")}, "finite"),
    ),
)
def test_claim_schema_rejects_invalid_or_private_content(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _claim(**overrides)


def test_failed_and_timeout_packets_cannot_create_negative_evidence() -> None:
    for status in ("failed", "timeout"):
        with pytest.raises(ValueError, match="claims"):
            _packet(
                status=status,
                claims=(
                    _claim(
                        supports=(),
                        refutes=("database_healthy",),
                    ),
                ),
            )

        packet = _packet(
            status=status,
            claims=(),
            tool_call_ids=(),
            limitations=(f"Investigator {status}.",),
        )
        result = aggregate_evidence_packets((packet,), context=_context())
        assert result.accepted_packets == (packet,)
        assert result.claims == ()


def test_aggregation_is_order_independent_and_deduplicates_claims() -> None:
    runtime_claim = _claim(
        evidence_ids=("evidence-runtime", "evidence-runtime")
    )
    runtime = _packet(claims=(runtime_claim, runtime_claim))
    log_claim = _claim(
        claim_id="database_deadlock_logged",
        value=True,
        evidence_ids=("evidence-log",),
        target_component="postgres",
    )
    log = _packet(
        dispatch_id="dispatch-log",
        investigator_type="log",
        claims=(log_claim,),
        tool_call_ids=("call-log",),
    )

    forward = aggregate_evidence_packets((log, runtime), context=_context())
    reverse = aggregate_evidence_packets((runtime, log), context=_context())

    assert forward == reverse
    assert tuple(packet.dispatch_id for packet in forward.accepted_packets) == (
        "dispatch-runtime",
        "dispatch-log",
    )
    assert len(forward.claims) == 2
    assert forward.claims[0].evidence_ids == ("evidence-runtime",)


def test_same_scope_contradictory_direct_values_create_stable_conflict() -> None:
    runtime = _packet(claims=(_claim(value=True),))
    log = _packet(
        dispatch_id="dispatch-log",
        investigator_type="log",
        claims=(
            _claim(value=False, evidence_ids=("evidence-log",)),
        ),
        tool_call_ids=("call-log",),
    )

    result = aggregate_evidence_packets((log, runtime), context=_context())

    assert len(result.conflicts) == 1
    assert result.conflicts[0]["claimId"] == "database_accepting_connections"
    assert result.conflicts[0]["targetComponent"] == "postgres"
    assert result.conflicts[0]["timeScope"] == "incident_window"


def test_incident_abnormal_and_current_healthy_are_not_a_conflict() -> None:
    incident = _packet(claims=(_claim(value=False),))
    current = _packet(
        dispatch_id="dispatch-log",
        investigator_type="log",
        claims=(
            _claim(
                value=True,
                evidence_ids=("evidence-log",),
                time_scope="current",
                observed_at=datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc),
            ),
        ),
        tool_call_ids=("call-log",),
    )

    result = aggregate_evidence_packets((incident, current), context=_context())

    assert len(result.claims) == 2
    assert result.conflicts == ()


def test_knowledge_claims_remain_reference_quality() -> None:
    knowledge = _packet(
        dispatch_id="dispatch-knowledge",
        investigator_type="knowledge",
        claims=(
            _claim(
                quality="reference",
                causal_role=None,
                supports=(),
                refutes=(),
                evidence_ids=("evidence-knowledge",),
                observed_at=None,
                time_scope="historical",
            ),
        ),
        tool_call_ids=("call-knowledge",),
    )

    result = aggregate_evidence_packets((knowledge,), context=_context())

    assert result.rejected_dispatches == {}
    assert result.claims[0].quality == "reference"


def test_direct_claim_requires_observation_time() -> None:
    with pytest.raises(ValueError, match="observation time"):
        _claim(observed_at=None)


def test_packet_rejects_private_limitations_and_negative_model_budget() -> None:
    with pytest.raises(ValueError, match="private"):
        _packet(limitations=("Prompt contained a credential.",))
    with pytest.raises(ValueError, match="model calls"):
        _packet(model_calls_used=-1)


def test_duplicate_packet_dispatch_is_rejected_fail_closed() -> None:
    first = _packet()
    duplicate = replace(first, limitations=("duplicate",))

    result = aggregate_evidence_packets((first, duplicate), context=_context())

    assert result.accepted_packets == ()
    assert result.rejected_dispatches == {
        "dispatch-runtime": "invalid_evidence_packet"
    }


def test_identical_packet_retry_is_aggregated_once() -> None:
    packet = _packet()

    result = aggregate_evidence_packets((packet, packet), context=_context())

    assert result.accepted_packets == (packet,)
    assert len(result.claims) == 1
    assert result.rejected_dispatches == {}


def _specialist_assignment(role: str) -> SpecialistAssignment:
    deadline = datetime(2026, 8, 20, 10, 10, tzinfo=timezone.utc)
    if role == "runtime":
        tools = frozenset({"InspectOrderPoolState"})
        arguments: dict[str, dict[str, object]] = {"InspectOrderPoolState": {}}
    else:
        tools = frozenset({"SearchLog"})
        arguments = {
            "SearchLog": {
                "Region": "ap-guangzhou",
                "TopicId": "topic-safe",
                "From": 10,
                "To": 20,
                "Query": 'incident_id:"safe"',
                "Limit": 20,
            }
        }
    return SpecialistAssignment(
        role=cast(Any, role),
        objective="Test public evidence.",
        hypotheses_to_test=("pool_lifecycle_failure",),
        required_causal_roles=("trigger", "mechanism", "impact"),
        allowed_tools=tools,
        trusted_arguments_by_tool=cast(Any, arguments),
        maximum_tool_steps=3,
        model_call_budget=2,
        soft_deadline_at=deadline,
        hard_deadline_at=deadline.replace(minute=11),
    )


def _specialist_evidence(
    evidence_id: str,
    *,
    role: str,
    source_fingerprint: str,
    owner_user_id: str = "owner-1",
    task_id: str = "diagnostic-1",
    tool_name: str | None = None,
) -> DiagnosticEvidenceRecord:
    selected_tool = tool_name or (
        "InspectOrderPoolState" if role == "runtime" else "SearchLog"
    )
    return DiagnosticEvidenceRecord(
        id=evidence_id,
        owner_user_id=owner_user_id,
        task_id=task_id,
        step_id=f"step-{role}",
        tool_call_id=f"call-{evidence_id}",
        kind="tool_observation",
        source=selected_tool,
        summary="A safe public observation.",
        payload={"sourceFingerprint": source_fingerprint},
        created_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
    )


def _specialist_audit(
    evidence: DiagnosticEvidenceRecord,
    *,
    tool_name: str | None = None,
) -> ToolCallAuditRecord:
    assert evidence.tool_call_id is not None
    return ToolCallAuditRecord(
        id=evidence.tool_call_id,
        owner_user_id=evidence.owner_user_id,
        task_id=evidence.task_id,
        tool_name=tool_name or evidence.source,
        status="completed",
        arguments={},
        result_payload={},
        error_message=None,
        started_at=evidence.created_at,
        completed_at=evidence.created_at,
        created_at=evidence.created_at,
    )


def _specialist_result(
    role: str,
    evidence_ids: tuple[str, ...],
    *,
    status: str = "completed",
    value: object = True,
    analysis_status: str | None = None,
    analysis_error_code: SpecialistAnalysisErrorCode | None = None,
    unresolved_questions: tuple[str, ...] | None = None,
) -> SpecialistResult:
    facts = (
        _claim(
            claim_id="order_pool_saturated",
            value=value,
            supports=("pool_lifecycle_failure",),
            refutes=(),
            evidence_ids=evidence_ids,
            target_component="order-api",
        ),
    ) if evidence_ids else ()
    assessments = (
        PublicAssessmentSignal(
            hypothesis_id="pool_lifecycle_failure",
            disposition="supported",
            evidence_ids=evidence_ids,
            summary="Untrusted Specialist assessment.",
        ),
    ) if evidence_ids else ()
    return SpecialistResult.create(
        role=cast(Any, role),
        terminal_status=cast(Any, status),
        evidence_status="complete" if evidence_ids else "none",
        analysis_status=cast(
            Any,
            analysis_status
            or (
                "complete"
                if status == "completed"
                else "timeout"
                if status == "timeout"
                else "failed"
            ),
        ),
        analysis_error_code=(
            analysis_error_code
            or ("specialist_hard_deadline_expired" if status == "timeout" else None)
        ),
        analysis_attempt_count=1 if evidence_ids else 0,
        soft_deadline_exceeded=False,
        hard_deadline_exceeded=status == "timeout",
        expected_tool_count=1 if evidence_ids else 0,
        tested_hypotheses=("pool_lifecycle_failure",),
        evidence_ids=evidence_ids,
        fact_candidates=facts,
        proposed_assessments=assessments,
        unresolved_questions=(
            unresolved_questions
            if unresolved_questions is not None
            else ()
            if status == "completed"
            else (f"{role}_unavailable",)
        ),
        completed_steps=(f"{role}-1",) if evidence_ids else (),
        model_call_count=2 if evidence_ids else 1,
        duration_ms=10,
    )


def _specialist_context(
    evidence: tuple[DiagnosticEvidenceRecord, ...],
    *,
    audits: tuple[ToolCallAuditRecord, ...] | None = None,
) -> SpecialistAggregationContext:
    selected_audits = audits or tuple(_specialist_audit(item) for item in evidence)
    return SpecialistAggregationContext(
        owner_user_id="owner-1",
        task_id="diagnostic-1",
        graph_version="evidence-driven-v4",
        assignments={
            "runtime": _specialist_assignment("runtime"),
            "log": _specialist_assignment("log"),
        },
        evidence_by_id={item.id: item for item in evidence},
        completed_tool_audit_by_id={item.id: item for item in selected_audits},
    )


def test_specialist_aggregation_is_order_independent_and_groups_sources() -> None:
    runtime_evidence = _specialist_evidence(
        "evidence-runtime", role="runtime", source_fingerprint="shared-source"
    )
    log_evidence = _specialist_evidence(
        "evidence-log", role="log", source_fingerprint="shared-source"
    )
    context = _specialist_context((runtime_evidence, log_evidence))
    runtime = _specialist_result("runtime", (runtime_evidence.id, runtime_evidence.id))
    log = _specialist_result("log", (log_evidence.id,))

    forward = aggregate_specialist_results((runtime, log), context=context)
    reverse = aggregate_specialist_results((log, runtime), context=context)

    assert forward == reverse
    assert forward.evidence == ("evidence-log", "evidence-runtime")
    assert forward.source_groups == {
        "shared-source": ("evidence-log", "evidence-runtime")
    }
    assert forward.budget_usage == {"log": 2, "runtime": 2, "total": 4}


def test_specialist_aggregation_separates_evidence_and_analysis_health() -> None:
    runtime_evidence = _specialist_evidence(
        "evidence-runtime", role="runtime", source_fingerprint="runtime-source"
    )
    log_evidence = _specialist_evidence(
        "evidence-log", role="log", source_fingerprint="log-source"
    )
    result = aggregate_specialist_results(
        (
            _specialist_result(
                "runtime",
                (runtime_evidence.id,),
                status="inconclusive",
                analysis_status="degraded",
                analysis_error_code="retry_exhausted",
            ),
            _specialist_result(
                "log",
                (log_evidence.id,),
                unresolved_questions=("Which deploy changed latency?",),
            ),
        ),
        context=_specialist_context((runtime_evidence, log_evidence)),
    )

    assert result.specialist_evidence_statuses == {
        "log": "complete",
        "runtime": "complete",
    }
    assert result.specialist_analysis_statuses == {
        "log": "complete",
        "runtime": "degraded",
    }
    assert result.specialist_analysis_error_codes == {
        "runtime": "retry_exhausted"
    }
    assert result.specialist_analysis_attempt_counts == {"log": 1, "runtime": 1}
    assert result.specialist_follow_up_question_counts == {"log": 1, "runtime": 1}
    assert result.specialist_statuses == {
        "log": "completed",
        "runtime": "inconclusive",
    }


def test_specialist_aggregation_records_conflict_without_voting() -> None:
    runtime_evidence = _specialist_evidence(
        "evidence-runtime", role="runtime", source_fingerprint="runtime-source"
    )
    log_evidence = _specialist_evidence(
        "evidence-log", role="log", source_fingerprint="log-source"
    )
    result = aggregate_specialist_results(
        (
            _specialist_result("runtime", (runtime_evidence.id,), value=True),
            _specialist_result("log", (log_evidence.id,), value=False),
        ),
        context=_specialist_context((runtime_evidence, log_evidence)),
    )

    assert len(result.normalized_facts) == 2
    assert len(result.conflicts) == 1
    assert result.conflicts[0]["claimId"] == "order_pool_saturated"
    assert len(result.hypothesis_signals) == 2


@pytest.mark.parametrize(
    ("record_overrides", "audit_tool"),
    (
        ({"owner_user_id": "owner-2"}, None),
        ({"task_id": "diagnostic-2"}, None),
        ({}, "SearchLog"),
    ),
)
def test_specialist_aggregation_rejects_foreign_or_wrong_role_evidence(
    record_overrides: dict[str, str],
    audit_tool: str | None,
) -> None:
    evidence = _specialist_evidence(
        "evidence-runtime",
        role="runtime",
        source_fingerprint="runtime-source",
        **record_overrides,
    )
    audit = _specialist_audit(evidence, tool_name=audit_tool)

    with pytest.raises(ValueError, match="invalid_specialist_evidence"):
        aggregate_specialist_results(
            (_specialist_result("runtime", (evidence.id,)),),
            context=_specialist_context((evidence,), audits=(audit,)),
        )


def test_specialist_timeout_preserves_other_domain_and_both_fail_closed() -> None:
    runtime_evidence = _specialist_evidence(
        "evidence-runtime", role="runtime", source_fingerprint="runtime-source"
    )
    context = _specialist_context((runtime_evidence,))
    partial = aggregate_specialist_results(
        (
            _specialist_result("runtime", (runtime_evidence.id,)),
            _specialist_result("log", (), status="timeout"),
        ),
        context=context,
    )

    assert partial.evidence == (runtime_evidence.id,)
    assert partial.missing_domains == ("log",)
    assert partial.terminal_failure_category is None

    failed = aggregate_specialist_results(
        (
            _specialist_result("runtime", (), status="failed"),
            _specialist_result("log", (), status="timeout"),
        ),
        context=_specialist_context(()),
    )

    assert failed.terminal_failure_category == "multi_investigation_failed"
    payload = failed.to_checkpoint_payload()
    assert "rootCause" not in payload
    assert "recovery" not in payload
