"""Run the reviewed retrieval benchmark against configured real services."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import cast

from super_ai.evaluation.retrieval import (
    RetrievalCitationAudit,
    RetrievalQueryResult,
    evaluate_retrieval,
    load_retrieval_queries,
)
from super_ai.llm import build_default_llm_provider, load_llm_provider_config
from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalTool,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolRunner,
)
from super_ai.vector_store import build_default_milvus_vector_store

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUERIES = REPOSITORY_ROOT / "benchmarks" / "agentpy" / "retrieval" / "queries.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the manual AgentPy knowledge retrieval benchmark."
    )
    parser.add_argument("--owner-user-id", required=True, help="Document owner user ID.")
    parser.add_argument("--knowledge-base-id", required=True, help="Authorized knowledge-base ID.")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--output", type=Path, help="Optional UTF-8 JSON report path.")
    parser.add_argument("--config", type=Path, help="Optional project configuration path.")
    return parser


async def run_queries(
    tool: KnowledgeRetrievalToolRunner,
    *,
    owner_user_id: str,
    knowledge_base_id: str,
    queries_path: Path,
    model_configuration: Mapping[str, str],
) -> dict[str, object]:
    """Run labels sequentially and return a content-free, tenant-safe report."""
    labels = load_retrieval_queries(queries_path)
    runs: list[dict[str, object]] = []
    scored: list[RetrievalQueryResult] = []
    for label in labels:
        started_at = monotonic()
        result = await tool.run(
            KnowledgeRetrievalToolInput(query=label.query, top_k=label.acceptable_top_k),
            owner_user_id=owner_user_id,
            accessible_knowledge_base_ids=(knowledge_base_id,),
        )
        _validate_scope(
            hits=result.results,
            citations=result.citations,
            owner_user_id=owner_user_id,
            knowledge_base_id=knowledge_base_id,
        )
        citations_by_chunk = {citation.chunk_id: citation for citation in result.citations}
        audits = tuple(
            RetrievalCitationAudit(
                chunk_id=hit.chunk_id,
                document_id=citation.document_id if citation is not None else hit.document_id,
                knowledge_base_id=(
                    citation.knowledge_base_id if citation is not None else hit.knowledge_base_id
                ),
                vector_score=citation.vector_score if citation is not None else None,
                rerank_score=citation.rerank_score if citation is not None else None,
                vector_rank=citation.vector_rank if citation is not None else None,
                bm25_rank=citation.bm25_rank if citation is not None else None,
                rerank_rank=citation.rerank_rank if citation is not None else None,
                bm25_score=citation.bm25_score if citation is not None else None,
                rrf_score=citation.rrf_score if citation is not None else None,
            )
            for hit in result.results
            for citation in (citations_by_chunk.get(hit.chunk_id),)
        )
        ranked_documents = tuple(Path(hit.source).name for hit in result.results)
        scored.append(
            RetrievalQueryResult(
                query_id=label.id,
                relevant_documents=label.relevant_documents,
                forbidden_top_one=label.forbidden_top_one,
                ranked_documents=ranked_documents,
                citations=audits,
                expected_no_answer=label.expected_no_answer,
            )
        )
        runs.append(
            {
                "queryId": label.id,
                "expectedNoAnswer": label.expected_no_answer,
                "durationMs": round((monotonic() - started_at) * 1_000),
                "topOneScore": _rerank_score_at(result.results, 0),
                "topTwoMargin": _top_two_margin(result.results),
                "hits": [
                    {
                        "source": Path(hit.source).name,
                        "chunkId": hit.chunk_id,
                        "documentId": hit.document_id,
                        "knowledgeBaseId": hit.knowledge_base_id,
                        "vectorRank": hit.vector_rank,
                        "bm25Rank": hit.bm25_rank,
                        "rerankRank": hit.rerank_rank,
                        "vectorScore": hit.vector_score,
                        "bm25Score": hit.bm25_score,
                        "rrfScore": hit.rrf_score,
                        "rerankScore": hit.rerank_score,
                        "retrievalChannels": _retrieval_channels(hit),
                    }
                    for hit in result.results
                ],
            }
        )
    metrics = evaluate_retrieval(scored)
    return {
        "ownerUserId": owner_user_id,
        "knowledgeBaseId": knowledge_base_id,
        "models": dict(model_configuration),
        "runs": runs,
        "metrics": {
            "queryCount": metrics.query_count,
            "answerableQueryCount": metrics.answerable_query_count,
            "noAnswerProbeCount": metrics.no_answer_probe_count,
            "recallAt1": metrics.recall_at_1,
            "recallAt3": metrics.recall_at_3,
            "mrr": metrics.mrr,
            "forbiddenTopOneRate": metrics.forbidden_top_one_rate,
            "citationCompletenessRate": metrics.citation_completeness_rate,
            "vectorChannelCoverageRate": metrics.vector_channel_coverage_rate,
            "bm25ChannelCoverageRate": metrics.bm25_channel_coverage_rate,
            "hybridChannelCoverageRate": metrics.hybrid_channel_coverage_rate,
        },
    }


def _retrieval_channels(hit: KnowledgeRetrievalHit) -> list[str]:
    channels: list[str] = []
    if hit.vector_rank is not None:
        channels.append("vector")
    if hit.bm25_rank is not None:
        channels.append("bm25")
    return channels


def _validate_scope(
    *,
    hits: list[KnowledgeRetrievalHit],
    citations: list[KnowledgeRetrievalCitationSource],
    owner_user_id: str,
    knowledge_base_id: str,
) -> None:
    for hit in hits:
        if (
            hit.owner_user_id != owner_user_id
            or hit.tenant_id != owner_user_id
            or hit.knowledge_base_id != knowledge_base_id
        ):
            raise ValueError("Retrieval result escaped the requested owner/knowledge-base scope.")
    hit_ids = {(hit.chunk_id, hit.document_id) for hit in hits}
    for citation in citations:
        if (
            citation.knowledge_base_id != knowledge_base_id
            or (citation.chunk_id, citation.document_id) not in hit_ids
        ):
            raise ValueError("Retrieval citation escaped the requested result scope.")


def _passes(payload: Mapping[str, object]) -> bool:
    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping):
        return False
    typed_metrics = cast(Mapping[str, object], metrics)
    return (
        _number_at_least(typed_metrics.get("recallAt1"), 0.80)
        and _number_at_least(typed_metrics.get("recallAt3"), 0.95)
        and _number_at_least(typed_metrics.get("mrr"), 0.85)
        and _number_at_most(typed_metrics.get("forbiddenTopOneRate"), 0.05)
        and typed_metrics.get("citationCompletenessRate") == 1.0
    )


def _number_at_least(value: object, minimum: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum


def _number_at_most(value: object, maximum: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= maximum


def _rerank_score_at(hits: list[KnowledgeRetrievalHit], index: int) -> float | None:
    if index >= len(hits):
        return None
    return hits[index].rerank_score


def _top_two_margin(hits: list[KnowledgeRetrievalHit]) -> float | None:
    first = _rerank_score_at(hits, 0)
    second = _rerank_score_at(hits, 1)
    if first is None or second is None:
        return None
    return first - second


async def run_command(arguments: argparse.Namespace) -> int:
    config_path = str(arguments.config) if arguments.config is not None else None
    provider_config = load_llm_provider_config(config_path=config_path)
    provider = build_default_llm_provider(config_path=config_path)
    tool = KnowledgeRetrievalTool(
        embedding_model=provider.create_embedding_model(),
        vector_store=build_default_milvus_vector_store(config_path=config_path),
        rerank_model=provider.create_rerank_model(),
    )
    payload = await run_queries(
        tool,
        owner_user_id=arguments.owner_user_id,
        knowledge_base_id=arguments.knowledge_base_id,
        queries_path=arguments.queries,
        model_configuration={
            "embeddingModel": provider_config.embedding_model,
            "rerankModel": provider_config.rerank_model,
        },
    )
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{serialized}\n", encoding="utf-8")
    return 0 if _passes(payload) else 1


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return asyncio.run(run_command(arguments))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
