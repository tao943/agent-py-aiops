"""Component-level A/B evaluation for adaptive Conversation query rewriting."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol, cast

import yaml

from super_ai.chat.query_rewrite import QueryRewriteContextMessage
from super_ai.evaluation.retrieval import (
    RetrievalCitationAudit,
    RetrievalQueryResult,
    evaluate_retrieval,
)
from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalQueryTransformer,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolRunner,
)

MIN_QUERY_REWRITE_CASES = 8


@dataclass(frozen=True, slots=True)
class QueryRewriteCase:
    id: str
    context: tuple[QueryRewriteContextMessage, ...]
    follow_up_query: str
    relevant_documents: tuple[str, ...]
    forbidden_top_one: tuple[str, ...]
    acceptable_top_k: int


class QueryTransformerFactory(Protocol):
    def __call__(
        self, context: Sequence[QueryRewriteContextMessage]
    ) -> KnowledgeRetrievalQueryTransformer: ...


def load_query_rewrite_cases(path: Path) -> tuple[QueryRewriteCase, ...]:
    """Load reviewed contextual queries while rejecting unsafe document labels."""

    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Query rewrite fixture is unavailable or invalid: {path}.") from exc
    root = _mapping(payload, "query rewrite fixture")
    raw_cases = _sequence(root.get("cases"), "cases")
    if len(raw_cases) < MIN_QUERY_REWRITE_CASES:
        raise ValueError(
            f"Query rewrite fixture requires at least {MIN_QUERY_REWRITE_CASES} cases."
        )
    cases = tuple(_case(raw_case) for raw_case in raw_cases)
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Query rewrite case IDs must be unique.")
    return cases


async def run_query_rewrite_arm(
    tool: KnowledgeRetrievalToolRunner,
    *,
    cases: Sequence[QueryRewriteCase],
    owner_user_id: str,
    knowledge_base_id: str,
    transformer_factory: QueryTransformerFactory | None = None,
) -> dict[str, object]:
    """Run one content-free retrieval arm against an explicit owner/KB scope."""

    scored: list[RetrievalQueryResult] = []
    query_results: list[dict[str, object]] = []
    durations: list[int] = []
    rewrite_applied_count = 0
    rewrite_model_call_count = 0
    for case in cases:
        started_at = monotonic()
        original = KnowledgeRetrievalToolInput(
            query=case.follow_up_query,
            top_k=case.acceptable_top_k,
        )
        transform = (
            await transformer_factory(case.context).transform(original)
            if transformer_factory is not None
            else None
        )
        effective = transform.input if transform is not None else original
        result = await tool.run(
            effective,
            owner_user_id=owner_user_id,
            accessible_knowledge_base_ids=(knowledge_base_id,),
        )
        duration_ms = round((monotonic() - started_at) * 1000)
        durations.append(duration_ms)
        _validate_scope(
            result.results,
            result.citations,
            owner_user_id=owner_user_id,
            knowledge_base_id=knowledge_base_id,
        )
        metadata: Mapping[str, object] = (
            transform.metadata if transform is not None else {}
        )
        rewrite_applied_count += int(metadata.get("applied") is True)
        model_calls = metadata.get("modelCallCount", 0)
        if isinstance(model_calls, int) and not isinstance(model_calls, bool):
            rewrite_model_call_count += model_calls
        audits = _citation_audits(result.results, result.citations)
        ranked_documents = tuple(Path(hit.source).name for hit in result.results)
        scored.append(
            RetrievalQueryResult(
                query_id=case.id,
                relevant_documents=case.relevant_documents,
                forbidden_top_one=case.forbidden_top_one,
                ranked_documents=ranked_documents,
                citations=audits,
            )
        )
        query_results.append(
            {
                "queryId": case.id,
                "rankedDocuments": list(ranked_documents),
                "durationMs": duration_ms,
                "rewrite": _safe_rewrite_metadata(metadata),
            }
        )
    metrics = evaluate_retrieval(scored)
    metric_payload: dict[str, object] = {
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
        "rewriteAppliedCount": rewrite_applied_count,
        "rewriteModelCallCount": rewrite_model_call_count,
        "averageDurationMs": round(sum(durations) / len(durations), 3),
        "p95DurationMs": _percentile_95(durations),
    }
    return {"metrics": metric_payload, "queryResults": query_results}


def validate_indexed_labels(
    cases: Sequence[QueryRewriteCase], *, indexed_sources: Sequence[str]
) -> None:
    """Require every reviewed label to resolve uniquely in the selected indexed KB."""

    source_counts: dict[str, int] = {}
    for source in indexed_sources:
        basename = Path(source).name
        source_counts[basename] = source_counts.get(basename, 0) + 1
    expected = {
        document
        for case in cases
        for document in (*case.relevant_documents, *case.forbidden_top_one)
    }
    invalid = sorted(document for document in expected if source_counts.get(document) != 1)
    if invalid:
        raise ValueError(
            "Query rewrite labels must resolve uniquely in the indexed knowledge base."
        )


def _case(value: object) -> QueryRewriteCase:
    item = _mapping(value, "query rewrite case")
    allowed = {
        "id",
        "context",
        "follow_up_query",
        "relevant_documents",
        "forbidden_top_one",
        "acceptable_top_k",
    }
    if set(item) != allowed:
        raise ValueError("Query rewrite case fields are invalid.")
    context = tuple(_context_message(value) for value in _sequence(item["context"], "context"))
    if len(context) < 2 or context[0].role != "user" or context[-1].role != "assistant":
        raise ValueError("Query rewrite context requires a complete user/assistant turn.")
    relevant = _documents(item["relevant_documents"], "relevant_documents")
    if not relevant:
        raise ValueError("Query rewrite case requires relevant documents.")
    forbidden = _documents(item["forbidden_top_one"], "forbidden_top_one")
    top_k = item["acceptable_top_k"]
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 5:
        raise ValueError("Query rewrite acceptable_top_k must be between 1 and 5.")
    return QueryRewriteCase(
        id=_text(item["id"], "id"),
        context=context,
        follow_up_query=_text(item["follow_up_query"], "follow_up_query"),
        relevant_documents=relevant,
        forbidden_top_one=forbidden,
        acceptable_top_k=top_k,
    )


def _context_message(value: object) -> QueryRewriteContextMessage:
    item = _mapping(value, "context message")
    if set(item) != {"role", "content"} or item.get("role") not in {"user", "assistant"}:
        raise ValueError("Query rewrite context message role is invalid.")
    return QueryRewriteContextMessage(
        cast(str, item["role"]),  # type: ignore[arg-type]
        _text(item["content"], "context content"),
    )


def _documents(value: object, label: str) -> tuple[str, ...]:
    documents = tuple(_text(item, label) for item in _sequence(value, label))
    if len(documents) != len(set(documents)):
        raise ValueError(f"{label} must not contain duplicates.")
    if any(
        Path(document).name != document or "/" in document or "\\" in document
        for document in documents
    ):
        raise ValueError(f"{label} entries must be safe basenames.")
    return documents


def _citation_audits(
    hits: Sequence[KnowledgeRetrievalHit],
    citations: Sequence[KnowledgeRetrievalCitationSource],
) -> tuple[RetrievalCitationAudit, ...]:
    by_chunk = {citation.chunk_id: citation for citation in citations}
    return tuple(
        RetrievalCitationAudit(
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            knowledge_base_id=hit.knowledge_base_id,
            vector_score=citation.vector_score if citation else None,
            rerank_score=citation.rerank_score if citation else None,
            vector_rank=citation.vector_rank if citation else None,
            bm25_rank=citation.bm25_rank if citation else None,
            rerank_rank=citation.rerank_rank if citation else None,
            bm25_score=citation.bm25_score if citation else None,
            rrf_score=citation.rrf_score if citation else None,
        )
        for hit in hits
        for citation in (by_chunk.get(hit.chunk_id),)
    )


def _validate_scope(
    hits: Sequence[KnowledgeRetrievalHit],
    citations: Sequence[KnowledgeRetrievalCitationSource],
    *,
    owner_user_id: str,
    knowledge_base_id: str,
) -> None:
    if any(
        hit.owner_user_id != owner_user_id
        or hit.tenant_id != owner_user_id
        or hit.knowledge_base_id != knowledge_base_id
        for hit in hits
    ) or any(citation.knowledge_base_id != knowledge_base_id for citation in citations):
        raise ValueError("Query rewrite retrieval escaped the requested scope.")


def _safe_rewrite_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    return {
        key: metadata.get(key)
        for key in (
            "action",
            "reason",
            "applied",
            "modelCallCount",
            "durationMs",
            "safeErrorCode",
        )
    }


def _percentile_95(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping.")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"{label} keys must be strings.")
    return cast(Mapping[str, object], mapping)


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence.")
    return cast(Sequence[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value.strip()
