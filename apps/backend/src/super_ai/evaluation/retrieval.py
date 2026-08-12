"""Answer-free labels and deterministic metrics for knowledge retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

_ANSWER_TOKENS = (
    "apy-",
    "ground_truth",
    "primary_cause",
    "evidence_id",
    "benchmark_container",
    "slow_transaction_pool_exhaustion",
    "borrowed_connection_not_returned",
    "service_process_stopped",
    "stale_connections_retained_after_recovery",
)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """One reviewed, answer-free retrieval query label."""

    id: str
    query: str
    relevant_documents: tuple[str, ...]
    acceptable_top_k: int
    forbidden_top_one: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalCitationAudit:
    """Citation fields required for traceable retrieval evidence."""

    chunk_id: str
    document_id: str
    knowledge_base_id: str
    vector_score: float | None
    rerank_score: float | None

    @property
    def complete(self) -> bool:
        return bool(
            self.chunk_id
            and self.document_id
            and self.knowledge_base_id
            and self.vector_score is not None
            and self.rerank_score is not None
        )


@dataclass(frozen=True, slots=True)
class RetrievalQueryResult:
    """Recorded ranked result for one reviewed query."""

    query_id: str
    relevant_documents: tuple[str, ...]
    forbidden_top_one: tuple[str, ...]
    ranked_documents: tuple[str, ...]
    citations: tuple[RetrievalCitationAudit, ...]


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationResult:
    """Aggregate deterministic retrieval metrics."""

    query_count: int
    recall_at_1: float
    recall_at_3: float
    mrr: float
    forbidden_top_one_rate: float
    citation_completeness_rate: float


def load_retrieval_queries(path: Path) -> tuple[RetrievalQuery, ...]:
    """Load and validate answer-free retrieval query labels."""
    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Retrieval query file does not exist: {path}.") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Retrieval query YAML is invalid: {path}.") from exc
    root = _mapping(payload, "retrieval query file")
    raw_queries = _sequence(root.get("queries"), "queries")
    queries: list[RetrievalQuery] = []
    for raw_query in raw_queries:
        item = _mapping(raw_query, "retrieval query")
        query_id = _text(item.get("id"), "id")
        query_text = _text(item.get("query"), "query")
        if any(token in query_text.lower() for token in _ANSWER_TOKENS):
            raise ValueError(f"Retrieval query {query_id} contains answer-only text.")
        relevant = _text_tuple(item.get("relevant_documents"), "relevant_documents")
        if not relevant:
            raise ValueError(f"Retrieval query {query_id} requires relevant documents.")
        top_k = item.get("acceptable_top_k")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 5:
            raise ValueError(f"Retrieval query {query_id} top K must be between 1 and 5.")
        forbidden = _text_tuple(item.get("forbidden_top_one", ()), "forbidden_top_one")
        queries.append(RetrievalQuery(query_id, query_text, relevant, top_k, forbidden))
    if not queries:
        raise ValueError("Retrieval query file must contain at least one query.")
    ids = [query.id for query in queries]
    if len(ids) != len(set(ids)):
        raise ValueError("Retrieval query IDs must be unique.")
    return tuple(queries)


def evaluate_retrieval(
    results: Sequence[RetrievalQueryResult],
) -> RetrievalEvaluationResult:
    """Compute deterministic ranking and citation metrics."""
    if not results:
        raise ValueError("Retrieval evaluation requires at least one query result.")
    count = len(results)
    recall_1 = 0
    recall_3 = 0
    reciprocal_rank = 0.0
    forbidden_top_one = 0
    citations: list[RetrievalCitationAudit] = []
    for result in results:
        relevant = set(result.relevant_documents)
        if relevant.intersection(result.ranked_documents[:1]):
            recall_1 += 1
        if relevant.intersection(result.ranked_documents[:3]):
            recall_3 += 1
        first_relevant = next(
            (
                rank
                for rank, document in enumerate(result.ranked_documents, start=1)
                if document in relevant
            ),
            None,
        )
        if first_relevant is not None:
            reciprocal_rank += 1 / first_relevant
        if result.ranked_documents and result.ranked_documents[0] in result.forbidden_top_one:
            forbidden_top_one += 1
        citations.extend(result.citations)
    citation_rate = (
        sum(citation.complete for citation in citations) / len(citations) if citations else 0.0
    )
    return RetrievalEvaluationResult(
        query_count=count,
        recall_at_1=recall_1 / count,
        recall_at_3=recall_3 / count,
        mrr=reciprocal_rank / count,
        forbidden_top_one_rate=forbidden_top_one / count,
        citation_completeness_rate=citation_rate,
    )


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


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(_text(item, label) for item in _sequence(value, label))
