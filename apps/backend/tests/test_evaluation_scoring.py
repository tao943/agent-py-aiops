from dataclasses import replace
from pathlib import Path

import pytest

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.evaluation import (
    ArtifactEvidence,
    ArtifactToolCall,
    RunArtifact,
    ScenarioOracle,
    load_scenario_oracle,
    score_run,
)
from super_ai.evaluation.artifacts import ArtifactHypothesisAssessment

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"


def process_down_oracle() -> ScenarioOracle:
    return load_scenario_oracle(SCENARIOS / "APY-003")


def port_mismatch_oracle() -> ScenarioOracle:
    return load_scenario_oracle(SCENARIOS / "APY-006")


def process_down_artifact() -> RunArtifact:
    oracle = process_down_oracle()
    return RunArtifact(
        scenario_id="APY-003",
        mode="snapshot",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            component=oracle.primary_cause.component,
            mechanism=oracle.primary_cause.mechanism,
            trigger=oracle.primary_cause.trigger,
            causal_chain=oracle.causal_chain,
            evidence_ids=("ev-container", "ev-nginx"),
            confidence=0.95,
        ),
        evidence=(
            ArtifactEvidence(
                record_id="ev-container",
                claim_id="container-status-exited",
                grounded=True,
            ),
            ArtifactEvidence(
                record_id="ev-nginx",
                claim_id="nginx-upstream-connection-refused",
                grounded=True,
            ),
        ),
        hypothesis_states=(
            HypothesisState(
                id="upstream_process_down",
                status="supported",
                confidence=0.95,
                evidence_ids=("ev-container", "ev-nginx"),
            ),
            HypothesisState(
                id="upstream_port_mismatch",
                status="refuted",
                confidence=0.1,
                evidence_ids=("ev-container",),
            ),
        ),
        observation_decisions=(
            ObservationDecision(
                purpose="Inspect the upstream process.",
                supports=("upstream_process_down",),
                refutes=("upstream_port_mismatch",),
                summary="The process is not running.",
            ),
            ObservationDecision(
                purpose="Inspect the gateway upstream.",
                supports=("upstream_process_down",),
                refutes=(),
                summary="The resolved upstream refused the configured port.",
            ),
        ),
        tool_calls=(
            ArtifactToolCall(name="InspectContainer", status="completed", risk_tier="L0"),
            ArtifactToolCall(name="InspectNginx", status="completed", risk_tier="L0"),
        ),
        plan_step_count=2,
        duration_ms=1_200,
        safety_events=(),
    )


def test_exact_grounded_decision_passes_paired_case() -> None:
    result = score_run(process_down_artifact(), process_down_oracle())

    assert result.diagnosis == 25
    assert result.evidence == 20
    assert result.hard_gate is None
    assert result.passed is True


def test_process_score_ignores_knowledge_retrieval_observation_count() -> None:
    artifact = process_down_artifact()
    with_rag = replace(
        artifact,
        tool_calls=(
            ArtifactToolCall("knowledge_retrieval", "completed", "L0"),
            *artifact.tool_calls,
        ),
    )

    result = score_run(with_rag, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert reason.points == 5


def apy_013_artifact(
    *,
    trigger: str,
    causal_chain: tuple[str, ...],
    mechanism: str = "opposite_order_transaction_deadlock",
) -> RunArtifact:
    evidence_ids = ("ev-error", "ev-cycle", "ev-order")
    return RunArtifact(
        scenario_id="APY-013",
        mode="snapshot",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            component="order-service",
            mechanism=mechanism,
            trigger=trigger,
            causal_chain=causal_chain,
            evidence_ids=evidence_ids,
            confidence=0.96,
        ),
        evidence=(
            ArtifactEvidence("ev-error", "postgres-40p01-deadlock-record", True),
            ArtifactEvidence("ev-cycle", "postgres-deadlock-cycle", True),
            ArtifactEvidence("ev-order", "postgres-opposite-resource-order", True),
        ),
        hypothesis_states=(
            HypothesisState("postgres_deadlock", "supported", 0.96, evidence_ids),
            HypothesisState("postgres_lock_wait", "refuted", 0.1, ("ev-cycle",)),
        ),
        observation_decisions=(
            ObservationDecision("error", ("postgres_deadlock",), (), causal_chain[2]),
            ObservationDecision("cycle", ("postgres_deadlock",), (), causal_chain[1]),
            ObservationDecision("order", ("postgres_deadlock",), (), causal_chain[0]),
        ),
        tool_calls=(
            ArtifactToolCall("InspectPostgresErrors", "completed", "L0"),
            ArtifactToolCall("InspectPostgresWaitGraph", "completed", "L0"),
            ArtifactToolCall("InspectTransactionResourceOrder", "completed", "L0"),
        ),
        plan_step_count=3,
        duration_ms=1_000,
        safety_events=(),
    )

def test_process_score_rejects_duplicate_observation_coverage() -> None:
    base = process_down_artifact()
    artifact = replace(
        base,
        tool_calls=(
            replace(base.tool_calls[0], audit_id="call-1"),
            replace(base.tool_calls[1], audit_id="call-2"),
            ArtifactToolCall(
                "InspectPostgresErrors", "completed", "L0", audit_id="call-3"
            ),
            ArtifactToolCall(
                "InspectPostgresWaitGraph", "completed", "L0", audit_id="call-4"
            ),
        ),
        evidence=(
            replace(base.evidence[0], tool_call_id="call-1"),
            replace(base.evidence[1], tool_call_id="call-2"),
            ArtifactEvidence("ev-3", "claim-3", True, tool_call_id="call-3"),
            ArtifactEvidence("ev-4", "claim-4", True, tool_call_id="call-4"),
        ),
        observation_decisions=(
            replace(base.observation_decisions[0], evidence_ids=("ev-container",)),
            replace(base.observation_decisions[1], evidence_ids=("ev-nginx",)),
            replace(base.observation_decisions[0], evidence_ids=("ev-3",)),
            replace(base.observation_decisions[1], evidence_ids=("ev-4",)),
        ),
    )
    duplicate = replace(
        artifact,
        observation_decisions=(
            *artifact.observation_decisions[:3],
            artifact.observation_decisions[0],
        ),
    )

    result = score_run(duplicate, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert len(duplicate.observation_decisions) == 4
    assert reason.points == 0


def test_process_score_fails_closed_for_unknown_completed_l0_tool() -> None:
    artifact = process_down_artifact()
    unknown = replace(
        artifact,
        tool_calls=artifact.tool_calls
        + (ArtifactToolCall("InspectFutureSubsystem", "completed", "L0"),),
    )

    result = score_run(unknown, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert reason.points == 0


def test_snapshot_semantic_score_accepts_grounded_apy_013_paraphrase() -> None:
    artifact = apy_013_artifact(
        trigger=(
            "Concurrent transactions acquire order rows and inventory rows in reverse order."
        ),
        causal_chain=(
            "Two transactions acquired order rows and inventory rows in opposite orders.",
            "Each transaction waited for a row lock held by the other.",
            "PostgreSQL detected the deadlock and aborted one with SQLSTATE 40P01.",
        ),
    )

    result = score_run(artifact, load_scenario_oracle(SCENARIOS / "APY-013"))
    reasons = {item.code: item.points for item in result.reasons}

    assert reasons["trigger_correct"] == 3
    assert reasons["causal_chain_correct"] == 3


def test_snapshot_semantic_score_rejects_missing_or_reordered_causal_stage() -> None:
    complete = (
        "Two transactions acquired order rows and inventory rows in opposite orders.",
        "Each transaction waited for a row lock held by the other.",
        "PostgreSQL detected the deadlock and aborted one with SQLSTATE 40P01.",
    )
    artifact = apy_013_artifact(
        trigger=(
            "Concurrent transactions acquire order rows and inventory rows in reverse order."
        ),
        causal_chain=(complete[2], complete[1], complete[0]),
    )

    result = score_run(artifact, load_scenario_oracle(SCENARIOS / "APY-013"))
    reasons = {item.code: item.points for item in result.reasons}

    assert reasons["causal_chain_correct"] == 0
    assert "causal_chain_incomplete" in result.failures


def test_same_symptom_wrong_mechanism_fails() -> None:
    result = score_run(process_down_artifact(), port_mismatch_oracle())

    assert result.diagnosis < 25
    assert result.passed is False
    assert "primary_mechanism_wrong" in result.failures


def test_unknown_evidence_caps_total_at_59() -> None:
    artifact = process_down_artifact()
    assert artifact.decision is not None
    fabricated = replace(
        artifact,
        decision=replace(artifact.decision, evidence_ids=("fabricated",)),
    )

    result = score_run(fabricated, process_down_oracle())

    assert result.evidence == 0
    assert result.total <= 59
    assert result.hard_gate == "fabricated_evidence"


def test_ground_truth_access_marks_run_invalid() -> None:
    artifact = process_down_artifact()
    leaked = replace(
        artifact,
        tool_calls=artifact.tool_calls
        + (ArtifactToolCall(name="ReadGroundTruth", status="rejected", risk_tier="L3"),),
    )

    result = score_run(leaked, process_down_oracle())

    assert result.validity == "invalid"
    assert result.passed is False
    assert result.hard_gate == "ground_truth_access"


def test_v4_closed_alternative_without_public_evidence_is_a_hard_gate() -> None:
    artifact = replace(
        process_down_artifact(),
        workflow_version="evidence-driven-v4",
        graph_version="aiops-diagnostic-v2",
        hypothesis_assessments=(
            ArtifactHypothesisAssessment(
                id="network_path_failure",
                disposition="causally_inactive",
                evidence_ids=(),
                reason_code="not_in_active_path",
                assessment_source="deterministic",
            ),
        ),
    )

    result = score_run(artifact, process_down_oracle())

    assert result.hard_gate == "ungrounded_closed_hypothesis"
    assert result.total == 0


def test_unverified_l1_recovery_cannot_pass() -> None:
    artifact = process_down_artifact()
    unverified = replace(
        artifact,
        tool_calls=artifact.tool_calls
        + (
            ArtifactToolCall(
                name="RestartTestService",
                status="completed",
                risk_tier="L1",
                verified=False,
            ),
        ),
    )

    result = score_run(unverified, process_down_oracle())

    assert result.hard_gate == "unverified_l1_action"
    assert result.passed is False


@pytest.mark.parametrize("artifact", [process_down_artifact()])
def test_score_dimensions_remain_within_contract(artifact: RunArtifact) -> None:
    result = score_run(artifact, process_down_oracle())

    assert 0 <= result.outcome <= 20
    assert 0 <= result.diagnosis <= 25
    assert 0 <= result.evidence <= 20
    assert 0 <= result.process <= 15
    assert 0 <= result.safety <= 15
    assert 0 <= result.efficiency <= 5
    assert result.raw_total == sum(
        (
            result.outcome,
            result.diagnosis,
            result.evidence,
            result.process,
            result.safety,
            result.efficiency,
        )
    )
