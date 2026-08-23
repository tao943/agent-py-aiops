from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from super_ai.evaluation.history import EvaluationRunEnvelope
from super_ai.evaluation.query_rewrite import (
    load_query_rewrite_cases,
    run_query_rewrite_arm,
)
from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalQueryTransform,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolResult,
)

CASES = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "retrieval"
    / "query_rewrite_cases.yaml"
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_query_rewrite_benchmark.py"
SPEC = importlib.util.spec_from_file_location("run_query_rewrite_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class _Tool:
    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        source = (
            "postgres-deadlock.md"
            if "PostgreSQL" in input.query
            else "postgres-slow-query-lock-wait.md"
        )
        hit = KnowledgeRetrievalHit(
            chunk_id="chunk-1",
            document_id="doc-1",
            knowledge_base_id=accessible_knowledge_base_ids[0],
            owner_user_id=owner_user_id,
            tenant_id=owner_user_id,
            content="private chunk content",
            source=source,
            metadata={},
            score=0.9,
            vector_rank=1,
            bm25_rank=1,
            rerank_rank=1,
            vector_score=0.8,
            bm25_score=2.0,
            rrf_score=0.03,
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
            excerpt="private chunk content",
            knowledge_type="diagnostic-case",
            vector_rank=1,
            bm25_rank=1,
            rerank_rank=1,
            vector_score=0.8,
            bm25_score=2.0,
            rrf_score=0.03,
            rerank_score=0.9,
        )
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=cast(int, input.top_k),
            results=[hit],
            citations=[citation],
        )


class _Transformer:
    async def transform(
        self, input: KnowledgeRetrievalToolInput
    ) -> KnowledgeRetrievalQueryTransform:
        return KnowledgeRetrievalQueryTransform(
            input=KnowledgeRetrievalToolInput(
                query="PostgreSQL SQLSTATE 40P01 deadlock 排查",
                top_k=input.top_k,
                filters=input.filters,
            ),
            metadata={
                "action": "rewrite",
                "reason": "context_reference",
                "applied": True,
                "modelCallCount": 1,
                "durationMs": 7,
                "safeErrorCode": None,
            },
        )


class _SlowTransformer(_Transformer):
    async def transform(
        self, input: KnowledgeRetrievalToolInput
    ) -> KnowledgeRetrievalQueryTransform:
        await asyncio.sleep(0.02)
        return await super().transform(input)


def test_loads_reviewed_query_rewrite_fixture() -> None:
    cases = load_query_rewrite_cases(CASES)

    assert len(cases) == 10
    assert cases[0].id == "QR-001"
    assert cases[0].context[0].role == "user"
    assert cases[0].relevant_documents == ("postgres-deadlock.md",)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("empty", "at least 8"),
        ("role", "role"),
        ("path", "basename"),
    ],
)
def test_loader_rejects_invalid_or_unsafe_fixture(
    tmp_path: Path, kind: str, message: str
) -> None:
    path = tmp_path / "cases.yaml"
    cases: list[dict[str, object]] = [
        {
            "id": f"QR-{index}",
            "context": [
                {"role": "user", "content": "Redis maxclients"},
                {"role": "assistant", "content": "连接被拒绝"},
            ],
            "follow_up_query": "那怎么办",
            "relevant_documents": ["redis-maxclients-pressure.md"],
            "forbidden_top_one": [],
            "acceptable_top_k": 3,
        }
        for index in range(8)
    ]
    if kind == "empty":
        cases = []
    elif kind == "role":
        context = cast(list[dict[str, object]], cases[0]["context"])
        context[0]["role"] = "tool"
    else:
        cases[0]["relevant_documents"] = ["../unsafe.md"]
    path.write_text(json.dumps({"cases": cases}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_query_rewrite_cases(path)


@pytest.mark.asyncio
async def test_component_arm_scores_rewrite_without_persisting_conversation_text() -> None:
    case = load_query_rewrite_cases(CASES)[0]

    baseline = await run_query_rewrite_arm(
        _Tool(),
        cases=(case,),
        owner_user_id="owner-a",
        knowledge_base_id="kb-a",
    )
    rewrite = await run_query_rewrite_arm(
        _Tool(),
        cases=(case,),
        owner_user_id="owner-a",
        knowledge_base_id="kb-a",
        transformer_factory=lambda context: _Transformer(),
    )

    baseline_metrics = cast(Mapping[str, object], baseline["metrics"])
    rewrite_metrics = cast(Mapping[str, object], rewrite["metrics"])
    assert baseline_metrics["recallAt1"] == 0.0
    assert rewrite_metrics["recallAt1"] == 1.0
    assert rewrite_metrics["rewriteAppliedCount"] == 1
    assert rewrite_metrics["rewriteModelCallCount"] == 1
    serialized = json.dumps(rewrite, ensure_ascii=False)
    assert case.follow_up_query not in serialized
    assert case.context[0].content not in serialized
    assert "private chunk content" not in serialized
    assert "PostgreSQL SQLSTATE 40P01 deadlock 排查" not in serialized


@pytest.mark.asyncio
async def test_arm_latency_includes_query_rewrite_time() -> None:
    case = load_query_rewrite_cases(CASES)[0]

    result = await run_query_rewrite_arm(
        _Tool(),
        cases=(case,),
        owner_user_id="owner-a",
        knowledge_base_id="kb-a",
        transformer_factory=lambda context: _SlowTransformer(),
    )

    metrics = cast(Mapping[str, object], result["metrics"])
    assert cast(float, metrics["averageDurationMs"]) >= 15
    assert cast(int, metrics["p95DurationMs"]) >= 15


class _Recorder:
    def __init__(self) -> None:
        self.started: list[EvaluationRunEnvelope] = []
        self.finished: list[EvaluationRunEnvelope] = []
        self.failed: list[EvaluationRunEnvelope] = []

    async def start(self, envelope: EvaluationRunEnvelope) -> object:
        self.started.append(envelope)
        return type("Outcome", (), {"database_pending": False})()

    async def finish(self, envelope: EvaluationRunEnvelope) -> object:
        self.finished.append(envelope)
        return type("Outcome", (), {"database_pending": False})()

    async def fail(self, envelope: EvaluationRunEnvelope) -> object:
        self.failed.append(envelope)
        return type("Outcome", (), {"database_pending": False})()


def _metrics(recall: float) -> dict[str, object]:
    return {
        "queryCount": 10,
        "answerableQueryCount": 10,
        "noAnswerProbeCount": 0,
        "recallAt1": recall,
        "recallAt3": recall,
        "mrr": recall,
        "forbiddenTopOneRate": 0.0,
        "citationCompletenessRate": 1.0,
        "vectorChannelCoverageRate": 1.0,
        "bm25ChannelCoverageRate": 1.0,
        "hybridChannelCoverageRate": 1.0,
        "rewriteAppliedCount": 10,
        "rewriteModelCallCount": 10,
        "averageDurationMs": 10.0,
        "p95DurationMs": 12,
    }


def test_query_rewrite_threshold_accepts_nine_of_ten_recall() -> None:
    assert CLI._threshold_failures(_metrics(0.90)) == []
    assert "recallAt3_below_threshold" in CLI._threshold_failures(_metrics(0.89))


def test_query_rewrite_threshold_keeps_forbidden_top_one_gate() -> None:
    metrics = _metrics(1.0)
    metrics["forbiddenTopOneRate"] = 0.10

    assert "forbiddenTopOneRate_below_threshold" in CLI._threshold_failures(metrics)


def test_corpus_fingerprint_is_content_safe_and_order_independent() -> None:
    chunks = [
        type("Chunk", (), {
            "chunk_id": "chunk-2",
            "document_id": "doc-1",
            "source": "runbook.md",
            "created_at": 200,
            "content": "private second chunk",
        })(),
        type("Chunk", (), {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "source": "runbook.md",
            "created_at": 100,
            "content": "private first chunk",
        })(),
    ]

    metadata = CLI._corpus_metadata(chunks)
    reversed_metadata = CLI._corpus_metadata(tuple(reversed(chunks)))

    assert metadata == reversed_metadata
    assert metadata["documentCount"] == 1
    assert metadata["chunkCount"] == 2
    assert len(cast(str, metadata["corpusFingerprint"])) == 64
    assert "private" not in json.dumps(metadata)


def test_real_cli_requires_explicit_confirmation() -> None:
    with pytest.raises(SystemExit):
        CLI.build_parser().parse_args(
            ["--owner-user-id", "owner-a", "--knowledge-base-id", "kb-a"]
        )


@pytest.mark.asyncio
async def test_pair_starts_both_arms_before_execution_and_persists_results() -> None:
    recorder = _Recorder()
    execution_started_after = 0
    corpus_metadata = {
        "corpusFingerprint": "a" * 64,
        "documentCount": 42,
        "chunkCount": 224,
    }

    async def baseline() -> dict[str, object]:
        nonlocal execution_started_after
        execution_started_after = len(recorder.started)
        return {
            "metrics": _metrics(0.8),
            "queryResults": [],
            "corpusMetadata": corpus_metadata,
        }

    async def rewrite() -> dict[str, object]:
        return {
            "metrics": _metrics(1.0),
            "queryResults": [],
            "corpusMetadata": corpus_metadata,
        }

    payload, exit_code = await CLI._run_query_rewrite_pair(
        run_id="qr-pair",
        cases_path=CASES,
        owner_user_id="owner-a",
        knowledge_base_id="kb-a",
        model_configuration={"chatModel": "chat", "rerankModel": "rerank"},
        execute_baseline=baseline,
        execute_rewrite=rewrite,
        recorder=recorder,
    )

    assert execution_started_after == 2
    assert [item.run_id for item in recorder.started] == [
        "qr-pair-baseline",
        "qr-pair-rewrite",
    ]
    assert [item.status for item in recorder.finished] == ["failed", "passed"]
    assert recorder.failed == []
    assert recorder.finished[0].metadata == recorder.started[0].metadata
    assert recorder.finished[0].result_payload["corpusMetadata"] == corpus_metadata
    assert exit_code == 0
    assert payload["evaluationScope"] == "query-rewrite-retrieval-component"


@pytest.mark.asyncio
async def test_baseline_failure_terminates_both_arms_as_infra_invalid() -> None:
    recorder = _Recorder()

    async def baseline() -> dict[str, object]:
        raise TimeoutError("private provider detail")

    async def rewrite() -> dict[str, object]:
        raise AssertionError("rewrite must not execute")

    payload, exit_code = await CLI._run_query_rewrite_pair(
        run_id="qr-failed",
        cases_path=CASES,
        owner_user_id="owner-a",
        knowledge_base_id="kb-a",
        model_configuration={},
        execute_baseline=baseline,
        execute_rewrite=rewrite,
        recorder=recorder,
    )

    assert exit_code == 2
    assert payload == {"error": "query_rewrite_benchmark_failed"}
    assert [item.status for item in recorder.failed] == ["infra_invalid", "infra_invalid"]
    assert recorder.failed[1].result_payload["failures"] == ["rewrite_not_executed"]


@pytest.mark.asyncio
async def test_baseline_cancellation_interrupts_both_arms() -> None:
    recorder = _Recorder()

    async def baseline() -> dict[str, object]:
        raise asyncio.CancelledError

    async def rewrite() -> dict[str, object]:
        raise AssertionError("rewrite must not execute")

    with pytest.raises(asyncio.CancelledError):
        await CLI._run_query_rewrite_pair(
            run_id="qr-cancelled",
            cases_path=CASES,
            owner_user_id="owner-a",
            knowledge_base_id="kb-a",
            model_configuration={},
            execute_baseline=baseline,
            execute_rewrite=rewrite,
            recorder=recorder,
        )

    assert [item.status for item in recorder.failed] == ["interrupted", "interrupted"]
