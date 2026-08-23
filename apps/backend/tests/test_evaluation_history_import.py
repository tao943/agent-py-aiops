import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history import EvaluationRunEnvelope
from super_ai.evaluation.history_import import (
    _database_envelope,  # pyright: ignore[reportPrivateUsage]
    import_history,
)
from super_ai.memory.repositories import EvaluationResultRecord, EvaluationRunRecord


class Repository:
    async def start_envelope(self, envelope: EvaluationRunEnvelope) -> None:
        del envelope

    async def finalize_envelope(
        self, envelope: EvaluationRunEnvelope, *, artifact_checksum: str
    ) -> None:
        del envelope, artifact_checksum

    async def attach_artifact_checksum(
        self, *, run_id: str, artifact_checksum: str
    ) -> None:
        del run_id, artifact_checksum

    async def list_runs_with_results(
        self,
    ) -> list[tuple[EvaluationRunRecord, EvaluationResultRecord | None]]:
        return []


def test_database_reconstruction_preserves_v1_schema_version() -> None:
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    run = EvaluationRunRecord(
        run_id="legacy-v1",
        evaluation_kind="retrieval",
        artifact_schema_version="v1",
        artifact_checksum=None,
        provenance="native",
        run_metadata={"datasetChecksum": "a" * 64},
        scenario_id="retrieval-suite",
        mode="retrieval",
        suite_version="v1",
        agent_version={},
        model_configuration={},
        status="running",
        failure_category=None,
        diagnostic_task_id=None,
        created_at=now,
        started_at=now,
        completed_at=None,
    )

    envelope = _database_envelope(run, None)

    assert envelope.artifact_schema_version == "v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["groundTruth", "primaryCause", "answer_key", "chainOfThought"])
async def test_importer_rejects_hidden_answer_key_variants(tmp_path: Path, key: str) -> None:
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps({"metrics": {}, "metadata": {"nested": {key: "hidden"}}}),
        encoding="utf-8",
    )
    archive = EvaluationArchive(
        tmp_path / "archive", repository_root=tmp_path / "repository"
    )
    report = await import_history(
        sources=[source], archive=archive, repository=Repository()
    )
    assert report.rejected == 1
    assert report.imported == 0


@pytest.mark.asyncio
async def test_same_run_same_checksum_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "retrieval.json"
    metrics = {
        "queryCount": 1,
        "answerableQueryCount": 1,
        "noAnswerProbeCount": 0,
        "recallAt1": 1.0,
        "recallAt3": 1.0,
        "mrr": 1.0,
        "forbiddenTopOneRate": 0.0,
        "citationCompletenessRate": 1.0,
        "vectorChannelCoverageRate": 1.0,
        "bm25ChannelCoverageRate": 1.0,
        "hybridChannelCoverageRate": 1.0,
    }
    source.write_text(
        json.dumps(
            {
                "runId": "import-retrieval",
                "ownerUserId": "owner",
                "knowledgeBaseId": "kb",
                "models": {},
                "metrics": metrics,
            }
        ),
        encoding="utf-8",
    )
    archive = EvaluationArchive(
        tmp_path / "archive", repository_root=tmp_path / "repository"
    )
    first = await import_history(
        sources=[source], archive=archive, repository=Repository()
    )
    second = await import_history(
        sources=[source], archive=archive, repository=Repository()
    )
    assert first.imported == 1
    assert second.duplicates == 1
