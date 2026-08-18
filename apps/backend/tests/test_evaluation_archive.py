from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history import EvaluationStatus, running_envelope, terminal_envelope
from super_ai.project_config import ProjectConfigurationError

FIXED_TIME = datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc)


def _running():
    return running_envelope(
        run_id="eval-1",
        evaluation_kind="snapshot",
        scenario_id="APY-013",
        suite_version="v1",
        metadata={"gitSha": "abc123", "ragMode": "on"},
        created_at=FIXED_TIME,
        started_at=FIXED_TIME,
    )


def _terminal(*, status: EvaluationStatus, passed: bool):
    return terminal_envelope(
        running=_running(),
        status=status,
        validity="VALID_PASS" if passed else "VALID_FAIL",
        passed=passed,
        metrics={
            "outcome": 20,
            "diagnosis": 20,
            "evidence": 20,
            "process": 15,
            "safety": 15,
            "efficiency": 10,
            "total": 100,
            "rawTotal": 100,
        },
        result_payload={"failures": [], "scoreReasons": [], "hardGate": None},
        diagnostic_task_id="diagnostic-1",
        failure_category=None,
        completed_at=FIXED_TIME,
    )


def test_archive_requires_absolute_path_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    with pytest.raises(ProjectConfigurationError, match="outside"):
        EvaluationArchive(repository / "var", repository_root=repository)

    with pytest.raises(ProjectConfigurationError, match="absolute"):
        EvaluationArchive(Path("relative/archive"), repository_root=repository)


def test_archive_from_config_reads_user_override_only_from_json(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    config_dir = repository / "config"
    config_dir.mkdir(parents=True)
    archive_root = tmp_path / "shared-archive"
    (config_dir / "project.json").write_text(
        json.dumps({"evaluation": {"archiveDir": ""}}),
        encoding="utf-8",
    )
    (config_dir / "user.project.json").write_text(
        json.dumps({"evaluation": {"archiveDir": str(archive_root)}}),
        encoding="utf-8",
    )

    archive = EvaluationArchive.from_config(
        config_path=config_dir / "project.json",
        repository_root=repository,
    )

    assert archive.root == archive_root.resolve()


def test_archive_advances_running_to_terminal_once_atomically(tmp_path: Path) -> None:
    archive = EvaluationArchive(tmp_path / "archive", repository_root=tmp_path / "repo")
    running = _running()
    passed = _terminal(status="passed", passed=True)
    failed = _terminal(status="failed", passed=False)

    archive.start(running)
    archive.finalize(passed)

    assert archive.load("eval-1") == passed
    assert list(archive.root.rglob("*.tmp")) == []
    assert archive.finalize(passed) == archive.path_for(passed)
    with pytest.raises(ValueError, match="terminal"):
        archive.finalize(failed)


def test_archive_rejects_run_id_path_traversal(tmp_path: Path) -> None:
    archive = EvaluationArchive(tmp_path / "archive", repository_root=tmp_path / "repo")

    with pytest.raises(ValueError, match="run ID"):
        archive.load("../APY-013")


def test_archive_iterates_canonical_envelopes(tmp_path: Path) -> None:
    archive = EvaluationArchive(tmp_path / "archive", repository_root=tmp_path / "repo")
    archive.start(_running())

    assert list(archive.iter_envelopes()) == [_running()]
