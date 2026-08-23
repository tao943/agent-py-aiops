"""Run and persist a real component A/B for adaptive query rewriting."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from super_ai.chat.query_rewrite import (
    AdaptiveKnowledgeQueryTransformer,
    StructuredQueryRewriter,
)
from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history import (
    EvaluationRunEnvelope,
    interrupted_envelope,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.query_rewrite import (
    QueryRewriteCase,
    load_query_rewrite_cases,
    run_query_rewrite_arm,
    validate_indexed_labels,
)
from super_ai.evaluation.recording import EvaluationRunRecorder
from super_ai.llm import (
    build_default_llm_provider,
    create_query_rewrite_model,
    load_llm_provider_config,
    query_rewrite_structured_output_method,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.retrieval import KnowledgeRetrievalTool
from super_ai.vector_store import build_default_milvus_vector_store

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES = (
    REPOSITORY_ROOT / "benchmarks" / "agentpy" / "retrieval" / "query_rewrite_cases.yaml"
)


class Recorder(Protocol):
    async def start(self, envelope: EvaluationRunEnvelope) -> object: ...
    async def finish(self, envelope: EvaluationRunEnvelope) -> object: ...
    async def fail(self, envelope: EvaluationRunEnvelope) -> object: ...


class CorpusChunk(Protocol):
    chunk_id: str
    document_id: str
    source: str
    created_at: object


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real Conversation query-rewrite retrieval component A/B."
    )
    parser.add_argument("--confirm-real-model", action="store_true", required=True)
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id")
    return parser


async def _run_query_rewrite_pair(
    *,
    run_id: str,
    cases_path: Path,
    owner_user_id: str,
    knowledge_base_id: str,
    model_configuration: Mapping[str, str],
    execute_baseline: Callable[[], Awaitable[dict[str, object]]],
    execute_rewrite: Callable[[], Awaitable[dict[str, object]]],
    recorder: Recorder,
) -> tuple[dict[str, object], int]:
    """Start both arms before execution and leave no running arm behind."""

    timestamp = datetime.now(timezone.utc)
    running = {
        arm: running_envelope(
            run_id=f"{run_id}-{arm}",
            evaluation_kind="retrieval",
            scenario_id=f"{cases_path.stem}-{arm}",
            suite_version="v1",
            metadata={
                "workflowVersion": "query-rewrite-retrieval-v1",
                "modelConfiguration": dict(model_configuration),
                "datasetChecksum": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
                "ownerUserId": owner_user_id,
                "knowledgeBaseId": knowledge_base_id,
            },
            created_at=timestamp,
            started_at=timestamp,
        )
        for arm in ("baseline", "rewrite")
    }
    starts = [await recorder.start(running[arm]) for arm in ("baseline", "rewrite")]
    database_pending = any(
        bool(getattr(outcome, "database_pending", False)) for outcome in starts
    )
    payloads: dict[str, dict[str, object]] = {}
    try:
        payloads["baseline"] = await execute_baseline()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await _interrupt_unfinished(recorder, running.values())
        raise
    except Exception:
        await recorder.fail(
            _infra_terminal(running["baseline"], failures=["baseline_runtime_error"])
        )
        await recorder.fail(
            _infra_terminal(running["rewrite"], failures=["rewrite_not_executed"])
        )
        return {"error": "query_rewrite_benchmark_failed"}, 2

    baseline_terminal = _scored_terminal(running["baseline"], payloads["baseline"])
    baseline_finish = await recorder.finish(baseline_terminal)
    database_pending = database_pending or bool(
        getattr(baseline_finish, "database_pending", False)
    )
    try:
        payloads["rewrite"] = await execute_rewrite()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await recorder.fail(
            interrupted_envelope(
                running["rewrite"], completed_at=datetime.now(timezone.utc)
            )
        )
        raise
    except Exception:
        await recorder.fail(
            _infra_terminal(running["rewrite"], failures=["rewrite_runtime_error"])
        )
        return {
            "evaluationScope": "query-rewrite-retrieval-component",
            "baseline": payloads["baseline"],
            "error": "query_rewrite_benchmark_failed",
        }, 2

    regression_failures = _regression_failures(
        _metrics(payloads["baseline"]), _metrics(payloads["rewrite"])
    )
    rewrite_terminal = _scored_terminal(
        running["rewrite"], payloads["rewrite"], extra_failures=regression_failures
    )
    rewrite_finish = await recorder.finish(rewrite_terminal)
    database_pending = database_pending or bool(
        getattr(rewrite_finish, "database_pending", False)
    )
    combined = {
        "evaluationScope": "query-rewrite-retrieval-component",
        "baseline": payloads["baseline"],
        "rewrite": payloads["rewrite"],
    }
    if database_pending:
        return combined, 2
    return combined, 0 if rewrite_terminal.passed else 1


async def _interrupt_unfinished(
    recorder: Recorder, running: Iterable[EvaluationRunEnvelope]
) -> None:
    for envelope in running:
        await recorder.fail(
            interrupted_envelope(
                envelope,
                completed_at=datetime.now(timezone.utc),
            )
        )


def _infra_terminal(
    running: EvaluationRunEnvelope, *, failures: list[str]
) -> EvaluationRunEnvelope:
    return terminal_envelope(
        running=running,
        status="infra_invalid",
        validity="INFRA_INVALID",
        passed=None,
        metrics={},
        result_payload={"failures": failures},
        diagnostic_task_id=None,
        failure_category="retrieval_runtime_error",
        completed_at=datetime.now(timezone.utc),
    )


def _scored_terminal(
    running: EvaluationRunEnvelope,
    payload: Mapping[str, object],
    *,
    extra_failures: list[str] | None = None,
) -> EvaluationRunEnvelope:
    metrics = _metrics(payload)
    failures = [*_threshold_failures(metrics), *(extra_failures or [])]
    passed = not failures
    query_results = payload.get("queryResults", [])
    result_payload: dict[str, object] = {
        "failures": failures,
        "queryResults": query_results,
    }
    corpus_metadata = payload.get("corpusMetadata")
    if isinstance(corpus_metadata, Mapping):
        result_payload["corpusMetadata"] = dict(corpus_metadata)
    return terminal_envelope(
        running=running,
        status="passed" if passed else "failed",
        validity="VALID_PASS" if passed else "VALID_FAIL",
        passed=passed,
        metrics=metrics,
        result_payload=result_payload,
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=datetime.now(timezone.utc),
    )


def _metrics(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("metrics")
    if not isinstance(value, Mapping):
        raise ValueError("Query rewrite benchmark metrics are missing.")
    return dict(cast(Mapping[str, object], value))


def _threshold_failures(metrics: Mapping[str, object]) -> list[str]:
    checks = (
        ("recallAt1", _at_least(metrics.get("recallAt1"), 0.80)),
        ("recallAt3", _at_least(metrics.get("recallAt3"), 0.90)),
        ("mrr", _at_least(metrics.get("mrr"), 0.85)),
        ("forbiddenTopOneRate", _at_most(metrics.get("forbiddenTopOneRate"), 0.05)),
        ("citationCompletenessRate", metrics.get("citationCompletenessRate") == 1.0),
    )
    return [f"{name}_below_threshold" for name, passed in checks if not passed]


def _regression_failures(
    baseline: Mapping[str, object], rewrite: Mapping[str, object]
) -> list[str]:
    checks = (
        ("recallAt1", _not_lower(rewrite.get("recallAt1"), baseline.get("recallAt1"))),
        ("recallAt3", _not_lower(rewrite.get("recallAt3"), baseline.get("recallAt3"))),
        ("mrr", _not_lower(rewrite.get("mrr"), baseline.get("mrr"))),
        (
            "forbiddenTopOneRate",
            _not_higher(
                rewrite.get("forbiddenTopOneRate"), baseline.get("forbiddenTopOneRate")
            ),
        ),
        (
            "citationCompletenessRate",
            _not_lower(
                rewrite.get("citationCompletenessRate"),
                baseline.get("citationCompletenessRate"),
            ),
        ),
    )
    return [f"{name}_regressed" for name, passed in checks if not passed]


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _at_least(value: object, minimum: float) -> bool:
    number = _number(value)
    return number is not None and number >= minimum


def _at_most(value: object, maximum: float) -> bool:
    number = _number(value)
    return number is not None and number <= maximum


def _not_lower(value: object, baseline: object) -> bool:
    left, right = _number(value), _number(baseline)
    return left is not None and right is not None and left >= right


def _not_higher(value: object, baseline: object) -> bool:
    left, right = _number(value), _number(baseline)
    return left is not None and right is not None and left <= right


def _corpus_metadata(chunks: Iterable[CorpusChunk]) -> dict[str, object]:
    """Build a content-free, order-independent fingerprint of indexed chunks."""
    identities = sorted(
        (
            str(chunk.chunk_id),
            str(chunk.document_id),
            str(chunk.source),
            str(chunk.created_at),
        )
        for chunk in chunks
    )
    canonical = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
    return {
        "corpusFingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "documentCount": len({identity[1] for identity in identities}),
        "chunkCount": len(identities),
    }


async def run_command(arguments: argparse.Namespace) -> int:
    config_path = str(arguments.config) if arguments.config is not None else None
    cases: tuple[QueryRewriteCase, ...] = load_query_rewrite_cases(arguments.cases)
    provider_config = load_llm_provider_config(config_path=config_path)
    model_configuration = {
        "chatModel": provider_config.chat_model,
        "queryRewriteModel": provider_config.query_rewrite_model,
        "embeddingModel": provider_config.embedding_model,
        "rerankModel": provider_config.rerank_model,
    }
    engine = create_memory_engine(config_path=config_path)
    recorder = EvaluationRunRecorder(
        archive=EvaluationArchive.from_config(config_path=arguments.config),
        repository=EvaluationRepository(create_memory_session_factory(engine)),
    )
    try:
        provider = build_default_llm_provider(config_path=config_path)
        vector_store = build_default_milvus_vector_store(config_path=config_path)
        tool = KnowledgeRetrievalTool(
            embedding_model=provider.create_embedding_model(),
            vector_store=vector_store,
            rerank_model=provider.create_rerank_model(),
        )
        rewrite_model = create_query_rewrite_model(provider)
        preflight_complete = False
        corpus_metadata: dict[str, object] = {}

        async def preflight() -> None:
            nonlocal preflight_complete, corpus_metadata
            if preflight_complete:
                return
            chunks = await asyncio.to_thread(
                vector_store.list_chunks,
                tenant_id=arguments.owner_user_id,
                knowledge_base_ids=(arguments.knowledge_base_id,),
            )
            validate_indexed_labels(
                cases, indexed_sources=tuple(sorted({chunk.source for chunk in chunks}))
            )
            corpus_metadata = _corpus_metadata(chunks)
            preflight_complete = True

        async def baseline() -> dict[str, object]:
            await preflight()
            result = await run_query_rewrite_arm(
                tool,
                cases=cases,
                owner_user_id=arguments.owner_user_id,
                knowledge_base_id=arguments.knowledge_base_id,
            )
            result["corpusMetadata"] = corpus_metadata
            return result

        async def rewrite() -> dict[str, object]:
            await preflight()
            result = await run_query_rewrite_arm(
                tool,
                cases=cases,
                owner_user_id=arguments.owner_user_id,
                knowledge_base_id=arguments.knowledge_base_id,
                transformer_factory=lambda context: AdaptiveKnowledgeQueryTransformer(
                    StructuredQueryRewriter(
                        rewrite_model,
                        timeout_seconds=25.0,
                        structured_output_method=query_rewrite_structured_output_method(
                            provider
                        ),
                    ),
                    context=context,
                ),
            )
            result["corpusMetadata"] = corpus_metadata
            return result

        payload, exit_code = await _run_query_rewrite_pair(
            run_id=arguments.run_id or f"eval-{uuid4().hex}",
            cases_path=arguments.cases,
            owner_user_id=arguments.owner_user_id,
            knowledge_base_id=arguments.knowledge_base_id,
            model_configuration=model_configuration,
            execute_baseline=baseline,
            execute_rewrite=rewrite,
            recorder=recorder,
        )
    finally:
        await engine.dispose()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{serialized}\n", encoding="utf-8")
    return exit_code


async def _run_with_sigterm(arguments: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run_command(arguments))
    previous = signal.getsignal(signal.SIGTERM)

    def cancel_active_run(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(task.cancel)

    try:
        signal.signal(signal.SIGTERM, cancel_active_run)
    except (ValueError, OSError):
        return await task
    try:
        return await task
    except asyncio.CancelledError as exc:
        raise KeyboardInterrupt from exc
    finally:
        signal.signal(signal.SIGTERM, previous)


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return asyncio.run(_run_with_sigterm(arguments))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"error": "query_rewrite_benchmark_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
