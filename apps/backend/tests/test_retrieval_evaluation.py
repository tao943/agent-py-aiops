from pathlib import Path

import pytest

from super_ai.evaluation.retrieval import (
    RetrievalCitationAudit,
    RetrievalQueryResult,
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


def test_loads_six_answer_free_reviewed_queries() -> None:
    queries = load_retrieval_queries(QUERIES)

    assert [query.id for query in queries] == [
        "RET-PG-001",
        "RET-PG-002",
        "RET-PG-003",
        "RET-REDIS-001",
        "RET-REDIS-002",
        "RET-REDIS-003",
    ]
    assert all(query.relevant_documents for query in queries)
    assert all(1 <= query.acceptable_top_k <= 5 for query in queries)
    assert not any("APY-" in query.query for query in queries)


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        (
            "queries:\n- id: RET-1\n  query: q\n"
            "  relevant_documents: [a.md]\n  acceptable_top_k: 3\n"
            "- id: RET-1\n  query: q2\n  relevant_documents: [b.md]\n  acceptable_top_k: 3\n",
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
    )
    incomplete = RetrievalCitationAudit(
        chunk_id="chunk-2",
        document_id="",
        knowledge_base_id="kb-1",
        vector_score=0.7,
        rerank_score=None,
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
