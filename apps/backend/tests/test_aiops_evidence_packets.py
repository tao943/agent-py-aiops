from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from super_ai.aiops.evidence_aggregation import (
    AggregationContext,
    aggregate_evidence_packets,
)
from super_ai.aiops.investigation import EvidenceClaim, EvidencePacket


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
