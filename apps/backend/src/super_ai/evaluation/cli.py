"""Pure CLI serialization contracts for AgentPy benchmark runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from super_ai.evaluation.runner import BenchmarkRunError
from super_ai.evaluation.scoring import EvaluationResult
from super_ai.memory.repositories import EVALUATION_FAILURE_CATEGORIES


def evaluation_result_payload(
    *,
    scenario_id: str,
    run_id: str,
    duration_ms: int,
    result: EvaluationResult,
) -> dict[str, object]:
    """Serialize one deterministic scorecard without private evaluator data."""
    return {
        "scenario": scenario_id,
        "runId": run_id,
        "dimensions": {
            "outcome": result.outcome,
            "diagnosis": result.diagnosis,
            "evidence": result.evidence,
            "process": result.process,
            "safety": result.safety,
            "efficiency": result.efficiency,
        },
        "rawTotal": result.raw_total,
        "total": result.total,
        "validity": result.validity,
        "passed": result.passed,
        "failures": list(result.failures),
        "hardGate": result.hard_gate,
        "durationMs": duration_ms,
        "scoreReasons": [
            {
                "code": reason.code,
                "points": reason.points,
                "maximum": reason.maximum,
                "evidenceIds": list(reason.evidence_ids),
            }
            for reason in result.reasons
        ],
    }


def evaluation_exit_code(results: Sequence[Mapping[str, object]]) -> int:
    """Return 0 for pass, 1 for valid failure, and 2 for invalid execution."""
    if any(result.get("validity") == "invalid" for result in results):
        return 2
    if all(result.get("passed") is True for result in results):
        return 0
    return 1


def safe_failure_payload(error: BaseException) -> dict[str, object]:
    """Return fixed public failure metadata without exception or cause text."""
    payload: dict[str, object] = {
        "validity": "invalid",
        "passed": False,
        "error": "Snapshot benchmark execution failed.",
        "category": "infrastructure_error",
    }
    if (
        isinstance(error, BenchmarkRunError)
        and error.category in EVALUATION_FAILURE_CATEGORIES
    ):
        payload["category"] = error.category
        payload["status"] = error.status
    return payload
