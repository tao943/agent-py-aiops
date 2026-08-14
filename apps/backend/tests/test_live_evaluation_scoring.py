from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TypedDict

import pytest

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.evaluation import ArtifactEvidence, ArtifactToolCall, RunArtifact
from super_ai.evaluation.artifacts import LiveRecoveryAudit
from super_ai.evaluation.live.domain import (
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)
from super_ai.evaluation.live.scenarios import load_live_oracle
from super_ai.evaluation.live.scoring import score_live_run

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
        live_recovery=LiveRecoveryAudit(
            "terminate_postgres_backend", "synthetic_blocker", True, True, True, "authorized"
        ),
    )


OBSERVATION = LiveFaultObservation(101, 102, True, True)
RECOVERY = LiveRecoveryRecord("terminate_postgres_backend", 101, True, True, "authorized")
VERIFICATION = LiveVerification(True, True, True, True, True, True)


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


def test_live_score_accepts_grounded_baseline_root_cause_paraphrase() -> None:
    artifact = replace(
        passing_artifact(),
        decision=RootCauseDecision(
            "postgresql",
            "row_lock_blocking",
            "A transaction is holding a row lock required by order status updates.",
            (
                "The observation reveals a session waiting on a Lock event.",
                "The lock graph confirmed a blocker to waiter edge causing the timeouts.",
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
        verification=replace(VERIFICATION, probe_succeeded=False),
    )

    assert result.hard_gate == "recovery_unverified"
