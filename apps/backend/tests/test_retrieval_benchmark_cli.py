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
    def __init__(self, *, wrong_owner: bool = False) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self._wrong_owner = wrong_owner

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
            vector_score=0.8,
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
            vector_score=0.8,
            rerank_score=0.9,
        )
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=cast(int, input.top_k),
            results=[hit],
            citations=[citation],
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

    assert len(tool.calls) == 6
    assert all(call[1:] == ("owner-a", ("kb-owner-a",)) for call in tool.calls)
    assert payload["metrics"]["recallAt3"] == 1.0
    serialized = json.dumps(payload)
    assert "secret chunk content" not in serialized
    assert "apiKey" not in serialized
    assert payload["runs"][0]["hits"][0] == {
        "source": "postgres-pool-exhaustion.md",
        "chunkId": "chunk-1",
        "documentId": "doc-1",
        "knowledgeBaseId": "kb-owner-a",
        "vectorScore": 0.8,
        "rerankScore": 0.9,
    }


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
