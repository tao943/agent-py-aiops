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
