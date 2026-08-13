from __future__ import annotations

from dataclasses import replace

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation.live.recovery import (
    PostgresRecoveryPlanner,
    PostgresRecoveryPolicy,
    PostgresSessionState,
)
from super_ai.evaluation.live.scenarios import validate_run_id


def _decision(mechanism: str = "row_lock_blocking") -> RootCauseDecision:
    return RootCauseDecision(
        component="postgresql",
        mechanism=mechanism,
        trigger="synthetic_transaction_holds_order_row_lock",
        causal_chain=("lock acquired", "update waits"),
        evidence_ids=("ev-wait", "ev-edge"),
        confidence=0.95,
    )


def _state(**changes: object) -> PostgresSessionState:
    base = PostgresSessionState(
        pid=4321,
        database="agent_py_live_eval",
        application_name="agentpy-live:run-1:blocker",
        backend_type="client backend",
        blocked_waiter_pids=(5678,),
    )
    return replace(base, **changes)


def test_planner_uses_unique_live_blocker_for_matching_agent_decision() -> None:
    intent = PostgresRecoveryPlanner().plan(
        decision=_decision(),
        blocker_pids=(4321,),
    )

    assert intent is not None
    assert intent.action == "terminate_postgres_backend"
    assert intent.target_pid == 4321


@pytest.mark.parametrize(
    "decision, blockers",
    [
        (None, (4321,)),
        (_decision("slow_query_without_lock"), (4321,)),
        (_decision(), ()),
        (_decision(), (4321, 4322)),
    ],
)
def test_planner_refuses_missing_wrong_or_ambiguous_diagnosis(
    decision: RootCauseDecision | None,
    blockers: tuple[int, ...],
) -> None:
    assert PostgresRecoveryPlanner().plan(decision=decision, blocker_pids=blockers) is None


def test_policy_allows_exact_current_run_synthetic_blocker() -> None:
    identity = validate_run_id("run-1")
    intent = PostgresRecoveryPlanner().plan(decision=_decision(), blocker_pids=(4321,))
    assert intent is not None

    result = PostgresRecoveryPolicy().authorize(
        identity=identity,
        intent=intent,
        state=_state(),
        injected_blocker_pid=4321,
        waiter_pid=5678,
        executor_pid=9999,
    )

    assert result.allowed is True
    assert result.code == "authorized"


@pytest.mark.parametrize(
    "intent_action, state, injected_pid, waiter_pid, executor_pid, code",
    [
        ("restart_database", _state(), 4321, 5678, 9999, "action_not_allowed"),
        ("terminate_postgres_backend", None, 4321, 5678, 9999, "target_missing"),
        (
            "terminate_postgres_backend",
            _state(database="agent_py"),
            4321,
            5678,
            9999,
            "wrong_database",
        ),
        (
            "terminate_postgres_backend",
            _state(application_name="ordinary-app"),
            4321,
            5678,
            9999,
            "wrong_application",
        ),
        (
            "terminate_postgres_backend",
            _state(application_name="agentpy-live:run-2:blocker"),
            4321,
            5678,
            9999,
            "cross_run_target",
        ),
        ("terminate_postgres_backend", _state(pid=9999), 9999, 5678, 9999, "executor_target"),
        ("terminate_postgres_backend", _state(pid=5678), 5678, 5678, 9999, "waiter_target"),
        (
            "terminate_postgres_backend",
            _state(backend_type="autovacuum worker"),
            4321,
            5678,
            9999,
            "system_backend",
        ),
        (
            "terminate_postgres_backend",
            _state(blocked_waiter_pids=()),
            4321,
            5678,
            9999,
            "blocking_edge_missing",
        ),
        ("terminate_postgres_backend", _state(), 7777, 5678, 9999, "injected_pid_mismatch"),
    ],
)
def test_policy_rejects_every_unsafe_target(
    intent_action: str,
    state: PostgresSessionState | None,
    injected_pid: int,
    waiter_pid: int,
    executor_pid: int,
    code: str,
) -> None:
    identity = validate_run_id("run-1")
    intent = PostgresRecoveryPlanner().plan(decision=_decision(), blocker_pids=(4321,))
    assert intent is not None
    intent = replace(intent, action=intent_action)
    if code in {"executor_target", "waiter_target"}:
        assert state is not None
        intent = replace(intent, target_pid=state.pid)

    result = PostgresRecoveryPolicy().authorize(
        identity=identity,
        intent=intent,
        state=state,
        injected_blocker_pid=injected_pid,
        waiter_pid=waiter_pid,
        executor_pid=executor_pid,
    )

    assert result.allowed is False
    assert result.code == code
