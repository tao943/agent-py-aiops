import json
from pathlib import Path

import pytest

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history_import import import_history


class Repository:
    async def start_envelope(self, envelope):
        del envelope

    async def finalize_envelope(self, envelope, *, artifact_checksum):
        del envelope, artifact_checksum

    async def attach_artifact_checksum(self, *, run_id, artifact_checksum):
        del run_id, artifact_checksum


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
