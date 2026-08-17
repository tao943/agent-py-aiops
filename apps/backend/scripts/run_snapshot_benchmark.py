"""Run AgentPy Snapshot benchmarks through the production diagnostic workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.cli import (
    evaluation_exit_code,
    evaluation_result_payload,
    safe_failure_payload,
)
from super_ai.evaluation.history import (
    interrupted_envelope,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.recording import EvaluationRunRecorder
from super_ai.evaluation.runner import (
    AgentVersion,
    ApplicationDiagnosticAdapter,
    BenchmarkRunError,
    NullKnowledgeRetrievalTool,
    SnapshotBenchmarkRunner,
)
from super_ai.llm import build_default_llm_provider, load_llm_provider_config
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalTool
from super_ai.vector_store import build_default_milvus_vector_store

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_ROOT = REPOSITORY_ROOT / "benchmarks" / "agentpy" / "scenarios"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AgentPy Snapshot SRE benchmark.")
    parser.add_argument("--scenario", required=True, help="Scenario ID, for example APY-003.")
    parser.add_argument("--suite-version", default="v1", help="Benchmark suite version.")
    parser.add_argument("--runs", type=int, default=1, help="Number of repeated runs.")
    parser.add_argument("--output", type=Path, help="Optional UTF-8 JSON report path.")
    parser.add_argument(
        "--adapter",
        choices=("application",),
        default="application",
        help="Production diagnostic adapter.",
    )
    parser.add_argument("--config", type=Path, help="Optional project configuration path.")
    parser.add_argument(
        "--rag-mode",
        choices=("off", "on"),
        default="on",
        help="Disable or enable the production knowledge retrieval path.",
    )
    parser.add_argument(
        "--owner-user-id",
        help="Explicit authorized knowledge owner for RAG-on runs.",
    )
    parser.add_argument(
        "--knowledge-base-id",
        help="Authorized knowledge base; defaults to kb_<owner-user-id>.",
    )
    return parser


async def run_command(arguments: argparse.Namespace) -> int:
    if arguments.runs < 1:
        raise ValueError("--runs must be at least 1.")
    config_path = str(arguments.config) if arguments.config is not None else None
    owner_user_id = arguments.owner_user_id
    knowledge_base_id = arguments.knowledge_base_id or (
        f"kb_{owner_user_id}" if owner_user_id else None
    )
    if arguments.rag_mode == "on" and (
        not owner_user_id or not knowledge_base_id
    ):
        raise ValueError(
            "RAG-on requires an explicit authorized owner and knowledge base."
        )
    resolved_owner = owner_user_id or "benchmark:snapshot-rag-off"
    engine = create_memory_engine(config_path=config_path)
    session_factory = create_memory_session_factory(engine)
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        provider_config = load_llm_provider_config(config_path=config_path)
        llm_provider = build_default_llm_provider(config_path=config_path)
        retrieval_tool = (
            NullKnowledgeRetrievalTool()
            if arguments.rag_mode == "off"
            else KnowledgeRetrievalTool(
                embedding_model=llm_provider.create_embedding_model(),
                vector_store=build_default_milvus_vector_store(config_path=config_path),
                rerank_model=llm_provider.create_rerank_model(),
            )
        )
        adapter = ApplicationDiagnosticAdapter(
            repositories=repositories,
            llm_provider=llm_provider,
            retrieval_tool=retrieval_tool,
            owner_user_id=resolved_owner,
            accessible_knowledge_base_ids=(
                (knowledge_base_id,) if knowledge_base_id is not None else ()
            ),
        )
        runner = SnapshotBenchmarkRunner(
            scenario_root=SCENARIO_ROOT,
            adapter=adapter,
        )
        recorder = EvaluationRunRecorder(
            archive=EvaluationArchive.from_config(config_path=arguments.config),
            repository=EvaluationRepository(session_factory),
        )
        agent_version = AgentVersion(
            git_sha=_git_sha(),
            workflow_version="agentpy-domainbench-v1",
        )
        model_configuration: dict[str, object] = {
            "provider": provider_config.provider,
            "model": provider_config.chat_model,
            "baseUrl": provider_config.base_url,
            "temperature": provider_config.temperature,
        }
        reports: list[dict[str, object]] = []
        database_pending = False
        for _ in range(arguments.runs):
            run_id = f"eval-{uuid4().hex}"
            report, pending = await _run_snapshot_once(
                scenario_id=arguments.scenario,
                suite_version=arguments.suite_version,
                rag_mode=arguments.rag_mode,
                run_id=run_id,
                agent_version=agent_version,
                model_configuration=model_configuration,
                runner=runner,
                recorder=recorder,
            )
            reports.append(report)
            database_pending = database_pending or pending
    finally:
        await engine.dispose()

    payload: dict[str, object] = {
        "scenario": arguments.scenario,
        "suiteVersion": arguments.suite_version,
        "ragMode": arguments.rag_mode,
        "runs": reports,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{serialized}\n", encoding="utf-8")
    return 2 if database_pending else evaluation_exit_code(reports)


async def _run_snapshot_once(
    *,
    scenario_id: str,
    suite_version: str,
    rag_mode: str,
    run_id: str,
    agent_version: AgentVersion,
    model_configuration: dict[str, object],
    runner: SnapshotBenchmarkRunner,
    recorder: EvaluationRunRecorder,
) -> tuple[dict[str, object], bool]:
    created_at = datetime.now(timezone.utc)
    running = running_envelope(
        run_id=run_id,
        evaluation_kind="snapshot",
        scenario_id=scenario_id,
        suite_version=suite_version,
        metadata={
            "gitSha": agent_version.git_sha,
            "workflowVersion": agent_version.workflow_version,
            "modelConfiguration": model_configuration,
            "ragMode": rag_mode,
        },
        created_at=created_at,
        started_at=created_at,
    )
    start_outcome = await recorder.start(running)
    monotonic_start = monotonic()
    try:
        outcome = await runner.run(scenario_id, run_id=run_id)
    except BenchmarkRunError as exc:
        terminal = terminal_envelope(
            running=running,
            status="agent_failed" if exc.status == "agent_failed" else "infra_invalid",
            validity="INFRA_INVALID",
            passed=None,
            metrics={},
            result_payload={},
            diagnostic_task_id=None,
            failure_category=exc.category,
            completed_at=datetime.now(timezone.utc),
        )
        finish_outcome = await recorder.fail(terminal)
        return (
            {
                "scenario": scenario_id,
                "runId": run_id,
                "status": terminal.status,
                "failureCategory": exc.category,
                "validity": "invalid",
                "passed": False,
            },
            start_outcome.database_pending or finish_outcome.database_pending,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        terminal = interrupted_envelope(
            running,
            completed_at=datetime.now(timezone.utc),
        )
        await recorder.fail(terminal)
        raise

    result = outcome.result
    terminal = terminal_envelope(
        running=running,
        status="passed" if result.passed else "failed",
        validity=result.validity,
        passed=result.passed,
        metrics={
            "outcome": result.outcome,
            "diagnosis": result.diagnosis,
            "evidence": result.evidence,
            "process": result.process,
            "safety": result.safety,
            "efficiency": result.efficiency,
            "total": result.total,
            "rawTotal": result.raw_total,
        },
        result_payload={
            "failures": list(result.failures),
            "scoreReasons": [
                {
                    "code": reason.code,
                    "points": reason.points,
                    "maximum": reason.maximum,
                    "evidenceIds": list(reason.evidence_ids),
                }
                for reason in result.reasons
            ],
            "hardGate": result.hard_gate,
        },
        diagnostic_task_id=outcome.diagnostic_task_id,
        failure_category=None,
        completed_at=datetime.now(timezone.utc),
    )
    finish_outcome = await recorder.finish(terminal)
    report = evaluation_result_payload(
        scenario_id=scenario_id,
        run_id=run_id,
        duration_ms=round((monotonic() - monotonic_start) * 1_000),
        result=result,
    )
    return report, start_outcome.database_pending or finish_outcome.database_pending


def _git_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        return asyncio.run(_run_with_sigterm(arguments))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps(safe_failure_payload(exc), ensure_ascii=False))
        return 2


async def _run_with_sigterm(arguments: argparse.Namespace) -> int:
    """Route supported SIGTERM delivery through the interrupt persistence path."""
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


if __name__ == "__main__":
    raise SystemExit(main())
