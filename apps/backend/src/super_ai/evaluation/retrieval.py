"""Answer-free labels and deterministic metrics for knowledge retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

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
RetrievalQueryType: TypeAlias = Literal[
    "explicit_component",
    "ambiguous_symptom",
    "log_signal",
    "operator_perturbation",
    "cross_component_distractor",
    "no_answer_probe",
]
RetrievalSourceType: TypeAlias = Literal["project-synthesized", "public-symptom-rewrite"]
RetrievalReviewStatus: TypeAlias = Literal["reviewed"]


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """One reviewed, answer-free retrieval query label."""

    id: str
    query: str
    relevant_documents: tuple[str, ...]
    acceptable_top_k: int
    forbidden_top_one: tuple[str, ...] = ()
    query_type: RetrievalQueryType = "explicit_component"
    source_type: RetrievalSourceType = "project-synthesized"
    review_status: RetrievalReviewStatus = "reviewed"
    expected_no_answer: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalCitationAudit:
    """Citation fields required for traceable retrieval evidence."""

    chunk_id: str
    document_id: str
    knowledge_base_id: str
    vector_score: float | None
    rerank_score: float | None
    vector_rank: int | None = None
    bm25_rank: int | None = None
    rerank_rank: int | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None

    @property
    def retrieval_channels(self) -> tuple[str, ...]:
        """Return recall channels recorded by rank provenance."""
        channels: list[str] = []
        if self.vector_rank is not None:
            channels.append("vector")
        if self.bm25_rank is not None:
            channels.append("bm25")
        return tuple(channels)

    @property
    def complete(self) -> bool:
        vector_consistent = (self.vector_rank is None) == (self.vector_score is None)
        bm25_consistent = (self.bm25_rank is None) == (self.bm25_score is None)
        return bool(
            self.chunk_id
            and self.document_id
            and self.knowledge_base_id
            and vector_consistent
            and bm25_consistent
            and self.retrieval_channels
            and self.rrf_score is not None
            and self.rerank_rank is not None
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
    expected_no_answer: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationResult:
    """Aggregate deterministic retrieval metrics."""

    query_count: int
    answerable_query_count: int
    no_answer_probe_count: int
    recall_at_1: float
    recall_at_3: float
    mrr: float
    forbidden_top_one_rate: float
    citation_completeness_rate: float
    vector_channel_coverage_rate: float
    bm25_channel_coverage_rate: float
    hybrid_channel_coverage_rate: float


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
        expected_no_answer = item.get("expected_no_answer", False)
        if not isinstance(expected_no_answer, bool):
            raise ValueError(f"Retrieval query {query_id} expected_no_answer must be boolean.")
        if not relevant and not expected_no_answer:
            raise ValueError(f"Retrieval query {query_id} requires relevant documents.")
        if relevant and expected_no_answer:
            raise ValueError(f"Retrieval query {query_id} no-answer probe cannot be relevant.")
        top_k = item.get("acceptable_top_k")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 5:
            raise ValueError(f"Retrieval query {query_id} top K must be between 1 and 5.")
        forbidden = _text_tuple(item.get("forbidden_top_one", ()), "forbidden_top_one")
        query_type = _choice(
            item.get("type"),
            "type",
            {
                "explicit_component",
                "ambiguous_symptom",
                "log_signal",
                "operator_perturbation",
                "cross_component_distractor",
                "no_answer_probe",
            },
        )
        if expected_no_answer != (query_type == "no_answer_probe"):
            raise ValueError(f"Retrieval query {query_id} has inconsistent no-answer metadata.")
        source_type = _choice(
            item.get("source_type"),
            "source_type",
            {"project-synthesized", "public-symptom-rewrite"},
        )
        review_status = _choice(item.get("review_status"), "review_status", {"reviewed"})
        queries.append(
            RetrievalQuery(
                query_id,
                query_text,
                relevant,
                top_k,
                forbidden,
                cast(RetrievalQueryType, query_type),
                cast(RetrievalSourceType, source_type),
                cast(RetrievalReviewStatus, review_status),
                expected_no_answer,
            )
        )
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
    answerable_count = sum(not result.expected_no_answer for result in results)
    if answerable_count == 0:
        raise ValueError("Retrieval evaluation requires at least one answerable query result.")
    recall_1 = 0
    recall_3 = 0
    reciprocal_rank = 0.0
    forbidden_top_one = 0
    citations: list[RetrievalCitationAudit] = []
    for result in results:
        citations.extend(result.citations)
        if result.expected_no_answer:
            continue
        relevant = set(result.relevant_documents)
        ranked_documents = deduplicate_ranked_documents(result.ranked_documents)
        if relevant.intersection(ranked_documents[:1]):
            recall_1 += 1
        if relevant.intersection(ranked_documents[:3]):
            recall_3 += 1
        first_relevant = next(
            (
                rank
                for rank, document in enumerate(ranked_documents, start=1)
                if document in relevant
            ),
            None,
        )
        if first_relevant is not None:
            reciprocal_rank += 1 / first_relevant
        if ranked_documents and ranked_documents[0] in result.forbidden_top_one:
            forbidden_top_one += 1
    citation_rate = (
        sum(citation.complete for citation in citations) / len(citations) if citations else 0.0
    )
    vector_coverage = (
        sum("vector" in citation.retrieval_channels for citation in citations) / len(citations)
        if citations
        else 0.0
    )
    bm25_coverage = (
        sum("bm25" in citation.retrieval_channels for citation in citations) / len(citations)
        if citations
        else 0.0
    )
    hybrid_coverage = (
        sum(len(citation.retrieval_channels) == 2 for citation in citations) / len(citations)
        if citations
        else 0.0
    )
    return RetrievalEvaluationResult(
        query_count=count,
        answerable_query_count=answerable_count,
        no_answer_probe_count=count - answerable_count,
        recall_at_1=recall_1 / answerable_count,
        recall_at_3=recall_3 / answerable_count,
        mrr=reciprocal_rank / answerable_count,
        forbidden_top_one_rate=forbidden_top_one / answerable_count,
        citation_completeness_rate=citation_rate,
        vector_channel_coverage_rate=vector_coverage,
        bm25_channel_coverage_rate=bm25_coverage,
        hybrid_channel_coverage_rate=hybrid_coverage,
    )


def deduplicate_ranked_documents(documents: Sequence[str]) -> tuple[str, ...]:
    """Preserve first appearance while collapsing repeated chunks from one source."""
    return tuple(dict.fromkeys(documents))


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


def _choice(value: object, label: str, allowed: set[str]) -> str:
    text = _text(value, label)
    if text not in allowed:
        raise ValueError(f"{label} must be one of: {', '.join(sorted(allowed))}.")
    return text
