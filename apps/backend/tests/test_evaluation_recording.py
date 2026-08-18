from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.evaluation.archive import EvaluationArchive, EvaluationArchiveError
from super_ai.evaluation.history import (
    artifact_checksum,
    interrupted_envelope,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.persistence import EvaluationDatabaseUnavailable
from super_ai.evaluation.recording import EvaluationRunRecorder

FIXED_TIME = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
RUNNING = running_envelope(
    run_id="eval-recorder",
    evaluation_kind="snapshot",
    scenario_id="APY-013",
    suite_version="v1",
    metadata={"gitSha": "abc", "workflowVersion": "v1", "ragMode": "off"},
    created_at=FIXED_TIME,
    started_at=FIXED_TIME,
)
PASSED = terminal_envelope(
    running=RUNNING,
    status="passed",
    validity="VALID_PASS",
    passed=True,
    metrics={"total": 100, "rawTotal": 100},
    result_payload={"failures": []},
    diagnostic_task_id="task-1",
    failure_category=None,
    completed_at=FIXED_TIME,
)


class FailingRepository:
    def __init__(self, *, fail_start: bool = False, fail_finish: bool = False) -> None:
        self.fail_start = fail_start
        self.fail_finish = fail_finish
        self.started = 0

    async def start_envelope(self, envelope: object) -> None:
        del envelope
        self.started += 1
        if self.fail_start:
            raise EvaluationDatabaseUnavailable("unavailable")

    async def finalize_envelope(self, envelope: object, *, artifact_checksum: str) -> None:
        del envelope, artifact_checksum
        if self.fail_finish:
            raise EvaluationDatabaseUnavailable("unavailable")


class FailingArchive:
    def start(self, envelope: object) -> Path:
        del envelope
        raise EvaluationArchiveError("archive unavailable")

    def finalize(self, envelope: object) -> Path:
        del envelope
        raise AssertionError("finalize must not be called")


def archive(tmp_path: Path) -> EvaluationArchive:
    return EvaluationArchive(tmp_path / "archive", repository_root=tmp_path / "repository")


@pytest.mark.asyncio
async def test_recorder_keeps_artifact_when_database_finalize_fails(tmp_path: Path) -> None:
    local_archive = archive(tmp_path)
    repository = FailingRepository(fail_finish=True)
    recorder = EvaluationRunRecorder(archive=local_archive, repository=repository)

    await recorder.start(RUNNING)
    outcome = await recorder.finish(PASSED)

    assert outcome.database_pending is True
    assert local_archive.load(RUNNING.run_id) == PASSED
    assert repository.started == 2


@pytest.mark.asyncio
async def test_recorder_continues_when_database_start_fails(tmp_path: Path) -> None:
    local_archive = archive(tmp_path)
    recorder = EvaluationRunRecorder(
        archive=local_archive,
        repository=FailingRepository(fail_start=True),
    )

    start_outcome = await recorder.start(RUNNING)
    finish_outcome = await recorder.finish(PASSED)

    assert start_outcome.database_pending is True
    assert finish_outcome.database_pending is True
    assert local_archive.load(RUNNING.run_id) == PASSED


@pytest.mark.asyncio
async def test_recorder_archive_failure_stops_before_database() -> None:
    repository = FailingRepository()
    recorder = EvaluationRunRecorder(archive=FailingArchive(), repository=repository)

    with pytest.raises(EvaluationArchiveError):
        await recorder.start(RUNNING)

    assert repository.started == 0


def test_recorder_uses_canonical_terminal_checksum(tmp_path: Path) -> None:
    assert len(artifact_checksum(PASSED)) == 64


def test_keyboard_interrupt_maps_to_safe_interrupted_envelope() -> None:
    envelope = interrupted_envelope(RUNNING, completed_at=FIXED_TIME)
    assert envelope.status == "interrupted"
    assert envelope.failure_category == "operator_interrupt"
