from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import cast

import pytest

from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolResult,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_retrieval_benchmark.py"
SPEC = importlib.util.spec_from_file_location("run_retrieval_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeRetrievalTool:
    def __init__(
        self,
        *,
        wrong_owner: bool = False,
        omit_citation: bool = False,
        bm25_only: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self._wrong_owner = wrong_owner
        self._omit_citation = omit_citation
        self._bm25_only = bm25_only

    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: tuple[str, ...],
    ) -> KnowledgeRetrievalToolResult:
        self.calls.append((input.query, owner_user_id, accessible_knowledge_base_ids))
        source = (
            "postgres-pool-exhaustion.md"
            if len(self.calls) <= 3
            else "redis-unavailable.md"
        )
        actual_owner = "other-owner" if self._wrong_owner else owner_user_id
        hit = KnowledgeRetrievalHit(
            chunk_id=f"chunk-{len(self.calls)}",
            document_id=f"doc-{len(self.calls)}",
            knowledge_base_id=accessible_knowledge_base_ids[0],
            owner_user_id=actual_owner,
            tenant_id=actual_owner,
            content="secret chunk content",
            source=source,
            metadata={},
            score=0.9,
            vector_rank=None if self._bm25_only else 1,
            bm25_rank=1,
            rerank_rank=1,
            vector_score=None if self._bm25_only else 0.8,
            bm25_score=2.1,
            rrf_score=0.016,
            rerank_score=0.9,
        )
        citation = KnowledgeRetrievalCitationSource(
            id=hit.chunk_id,
            title=source,
            source_type="knowledge-base",
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            knowledge_base_id=hit.knowledge_base_id,
            source=source,
            metadata={},
            score=0.9,
            excerpt="secret chunk content",
            knowledge_type="document",
            vector_rank=hit.vector_rank,
            bm25_rank=hit.bm25_rank,
            rerank_rank=hit.rerank_rank,
            vector_score=hit.vector_score,
            bm25_score=hit.bm25_score,
            rrf_score=hit.rrf_score,
            rerank_score=0.9,
        )
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=cast(int, input.top_k),
            results=[hit],
            citations=[] if self._omit_citation else [citation],
        )


@pytest.mark.asyncio
async def test_run_queries_uses_explicit_scope_and_returns_safe_report() -> None:
    tool = FakeRetrievalTool()

    payload = await MODULE.run_queries(
        tool,
        owner_user_id="owner-a",
        knowledge_base_id="kb-owner-a",
        queries_path=MODULE.DEFAULT_QUERIES,
        model_configuration={"embeddingModel": "embed", "rerankModel": "rerank"},
    )

    assert len(tool.calls) == 64
    assert all(call[1:] == ("owner-a", ("kb-owner-a",)) for call in tool.calls)
    assert payload["metrics"]["queryCount"] == 64
    assert payload["metrics"]["answerableQueryCount"] == 58
    assert payload["metrics"]["noAnswerProbeCount"] == 6
    serialized = json.dumps(payload)
    assert "secret chunk content" not in serialized
    assert "apiKey" not in serialized
    assert payload["runs"][0]["hits"][0] == {
        "source": "postgres-pool-exhaustion.md",
        "chunkId": "chunk-1",
        "documentId": "doc-1",
        "knowledgeBaseId": "kb-owner-a",
        "vectorRank": 1,
        "bm25Rank": 1,
        "rerankRank": 1,
        "vectorScore": 0.8,
        "bm25Score": 2.1,
        "rrfScore": 0.016,
        "rerankScore": 0.9,
        "retrievalChannels": ["vector", "bm25"],
    }
    assert payload["metrics"]["vectorChannelCoverageRate"] == 1.0
    assert payload["metrics"]["bm25ChannelCoverageRate"] == 1.0
    assert payload["metrics"]["hybridChannelCoverageRate"] == 1.0


@pytest.mark.asyncio
async def test_bm25_only_hit_has_complete_channel_aware_citation() -> None:
    payload = await MODULE.run_queries(
        FakeRetrievalTool(bm25_only=True),
        owner_user_id="owner-a",
        knowledge_base_id="kb-owner-a",
        queries_path=MODULE.DEFAULT_QUERIES,
        model_configuration={},
    )

    hit = payload["runs"][0]["hits"][0]
    assert hit["vectorRank"] is None
    assert hit["vectorScore"] is None
    assert hit["retrievalChannels"] == ["bm25"]
    assert payload["metrics"]["citationCompletenessRate"] == 1.0


@pytest.mark.asyncio
async def test_run_queries_rejects_cross_tenant_hit() -> None:
    with pytest.raises(ValueError, match="scope"):
        await MODULE.run_queries(
            FakeRetrievalTool(wrong_owner=True),
            owner_user_id="owner-a",
            knowledge_base_id="kb-owner-a",
            queries_path=MODULE.DEFAULT_QUERIES,
            model_configuration={},
        )


def test_parser_requires_explicit_owner_and_knowledge_base() -> None:
    parser = MODULE.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_exit_gate_uses_approved_answerable_thresholds() -> None:
    passing = {
        "metrics": {
            "recallAt1": 0.80,
            "recallAt3": 0.95,
            "mrr": 0.85,
            "forbiddenTopOneRate": 0.05,
            "citationCompletenessRate": 1.0,
        }
    }

    assert MODULE._passes(passing) is True
    for metric, failing_value in (
        ("recallAt1", 0.79),
        ("recallAt3", 0.94),
        ("mrr", 0.84),
        ("forbiddenTopOneRate", 0.06),
        ("citationCompletenessRate", 0.99),
    ):
        failing = json.loads(json.dumps(passing))
        failing["metrics"][metric] = failing_value
        assert MODULE._passes(failing) is False


@pytest.mark.asyncio
async def test_no_answer_probe_reports_top_one_score_and_margin() -> None:
    payload = await MODULE.run_queries(
        FakeRetrievalTool(),
        owner_user_id="owner-a",
        knowledge_base_id="kb-owner-a",
        queries_path=MODULE.DEFAULT_QUERIES,
        model_configuration={},
    )

    probe = next(run for run in payload["runs"] if run["queryId"] == "RET-N-001")
    assert probe["expectedNoAnswer"] is True
    assert probe["topOneScore"] == 0.9
    assert probe["topTwoMargin"] is None


@pytest.mark.asyncio
async def test_missing_citation_counts_as_incomplete_instead_of_disappearing() -> None:
    payload = await MODULE.run_queries(
        FakeRetrievalTool(omit_citation=True),
        owner_user_id="owner-a",
        knowledge_base_id="kb-owner-a",
        queries_path=MODULE.DEFAULT_QUERIES,
        model_configuration={},
    )

    assert payload["metrics"]["citationCompletenessRate"] == 0.0
