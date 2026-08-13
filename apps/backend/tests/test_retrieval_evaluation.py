from collections import Counter
from pathlib import Path

import pytest

from super_ai.evaluation.retrieval import (
    RetrievalCitationAudit,
    RetrievalQueryResult,
    deduplicate_ranked_documents,
    evaluate_retrieval,
    load_retrieval_queries,
)

QUERIES = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "retrieval"
    / "queries.yaml"
)
KNOWLEDGE = Path(__file__).resolve().parents[3] / "docs" / "knowledge-candidates"


def _citation_audit(
    *,
    vector_rank: int | None = 1,
    bm25_rank: int | None = 1,
    rerank_rank: int | None = 1,
    vector_score: float | None = 0.8,
    bm25_score: float | None = 2.1,
    rrf_score: float | None = 0.016,
    rerank_score: float | None = 0.9,
) -> RetrievalCitationAudit:
    return RetrievalCitationAudit(
        chunk_id="chunk-1",
        document_id="doc-1",
        knowledge_base_id="kb-1",
        vector_rank=vector_rank,
        bm25_rank=bm25_rank,
        rerank_rank=rerank_rank,
        vector_score=vector_score,
        bm25_score=bm25_score,
        rrf_score=rrf_score,
        rerank_score=rerank_score,
    )


@pytest.mark.parametrize(
    "audit, channels",
    [
        (
            _citation_audit(vector_rank=None, vector_score=None),
            ("bm25",),
        ),
        (
            _citation_audit(bm25_rank=None, bm25_score=None),
            ("vector",),
        ),
        (_citation_audit(), ("vector", "bm25")),
    ],
)
def test_complete_citation_tracks_actual_retrieval_channels(
    audit: RetrievalCitationAudit,
    channels: tuple[str, ...],
) -> None:
    assert audit.complete is True
    assert audit.retrieval_channels == channels


@pytest.mark.parametrize(
    "audit",
    [
        _citation_audit(vector_score=None),
        _citation_audit(vector_rank=None),
        _citation_audit(bm25_score=None),
        _citation_audit(bm25_rank=None),
        _citation_audit(
            vector_rank=None,
            vector_score=None,
            bm25_rank=None,
            bm25_score=None,
        ),
        _citation_audit(rrf_score=None),
        _citation_audit(rerank_rank=None),
        _citation_audit(rerank_score=None),
    ],
)
def test_incomplete_citation_rejects_inconsistent_or_missing_provenance(
    audit: RetrievalCitationAudit,
) -> None:
    assert audit.complete is False


def test_evaluation_reports_retrieval_channel_coverage() -> None:
    vector_only = _citation_audit(bm25_rank=None, bm25_score=None)
    bm25_only = _citation_audit(vector_rank=None, vector_score=None)
    hybrid = _citation_audit()

    report = evaluate_retrieval(
        (
            RetrievalQueryResult(
                query_id="q1",
                relevant_documents=("target.md",),
                forbidden_top_one=(),
                ranked_documents=("target.md",),
                citations=(vector_only, bm25_only, hybrid),
            ),
        )
    )

    assert report.citation_completeness_rate == 1.0
    assert report.vector_channel_coverage_rate == pytest.approx(2 / 3)
    assert report.bm25_channel_coverage_rate == pytest.approx(2 / 3)
    assert report.hybrid_channel_coverage_rate == pytest.approx(1 / 3)


def test_loads_sixty_answer_free_reviewed_queries_with_approved_distribution() -> None:
    queries = load_retrieval_queries(QUERIES)

    assert len(queries) == 60
    assert sum(not query.expected_no_answer for query in queries) == 54
    assert sum(query.expected_no_answer for query in queries) == 6
    distribution = Counter(query.query_type for query in queries)
    assert distribution == {
        "explicit_component": 12,
        "ambiguous_symptom": 14,
        "log_signal": 12,
        "operator_perturbation": 8,
        "cross_component_distractor": 8,
        "no_answer_probe": 6,
    }
    assert all(query.relevant_documents or query.expected_no_answer for query in queries)
    assert all(1 <= query.acceptable_top_k <= 5 for query in queries)
    assert not any("APY-" in query.query for query in queries)


def test_answerable_queries_cover_all_cards_with_exactly_twenty_four_second_queries() -> None:
    queries = load_retrieval_queries(QUERIES)
    approved_cards = {path.name for path in KNOWLEDGE.glob("*.md")}
    coverage = Counter(
        document
        for query in queries
        if not query.expected_no_answer
        for document in query.relevant_documents
    )

    assert set(coverage) == approved_cards
    assert set(coverage.values()) <= {1, 2}
    assert sum(count == 2 for count in coverage.values()) == 24


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        (
            "queries:\n- id: RET-1\n  query: q\n"
            "  relevant_documents: [a.md]\n  acceptable_top_k: 3\n"
            "  type: explicit_component\n  source_type: project-synthesized\n"
            "  review_status: reviewed\n  expected_no_answer: false\n"
            "- id: RET-1\n  query: q2\n  relevant_documents: [b.md]\n  acceptable_top_k: 3\n"
            "  type: explicit_component\n  source_type: project-synthesized\n"
            "  review_status: reviewed\n  expected_no_answer: false\n",
            "unique",
        ),
        (
            "queries:\n- id: RET-1\n  query: q\n  relevant_documents: []\n  acceptable_top_k: 3\n",
            "relevant",
        ),
        (
            "queries:\n- id: RET-1\n  query: q\n"
            "  relevant_documents: [a.md]\n  acceptable_top_k: 6\n",
            "top",
        ),
        (
            "queries:\n- id: RET-1\n  query: APY-002 ground_truth\n"
            "  relevant_documents: [a.md]\n  acceptable_top_k: 3\n",
            "answer",
        ),
    ],
)
def test_loader_rejects_invalid_or_answer_bearing_labels(
    tmp_path: Path,
    yaml_text: str,
    message: str,
) -> None:
    path = tmp_path / "queries.yaml"
    if "type:" not in yaml_text:
        yaml_text = yaml_text.replace(
            "  acceptable_top_k:",
            "  type: explicit_component\n"
            "  source_type: project-synthesized\n"
            "  review_status: reviewed\n"
            "  expected_no_answer: false\n"
            "  acceptable_top_k:",
        )
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_retrieval_queries(path)


def test_evaluate_retrieval_calculates_ranking_and_citation_metrics() -> None:
    complete = RetrievalCitationAudit(
        chunk_id="chunk-1",
        document_id="doc-1",
        knowledge_base_id="kb-1",
        vector_score=0.8,
        rerank_score=0.9,
        vector_rank=1,
        rerank_rank=1,
        rrf_score=0.016,
    )
    incomplete = RetrievalCitationAudit(
        chunk_id="chunk-2",
        document_id="",
        knowledge_base_id="kb-1",
        vector_score=0.7,
        rerank_score=None,
        vector_rank=1,
        rerank_rank=1,
        rrf_score=0.016,
    )
    report = evaluate_retrieval(
        (
            RetrievalQueryResult(
                query_id="q1",
                relevant_documents=("target.md",),
                forbidden_top_one=("wrong.md",),
                ranked_documents=("target.md", "wrong.md"),
                citations=(complete,),
            ),
            RetrievalQueryResult(
                query_id="q2",
                relevant_documents=("target.md",),
                forbidden_top_one=("wrong.md",),
                ranked_documents=("wrong.md", "target.md"),
                citations=(incomplete,),
            ),
            RetrievalQueryResult(
                query_id="q3",
                relevant_documents=("target.md",),
                forbidden_top_one=("wrong.md",),
                ranked_documents=("other.md",),
                citations=(),
            ),
        )
    )

    assert report.query_count == 3
    assert report.recall_at_1 == pytest.approx(1 / 3)
    assert report.recall_at_3 == pytest.approx(2 / 3)
    assert report.mrr == pytest.approx(0.5)
    assert report.forbidden_top_one_rate == pytest.approx(1 / 3)
    assert report.citation_completeness_rate == pytest.approx(0.5)


def test_perfect_retrieval_result_scores_one_without_forbidden_top_one() -> None:
    report = evaluate_retrieval(
        (
            RetrievalQueryResult(
                query_id="q1",
                relevant_documents=("target.md",),
                forbidden_top_one=("wrong.md",),
                ranked_documents=("target.md",),
                citations=(
                    RetrievalCitationAudit(
                        chunk_id="chunk-1",
                        document_id="doc-1",
                        knowledge_base_id="kb-1",
                        vector_score=0.8,
                        rerank_score=0.9,
                        vector_rank=1,
                        rerank_rank=1,
                        rrf_score=0.016,
                    ),
                ),
            ),
        )
    )

    assert report.recall_at_1 == 1.0
    assert report.recall_at_3 == 1.0
    assert report.mrr == 1.0
    assert report.forbidden_top_one_rate == 0.0
    assert report.citation_completeness_rate == 1.0


def test_evaluate_retrieval_rejects_empty_results() -> None:
    with pytest.raises(ValueError, match="at least one"):
        evaluate_retrieval(())


def test_deduplicates_chunk_hits_before_document_ranking() -> None:
    assert deduplicate_ranked_documents(
        ("target.md", "target.md", "wrong.md", "target.md")
    ) == ("target.md", "wrong.md")


def test_no_answer_probes_do_not_enter_ranking_metric_denominators() -> None:
    complete = RetrievalCitationAudit(
        "chunk",
        "doc",
        "kb",
        0.8,
        0.9,
        vector_rank=1,
        rerank_rank=1,
        rrf_score=0.016,
    )
    report = evaluate_retrieval(
        (
            RetrievalQueryResult(
                query_id="answerable",
                relevant_documents=("target.md",),
                forbidden_top_one=(),
                ranked_documents=("target.md", "target.md", "wrong.md"),
                citations=(complete, complete, complete),
                expected_no_answer=False,
            ),
            RetrievalQueryResult(
                query_id="probe",
                relevant_documents=(),
                forbidden_top_one=(),
                ranked_documents=("wrong.md",),
                citations=(complete,),
                expected_no_answer=True,
            ),
        )
    )

    assert report.query_count == 2
    assert report.answerable_query_count == 1
    assert report.no_answer_probe_count == 1
    assert report.recall_at_1 == 1.0
    assert report.recall_at_3 == 1.0
    assert report.mrr == 1.0


def test_loader_accepts_reviewed_no_answer_probe(tmp_path: Path) -> None:
    path = tmp_path / "queries.yaml"
    path.write_text(
        "queries:\n"
        "- id: RET-NONE-1\n"
        "  type: no_answer_probe\n"
        "  query: 未覆盖组件出现冷门告警\n"
        "  relevant_documents: []\n"
        "  acceptable_top_k: 3\n"
        "  forbidden_top_one: []\n"
        "  source_type: project-synthesized\n"
        "  review_status: reviewed\n"
        "  expected_no_answer: true\n",
        encoding="utf-8",
    )

    query = load_retrieval_queries(path)[0]

    assert query.expected_no_answer is True
    assert query.relevant_documents == ()
