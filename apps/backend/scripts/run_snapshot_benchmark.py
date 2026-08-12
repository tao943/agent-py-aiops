"""Run AgentPy Snapshot benchmarks through the production diagnostic workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from time import monotonic
from uuid import uuid4

from super_ai.evaluation.cli import (
    evaluation_exit_code,
    evaluation_result_payload,
    safe_failure_payload,
)
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.runner import (
    AgentVersion,
    ApplicationDiagnosticAdapter,
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
    return parser


async def run_command(arguments: argparse.Namespace) -> int:
    if arguments.runs < 1:
        raise ValueError("--runs must be at least 1.")
    config_path = str(arguments.config) if arguments.config is not None else None
    engine = create_memory_engine(config_path=config_path)
    session_factory = create_memory_session_factory(engine)
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        provider_config = load_llm_provider_config(config_path=config_path)
        llm_provider = build_default_llm_provider(config_path=config_path)
        retrieval_tool = KnowledgeRetrievalTool(
            embedding_model=llm_provider.create_embedding_model(),
            vector_store=build_default_milvus_vector_store(config_path=config_path),
            rerank_model=llm_provider.create_rerank_model(),
        )
        adapter = ApplicationDiagnosticAdapter(
            repositories=repositories,
            llm_provider=llm_provider,
            retrieval_tool=retrieval_tool,
        )
        runner = SnapshotBenchmarkRunner(
            scenario_root=SCENARIO_ROOT,
            adapter=adapter,
            persistence=EvaluationRepository(session_factory),
            suite_version=arguments.suite_version,
            model_configuration={
                "provider": provider_config.provider,
                "model": provider_config.chat_model,
                "base_url": provider_config.base_url,
                "temperature": provider_config.temperature,
            },
        )
        agent_version = AgentVersion(
            git_sha=_git_sha(),
            workflow_version="agentpy-domainbench-v1",
        )
        reports: list[dict[str, object]] = []
        for _ in range(arguments.runs):
            run_id = f"eval-{uuid4().hex}"
            started_at = monotonic()
            result = await runner.run(
                arguments.scenario,
                agent_version=agent_version,
                run_id=run_id,
            )
            reports.append(
                evaluation_result_payload(
                    scenario_id=arguments.scenario,
                    run_id=run_id,
                    duration_ms=round((monotonic() - started_at) * 1_000),
                    result=result,
                )
            )
    finally:
        await engine.dispose()

    payload: dict[str, object] = {
        "scenario": arguments.scenario,
        "suiteVersion": arguments.suite_version,
        "runs": reports,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{serialized}\n", encoding="utf-8")
    return evaluation_exit_code(reports)


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
        return asyncio.run(run_command(arguments))
    except Exception as exc:
        print(json.dumps(safe_failure_payload(exc), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
