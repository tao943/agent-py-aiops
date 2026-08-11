from __future__ import annotations

import json

from super_ai.evaluation.cli import (
    evaluation_exit_code,
    evaluation_result_payload,
    safe_failure_payload,
)
from super_ai.evaluation.runner import BenchmarkRunError
from super_ai.evaluation.scoring import EvaluationResult, ScoreReason


def passing_result() -> EvaluationResult:
    return EvaluationResult(
        outcome=20,
        diagnosis=25,
        evidence=20,
        process=15,
        safety=15,
        efficiency=5,
        raw_total=100,
        total=100,
        validity="valid",
        passed=True,
        failures=(),
        hard_gate=None,
        reasons=(ScoreReason("primary_mechanism_correct", 10, 10, ("ev-1",)),),
    )


def test_evaluation_exit_code_distinguishes_pass_fail_and_invalid() -> None:
    assert evaluation_exit_code([{"validity": "valid", "passed": True}]) == 0
    assert evaluation_exit_code([{"validity": "valid", "passed": False}]) == 1
    assert evaluation_exit_code([{"validity": "invalid", "passed": False}]) == 2


def test_evaluation_result_payload_serializes_the_public_scorecard() -> None:
    payload = evaluation_result_payload(
        scenario_id="APY-003",
        run_id="run-cli",
        duration_ms=125,
        result=passing_result(),
    )

    assert payload["scenario"] == "APY-003"
    assert payload["runId"] == "run-cli"
    assert payload["validity"] == "valid"
    assert payload["passed"] is True
    assert payload["durationMs"] == 125


def test_safe_failure_payload_never_serializes_unknown_exception_text() -> None:
    error = RuntimeError("api-key-secret ground_truth C:\\private\\oracle.yaml")

    payload = safe_failure_payload(error)
    serialized = json.dumps(payload)

    assert payload["validity"] == "invalid"
    assert payload["category"] == "infrastructure_error"
    assert "api-key-secret" not in serialized
    assert "ground_truth" not in serialized
    assert "C:\\private" not in serialized


def test_safe_failure_payload_preserves_only_classified_benchmark_category() -> None:
    classified = BenchmarkRunError("agent_failed", "artifact_invalid")
    classified.__cause__ = RuntimeError("classified-secret-sentinel")

    payload = safe_failure_payload(classified)
    serialized = json.dumps(payload)

    assert payload["category"] == "artifact_invalid"
    assert payload["status"] == "agent_failed"
    assert "classified-secret-sentinel" not in serialized


def test_safe_failure_payload_rejects_unrecognized_benchmark_category() -> None:
    error = BenchmarkRunError("infra_failed", "secret-category")

    payload = safe_failure_payload(error)

    assert payload["category"] == "infrastructure_error"
