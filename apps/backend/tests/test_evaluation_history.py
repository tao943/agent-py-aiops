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
    assert envelope.to_json()["artifactSchemaVersion"] == "v1"


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
