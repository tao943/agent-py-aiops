from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation.live.scenarios import load_live_oracle
from super_ai.evaluation.live.semantic_scoring import score_root_cause_semantics

SCENARIO = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "live"
    / "APY-LIVE-PG-LOCK-001"
)
ORDER_POOL_SCENARIO = SCENARIO.parent / "APY-LIVE-ORDER-POOL-LEAK-001"


def _decision(
    *,
    component: str = "postgresql",
    mechanism: str = "row_lock_blocking",
    trigger: str = "A transaction is holding a row lock required by order status updates.",
    causal_chain: tuple[str, ...] = (
        "The observation reveals a session waiting on a Lock event.",
        "The lock graph confirmed a blocker to waiter edge causing the timeouts.",
    ),
) -> RootCauseDecision:
    return RootCauseDecision(
        component,
        mechanism,
        trigger,
        causal_chain,
        ("ev-session", "ev-graph"),
        1.0,
    )


def test_scores_grounded_baseline_paraphrase_at_twenty() -> None:
    result = score_root_cause_semantics(_decision(), load_live_oracle(SCENARIO))

    assert result.component == 4
    assert result.mechanism == 6
    assert result.trigger == 4
    assert result.milestones == (
        ("lock_held", 2),
        ("update_waits", 2),
        ("probe_times_out", 2),
    )
    assert result.total == 20


def test_scores_oracle_language_at_twenty() -> None:
    oracle = load_live_oracle(SCENARIO)
    result = score_root_cause_semantics(
        _decision(
            trigger=oracle.primary_cause.trigger,
            causal_chain=oracle.causal_chain,
        ),
        oracle,
    )

    assert result.total == 20


def test_normalizes_structured_label_syntax_and_text_punctuation() -> None:
    result = score_root_cause_semantics(
        _decision(
            component="  PostgreSQL  ",
            mechanism="Row-Lock-Blocking",
            trigger="BLOCKER: row-lock contention.",
        ),
        load_live_oracle(SCENARIO),
    )

    assert (result.component, result.mechanism, result.trigger) == (4, 6, 4)


@pytest.mark.parametrize(
    ("component", "mechanism"),
    (
        ("mysql", "row_lock_blocking"),
        ("postgresql", "deadlock"),
        ("postgresql", "slow_query"),
        ("postgresql", "connectivity_failure"),
    ),
)
def test_wrong_structured_cause_cannot_earn_semantic_points(
    component: str, mechanism: str
) -> None:
    result = score_root_cause_semantics(
        _decision(component=component, mechanism=mechanism),
        load_live_oracle(SCENARIO),
    )

    assert result.trigger == 0
    assert all(points == 0 for _, points in result.milestones)


@pytest.mark.parametrize(
    "trigger",
    (
        "A transaction is holding resources.",
        "A row lock blocks the update.",
        "The business probe timed out.",
    ),
)
def test_trigger_requires_both_lock_holder_and_row_lock_concepts(
    trigger: str,
) -> None:
    result = score_root_cause_semantics(
        _decision(trigger=trigger), load_live_oracle(SCENARIO)
    )

    assert result.trigger == 0


def test_does_not_combine_milestone_concepts_across_chain_steps() -> None:
    result = score_root_cause_semantics(
        _decision(
            causal_chain=(
                "A blocker transaction exists.",
                "A row lock is visible.",
                "A waiting session appears.",
                "The order status update saw a Lock event.",
                "Timeout occurred.",
                "This results in degradation.",
            )
        ),
        load_live_oracle(SCENARIO),
    )

    assert result.milestones == (
        ("lock_held", 0),
        ("update_waits", 0),
        ("probe_times_out", 0),
    )


def test_milestone_order_is_not_significant() -> None:
    result = score_root_cause_semantics(
        _decision(causal_chain=tuple(reversed(_decision().causal_chain))),
        load_live_oracle(SCENARIO),
    )

    assert all(points == 2 for _, points in result.milestones)


def test_alias_matching_respects_token_boundaries() -> None:
    result = score_root_cause_semantics(
        _decision(trigger="A transaction sees a row locksmith."),
        load_live_oracle(SCENARIO),
    )

    assert result.trigger == 0


def test_requires_semantic_rubric() -> None:
    oracle = replace(load_live_oracle(SCENARIO), root_cause_semantics=None)

    with pytest.raises(ValueError, match="semantic rubric"):
        score_root_cause_semantics(_decision(), oracle)


def test_scores_grounded_order_pool_lifecycle_cause_at_twenty() -> None:
    decision = RootCauseDecision(
        "order-api",
        "exception_path_connection_not_released",
        "The exception path checks out a connection but omits the matching release.",
        (
            "The acquired connection has no checkin after the request error.",
            "This causes the database pool to become saturated with no free connection.",
            "New requests wait for acquisition and order updates time out "
            "while PostgreSQL remains reachable.",
        ),
        ("order-pool-saturated", "cls-order-connection-lifecycle"),
        1.0,
    )
    result = score_root_cause_semantics(
        decision,
        load_live_oracle(ORDER_POOL_SCENARIO),
    )
    assert result.total == 20


def test_order_pool_saturation_alone_does_not_earn_trigger_points() -> None:
    decision = RootCauseDecision(
        "order-api",
        "exception_path_connection_not_released",
        "The connection pool is saturated.",
        ("Order updates time out.",),
        ("order-pool-saturated",),
        1.0,
    )
    result = score_root_cause_semantics(
        decision,
        load_live_oracle(ORDER_POOL_SCENARIO),
    )
    assert result.trigger == 0
