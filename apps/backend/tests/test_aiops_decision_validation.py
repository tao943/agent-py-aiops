from __future__ import annotations

from dataclasses import replace

import pytest

from super_ai.aiops.decision_validation import (
    deterministic_checks_payload,
    validate_grounded_candidate,
)
from super_ai.aiops.reasoning import RootCauseDecision


def _decision(
    *,
    component: str = "order-service",
    mechanism: str = "opposite_order_transaction_deadlock",
    trigger: str = "Concurrent transactions acquired shared rows in opposite orders.",
    causal_chain: tuple[str, ...] = (
        "PostgreSQL emitted SQLSTATE 40P01.",
        "The wait graph contained a two-session cycle.",
        "Transactions acquired the same rows in opposite order.",
    ),
    evidence_ids: tuple[str, ...] = ("ev-error", "ev-cycle", "ev-order"),
    confidence: float = 0.97,
) -> RootCauseDecision:
    return RootCauseDecision(
        component=component,
        mechanism=mechanism,
        trigger=trigger,
        causal_chain=causal_chain,
        evidence_ids=evidence_ids,
        confidence=confidence,
    )


def _states() -> list[dict[str, object]]:
    return [
        {
            "id": "postgres_deadlock",
            "status": "supported",
            "confidence": 1.0,
            "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
        },
        {
            "id": "postgres_lock_wait",
            "status": "refuted",
            "confidence": 0.1,
            "evidenceIds": ["ev-cycle"],
        },
        {
            "id": "postgres_slow_query",
            "status": "refuted",
            "confidence": 0.1,
            "evidenceIds": ["ev-error"],
        },
    ]


def _observations() -> list[dict[str, object]]:
    return [
        {
            "supports": ["postgres_deadlock"],
            "refutes": ["postgres_slow_query"],
            "evidenceIds": ["ev-error"],
            "summary": "PostgreSQL emitted SQLSTATE 40P01.",
        },
        {
            "supports": ["postgres_deadlock"],
            "refutes": ["postgres_lock_wait"],
            "evidenceIds": ["ev-cycle"],
            "summary": "The wait graph contained a two-session cycle.",
        },
        {
            "supports": ["postgres_deadlock"],
            "refutes": [],
            "evidenceIds": ["ev-order"],
            "summary": "Transactions acquired the same rows in opposite order.",
        },
    ]


def _vocabulary() -> dict[str, object]:
    return {
        "labelsByHypothesis": {
            "postgres_deadlock": {
                "component": "order-service",
                "mechanism": "opposite_order_transaction_deadlock",
            },
            "postgres_lock_wait": {
                "component": "postgresql",
                "mechanism": "long_transaction_lock_blocking",
            },
            "postgres_slow_query": {
                "component": "postgresql",
                "mechanism": "slow_query_without_lock",
            },
        }
    }


def _validate(
    candidate: RootCauseDecision,
    *,
    states: list[dict[str, object]] | None = None,
    observations: list[dict[str, object]] | None = None,
    available_evidence_ids: set[str] | None = None,
):
    return validate_grounded_candidate(
        candidate=candidate,
        available_evidence_ids=available_evidence_ids
        or {"ev-error", "ev-cycle", "ev-order"},
        hypothesis_states=states or _states(),
        observation_decisions=observations or _observations(),
        decision_vocabulary=_vocabulary(),
    )


def test_grounded_candidate_passes_every_public_evidence_check() -> None:
    result = _validate(_decision())

    assert result.passed is True
    assert result.supported_hypothesis_id == "postgres_deadlock"
    assert result.unsupported_fields == ()
    assert result.missing_evidence == ()
    assert all(check.passed for check in result.checks)
    assert deterministic_checks_payload(result) == [
        {"code": check.code, "passed": True} for check in result.checks
    ]


@pytest.mark.parametrize(
    ("case", "expected_check"),
    [
        ("multiple_supported", "unique_supported_hypothesis"),
        ("open_competitor", "no_open_competitor"),
        ("wrong_public_label", "public_label_match"),
        ("foreign_evidence_id", "task_evidence_only"),
        ("available_but_non_supporting_evidence_id", "supporting_evidence_only"),
        ("one_positive_evidence", "independent_positive_evidence"),
        ("one_supporting_observation", "supporting_observations"),
        ("invented_causal_step", "grounded_causal_chain"),
        ("blank_trigger", "trigger_present"),
        ("confidence_out_of_range", "confidence_in_range"),
    ],
)
def test_grounded_candidate_fails_closed_for_each_missing_condition(
    case: str,
    expected_check: str,
) -> None:
    candidate = _decision()
    states = _states()
    observations = _observations()
    available = {"ev-error", "ev-cycle", "ev-order"}

    if case == "multiple_supported":
        states[1] = {**states[1], "status": "supported"}
    elif case == "open_competitor":
        states[1] = {**states[1], "status": "open"}
    elif case == "wrong_public_label":
        candidate = replace(candidate, mechanism="generic_deadlock")
    elif case == "foreign_evidence_id":
        candidate = replace(candidate, evidence_ids=("ev-error", "ev-foreign"))
    elif case == "available_but_non_supporting_evidence_id":
        available.add("ev-alert")
        candidate = replace(candidate, evidence_ids=("ev-alert",))
    elif case == "one_positive_evidence":
        observations = [observations[0]]
        candidate = replace(
            candidate,
            causal_chain=(observations[0]["summary"], observations[0]["summary"]),
            evidence_ids=("ev-error",),
        )
    elif case == "one_supporting_observation":
        observations = [
            {
                **observations[0],
                "evidenceIds": ["ev-error", "ev-cycle"],
            }
        ]
        candidate = replace(
            candidate,
            causal_chain=(observations[0]["summary"], observations[0]["summary"]),
            evidence_ids=("ev-error", "ev-cycle"),
        )
    elif case == "invented_causal_step":
        candidate = replace(
            candidate,
            causal_chain=(candidate.causal_chain[0], "An invented causal assertion."),
        )
    elif case == "blank_trigger":
        candidate = replace(candidate, trigger="   ")
    elif case == "confidence_out_of_range":
        candidate = replace(candidate, confidence=1.1)

    result = _validate(
        candidate,
        states=states,
        observations=observations,
        available_evidence_ids=available,
    )

    assert result.passed is False
    assert expected_check in {check.code for check in result.checks if not check.passed}


def test_knowledge_like_available_id_cannot_count_as_positive_evidence() -> None:
    candidate = replace(_decision(), evidence_ids=("ev-knowledge", "ev-alert"))

    result = _validate(
        candidate,
        available_evidence_ids={
            "ev-error",
            "ev-cycle",
            "ev-order",
            "ev-knowledge",
            "ev-alert",
        },
    )

    assert result.passed is False
    assert "supporting_evidence_only" in {
        check.code for check in result.checks if not check.passed
    }
