from __future__ import annotations

from datetime import datetime, timezone

import pytest

from super_ai.evaluation.history import (
    artifact_checksum,
    running_envelope,
    terminal_envelope,
)

FIXED_TIME = datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc)


def _running(*, metadata: dict[str, object] | None = None):
    return running_envelope(
        run_id="eval-1",
        evaluation_kind="retrieval",
        scenario_id="retrieval-64",
        suite_version="v1",
        metadata=metadata or {"gitSha": "abc123", "datasetChecksum": "d" * 64},
        created_at=FIXED_TIME,
        started_at=FIXED_TIME,
    )


def _running_live():  # type: ignore[no-untyped-def]
    return running_envelope(
        run_id="live-ab-1",
        evaluation_kind="live",
        scenario_id="APY-LIVE-PG-LOCK-001",
        suite_version="v1",
        metadata={
            "gitSha": "abc123",
            "workflowVersion": "evidence-driven-v4",
            "investigationStrategy": "multi_agent",
            "investigationPolicyVersion": "investigation-router-v1",
        },
        created_at=FIXED_TIME,
        started_at=FIXED_TIME,
    )


def test_investigation_metrics_are_flat_allowlisted_and_round_trip() -> None:
    envelope = terminal_envelope(
        running=_running_live(),
        status="passed",
        validity="VALID_PASS",
        passed=True,
        metrics={
            "total": 96,
            "rootCauseTop1Correct": True,
            "evidenceRecallBasisPoints": 9000,
            "durationMs": 1234,
            "modelCallCount": 3,
            "duplicateEvidenceBasisPoints": 0,
            "securityHardGatePassed": True,
        },
        result_payload={"failures": [], "hardGate": None},
        diagnostic_task_id="diagnostic-1",
        failure_category=None,
        completed_at=FIXED_TIME,
    )

    restored = type(envelope).from_json(envelope.to_json())

    assert restored == envelope
    assert artifact_checksum(restored) == artifact_checksum(envelope)


def _live_failure_result_payload() -> dict[str, object]:
    return {
        "failures": ["fault_injection_failed"],
        "failureStage": "inject",
        "checkResults": [
            {"name": "pool_at_capacity", "passed": True, "source": "driver"},
            {
                "name": "business_probe_timed_out",
                "passed": False,
                "source": "driver",
            },
        ],
        "failedChecks": ["business_probe_timed_out"],
        "safeFacts": {"poolCapacity": 3, "businessProbeTimedOut": False},
    }


def test_live_failure_diagnostics_are_structured_and_round_trip_in_v2() -> None:
    envelope = terminal_envelope(
        running=_running_live(),
        status="failed",
        validity="VALID_FAIL",
        passed=False,
        metrics={"cleanupSucceeded": True},
        result_payload=_live_failure_result_payload(),
        diagnostic_task_id=None,
        failure_category="fault_injection_failed",
        completed_at=FIXED_TIME,
    )

    restored = type(envelope).from_json(envelope.to_json())

    assert restored == envelope
    assert artifact_checksum(restored) == artifact_checksum(envelope)
    assert restored.artifact_schema_version == "v2"


def _invalid_live_failure_payloads() -> tuple[dict[str, object], ...]:
    missing_field = _live_failure_result_payload()
    missing_field.pop("safeFacts")
    extra_check_key = _live_failure_result_payload()
    extra_check_key["checkResults"] = [
        {"name": "probe_failed", "passed": False, "source": "driver", "detail": "no"}
    ]
    duplicate_checks = _live_failure_result_payload()
    duplicate_checks["checkResults"] = [
        {"name": "duplicate", "passed": False, "source": "driver"},
        {"name": "duplicate", "passed": True, "source": "driver"},
    ]
    inconsistent_failed = _live_failure_result_payload()
    inconsistent_failed["failedChecks"] = ["pool_at_capacity"]
    reordered_failed = _live_failure_result_payload()
    reordered_failed["checkResults"] = [
        {"name": "first_failed", "passed": False, "source": "driver"},
        {"name": "second_failed", "passed": False, "source": "driver"},
    ]
    reordered_failed["failedChecks"] = ["second_failed", "first_failed"]
    non_scalar_fact = _live_failure_result_payload()
    non_scalar_fact["safeFacts"] = {"nested": {"message": "no"}}
    too_many_checks = _live_failure_result_payload()
    too_many_checks["checkResults"] = [
        {"name": f"check_{index}", "passed": False, "source": "driver"}
        for index in range(65)
    ]
    too_many_checks["failedChecks"] = [f"check_{index}" for index in range(65)]
    too_many_facts = _live_failure_result_payload()
    too_many_facts["safeFacts"] = {f"fact_{index}": index for index in range(65)}
    non_finite_fact = _live_failure_result_payload()
    non_finite_fact["safeFacts"] = {"ratio": float("nan")}
    forbidden_identifier = _live_failure_result_payload()
    forbidden_identifier["safeFacts"] = {"ground_truth": "hidden"}
    missing_check_key = _live_failure_result_payload()
    missing_check_key["checkResults"] = [
        {"name": "probe_failed", "passed": False}
    ]
    zero_checks = _live_failure_result_payload()
    zero_checks["checkResults"] = []
    zero_checks["failedChecks"] = []
    non_mapping_facts = _live_failure_result_payload()
    non_mapping_facts["safeFacts"] = ["invalid"]
    overlong_fact = _live_failure_result_payload()
    overlong_fact["safeFacts"] = {"message": "x" * 257}
    nested_failed_checks = _live_failure_result_payload()
    nested_failed_checks["failedChecks"] = [{"message": "invalid"}]
    return (
        missing_field,
        extra_check_key,
        duplicate_checks,
        inconsistent_failed,
        reordered_failed,
        non_scalar_fact,
        too_many_checks,
        too_many_facts,
        non_finite_fact,
        forbidden_identifier,
        missing_check_key,
        zero_checks,
        non_mapping_facts,
        overlong_fact,
        nested_failed_checks,
    )


@pytest.mark.parametrize("result_payload", _invalid_live_failure_payloads())
def test_live_failure_diagnostics_reject_malformed_or_inconsistent_structures(
    result_payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="failure diagnostic|forbidden"):
        terminal_envelope(
            running=_running_live(),
            status="failed",
            validity="VALID_FAIL",
            passed=False,
            metrics={"cleanupSucceeded": True},
            result_payload=result_payload,
            diagnostic_task_id=None,
            failure_category="fault_injection_failed",
            completed_at=FIXED_TIME,
        )


def test_terminal_envelope_checksum_is_stable_after_round_trip() -> None:
    envelope = terminal_envelope(
        running=_running(),
        status="failed",
        validity="VALID_FAIL",
        passed=False,
        metrics={"recallAt1": 0.75, "recallAt3": 0.95},
        result_payload={"failures": ["recall_at_1_below_threshold"]},
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=FIXED_TIME,
    )

    restored = type(envelope).from_json(envelope.to_json())

    assert restored == envelope
    assert artifact_checksum(restored) == artifact_checksum(envelope)
    assert envelope.to_json()["artifactSchemaVersion"] == "v2"


def test_v1_artifact_remains_readable_after_v2_upgrade() -> None:
    payload = terminal_envelope(
        running=_running(),
        status="passed",
        validity="VALID_PASS",
        passed=True,
        metrics={"recallAt1": 1.0},
        result_payload={"failures": []},
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=FIXED_TIME,
    ).to_json()
    payload["artifactSchemaVersion"] = "v1"

    restored = type(_running()).from_json(payload)

    assert restored.artifact_schema_version == "v1"
    assert restored.evaluation_kind == "retrieval"


def test_v2_conversation_model_and_live_conversation_metrics_round_trip() -> None:
    model_running = running_envelope(
        run_id="conversation-model-1",
        evaluation_kind="conversation_model",
        scenario_id="conversation-model-suite",
        suite_version="conversation-model-v1",
        metadata={
            "gitSha": "abc123",
            "workflowVersion": "conversation-model-v1",
            "modelConfiguration": {"model": "fake-model"},
            "scenarioVersion": "conversation-model-v1",
        },
        created_at=FIXED_TIME,
        started_at=FIXED_TIME,
    )
    model_terminal = terminal_envelope(
        running=model_running,
        status="passed",
        validity="VALID_PASS",
        passed=True,
        metrics={
            "scenarioCount": 6,
            "passedScenarioCount": 6,
            "routeAccuracy": 1.0,
            "structuredInterpretationAccuracy": 1.0,
            "degradedFallbackAccuracy": 1.0,
            "promptInjectionSafety": 1.0,
            "modelCallCount": 4,
            "providerCallCount": 4,
            "modelBoundaryAttemptCount": 5,
            "scenarioAttemptCount": 6,
            "injectedFailureCount": 1,
        },
        result_payload={
            "failures": [],
            "scenarioResults": [],
            "safetyCategories": ["prompt_injection_resisted"],
        },
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=FIXED_TIME,
    )
    live_terminal = terminal_envelope(
        running=_running_live(),
        status="passed",
        validity="VALID_PASS",
        passed=True,
        metrics={
            "total": 100,
            "conversationMetrics": {
                "routeAccuracy": 1.0,
                "confirmationAccuracy": 1.0,
            },
        },
        result_payload={"failures": []},
        diagnostic_task_id="diagnostic-1",
        failure_category=None,
        completed_at=FIXED_TIME,
    )

    assert type(model_terminal).from_json(model_terminal.to_json()) == model_terminal
    assert type(live_terminal).from_json(live_terminal.to_json()) == live_terminal


def test_v1_rejects_conversation_model_kind() -> None:
    payload = {
        **running_envelope(
            run_id="conversation-model-v1-invalid",
            evaluation_kind="conversation_model",
            scenario_id="conversation-model-suite",
            suite_version="conversation-model-v1",
            metadata={
                "gitSha": "abc123",
                "workflowVersion": "conversation-model-v1",
                "modelConfiguration": {"model": "fake-model"},
                "scenarioVersion": "conversation-model-v1",
            },
            created_at=FIXED_TIME,
            started_at=FIXED_TIME,
        ).to_json(),
        "artifactSchemaVersion": "v1",
    }

    with pytest.raises(ValueError, match="schema version"):
        type(_running()).from_json(payload)


@pytest.mark.parametrize(
    "key",
    [
        "apiKey",
        "secret_key",
        "password",
        "token",
        "oracle",
        "ground_truth",
        "groundTruth",
        "primary_cause",
        "primaryCause",
        "answer-key",
        "chainOfThought",
    ],
)
@pytest.mark.parametrize("container", ["metadata", "metrics", "result_payload"])
def test_envelope_rejects_forbidden_recursive_keys(key: str, container: str) -> None:
    values: dict[str, dict[str, object]] = {
        "metadata": {"gitSha": "abc123", "datasetChecksum": "d" * 64},
        "metrics": {"recallAt1": 0.8},
        "result_payload": {"failures": []},
    }
    values[container] = {"nested": {key: "must-not-persist"}}

    with pytest.raises(ValueError, match="forbidden"):
        terminal_envelope(
            running=_running(metadata=values["metadata"]),
            status="failed",
            validity="VALID_FAIL",
            passed=False,
            metrics=values["metrics"],
            result_payload=values["result_payload"],
            diagnostic_task_id=None,
            failure_category=None,
            completed_at=FIXED_TIME,
        )


def test_envelope_rejects_unknown_kind_specific_field() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        terminal_envelope(
            running=_running(),
            status="failed",
            validity="VALID_FAIL",
            passed=False,
            metrics={"verificationPassed": True},
            result_payload={"failures": []},
            diagnostic_task_id=None,
            failure_category=None,
            completed_at=FIXED_TIME,
        )


def test_running_envelope_requires_utc_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone"):
        running_envelope(
            run_id="eval-1",
            evaluation_kind="snapshot",
            scenario_id="APY-013",
            suite_version="v1",
            metadata={"gitSha": "abc123"},
            created_at=datetime(2026, 8, 17, 8, 30),
            started_at=FIXED_TIME,
        )
