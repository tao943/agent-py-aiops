"""Safe command boundary for manually triggered Docker Live evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import cast

from super_ai.evaluation.live.diagnostics import ApplicationLiveDiagnosticAdapter
from super_ai.evaluation.live.postgres import (
    PostgresConnectionConfig,
    PostgresLiveRecoveryService,
    PostgresLockScenarioDriver,
)
from super_ai.evaluation.live.runner import LiveBenchmarkRunner, LocalLiveEvidencePreparer
from super_ai.evaluation.live.scenarios import validate_run_id
from super_ai.evaluation.live.scoring import LiveEvaluationResult, score_live_run
from super_ai.llm import build_default_llm_provider
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalTool
from super_ai.vector_store import build_default_milvus_vector_store

REPOSITORY_ROOT = Path(__file__).resolve().parents[6]
LIVE_SCENARIO_ROOT = REPOSITORY_ROOT / "benchmarks" / "agentpy" / "live"
LIVE_REPORT_ROOT = REPOSITORY_ROOT / "apps" / "backend" / "var" / "benchmarks" / "live"

_SAFE_RESULT_FIELDS = frozenset(
    {
        "total",
        "rawTotal",
        "passed",
        "hardGate",
        "failures",
        "verificationPassed",
        "cleanupSucceeded",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or inspect the isolated AgentPy Docker Live benchmark."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "verify", "cleanup", "report"):
        command = subcommands.add_parser(name)
        command.add_argument("--scenario", required=True)
        command.add_argument("--run-id", required=True)
        if name == "run":
            command.add_argument("--owner-user-id", required=True)
            command.add_argument("--knowledge-base-id", required=True)
            command.add_argument("--config")
    return parser


def safe_output(
    *,
    command: str,
    scenario_id: str,
    run_id: str,
    status: str,
    result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "command": command,
        "scenarioId": scenario_id,
        "runId": run_id,
        "status": status,
    }
    if result is not None:
        payload["result"] = {
            key: value for key, value in result.items() if key in _SAFE_RESULT_FIELDS
        }
    return payload


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    command = cast(str, arguments.command)
    scenario_id = cast(str, arguments.scenario)
    identity = validate_run_id(cast(str, arguments.run_id))
    if command in {"verify", "cleanup"}:
        payload, exit_code = asyncio.run(
            _run_infrastructure_command(command, scenario_id, identity.run_id)
        )
    elif command == "run":
        payload, exit_code = asyncio.run(_run_live_command(arguments))
    else:
        report_path = LIVE_REPORT_ROOT / f"{identity.run_id}.json"
        payload = read_safe_report(report_path)
        payload["command"] = "report"
        exit_code = 0 if payload.get("status") == "passed" else 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


async def _run_live_command(
    arguments: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config_path = cast(str | None, arguments.config)
    engine = create_memory_engine(config_path=config_path)
    driver = PostgresLockScenarioDriver(_postgres_config_from_environment())
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        llm_provider = build_default_llm_provider(config_path=config_path)
        retrieval_tool = KnowledgeRetrievalTool(
            embedding_model=llm_provider.create_embedding_model(),
            vector_store=build_default_milvus_vector_store(config_path=config_path),
            rerank_model=llm_provider.create_rerank_model(),
        )
        diagnostic = ApplicationLiveDiagnosticAdapter(
            repositories=repositories,
            llm_provider=llm_provider,
            retrieval_tool=retrieval_tool,
            accessible_knowledge_base_ids=(cast(str, arguments.knowledge_base_id),),
            owner_user_id=cast(str, arguments.owner_user_id),
        )
        runner = LiveBenchmarkRunner[LiveEvaluationResult](
            scenario_root=LIVE_SCENARIO_ROOT,
            driver=driver,
            evidence_preparer=LocalLiveEvidencePreparer(),
            diagnostic=diagnostic,
            recovery=PostgresLiveRecoveryService(driver),
            evaluator=_LiveScoringEvaluator(),
        )
        result = await runner.run(
            cast(str, arguments.scenario),
            run_id=cast(str, arguments.run_id),
        )
    finally:
        await engine.dispose()
    result_payload = _live_result_payload(result)
    payload = safe_output(
        command="run",
        scenario_id=cast(str, arguments.scenario),
        run_id=cast(str, arguments.run_id),
        status="passed" if result.passed else "failed",
        result=result_payload,
    )
    write_safe_report(LIVE_REPORT_ROOT / f"{arguments.run_id}.json", payload)
    return payload, 0 if result.passed else 1


class _LiveScoringEvaluator:
    def evaluate(self, **values: object) -> LiveEvaluationResult:
        from super_ai.evaluation.artifacts import RunArtifact
        from super_ai.evaluation.domain import ScenarioOracle
        from super_ai.evaluation.live.domain import (
            LiveFaultObservation,
            LiveRecoveryRecord,
            LiveVerification,
        )

        artifact = values["diagnostic_artifact"]
        observation = values["observation"]
        recovery = values["recovery"]
        verification = values["verification"]
        oracle = values["oracle"]
        if not isinstance(artifact, RunArtifact):
            raise TypeError("Live diagnostic did not return a scoreable artifact.")
        if not isinstance(observation, LiveFaultObservation):
            raise TypeError("Live observation contract is invalid.")
        if not isinstance(recovery, LiveRecoveryRecord):
            raise TypeError("Live recovery contract is invalid.")
        if not isinstance(verification, LiveVerification):
            raise TypeError("Live verification contract is invalid.")
        if not isinstance(oracle, ScenarioOracle):
            raise TypeError("Live oracle contract is invalid.")
        return score_live_run(
            artifact,
            oracle,
            observation=observation,
            recovery=recovery,
            verification=verification,
        )


def _live_result_payload(result: LiveEvaluationResult) -> dict[str, object]:
    raw = asdict(result)
    return {
        "total": raw["total"],
        "rawTotal": raw["raw_total"],
        "passed": raw["passed"],
        "hardGate": raw["hard_gate"],
        "failures": raw["failures"],
        "verificationPassed": result.recovery_verification == 15,
        "cleanupSucceeded": True,
    }


async def _run_infrastructure_command(
    command: str,
    scenario_id: str,
    run_id: str,
) -> tuple[dict[str, object], int]:
    identity = validate_run_id(run_id)
    driver = PostgresLockScenarioDriver(_postgres_config_from_environment())
    if command == "cleanup":
        await driver.cleanup(identity)
    audit = await driver.audit(identity)
    payload = safe_output(
        command=command,
        scenario_id=scenario_id,
        run_id=run_id,
        status="clean" if audit.clean else "residual_detected",
        result={
            "verificationPassed": audit.clean,
            "cleanupSucceeded": audit.clean if command == "cleanup" else None,
        },
    )
    return payload, 0 if audit.clean else 1


def _postgres_config_from_environment() -> PostgresConnectionConfig:
    return PostgresConnectionConfig(
        host=os.getenv("LIVE_POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("LIVE_POSTGRES_PORT", "5432")),
        user=os.getenv("LIVE_POSTGRES_USER", "agent_py"),
        password=os.getenv("LIVE_POSTGRES_PASSWORD", "agent_py_dev"),
        database="agent_py_live_eval",
    )


def write_safe_report(path: Path, payload: Mapping[str, object]) -> None:
    report = _sanitize_stored_report(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def read_safe_report(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("Live report must contain a JSON object.")
    return _sanitize_stored_report(cast(Mapping[str, object], raw))


def _sanitize_stored_report(payload: Mapping[str, object]) -> dict[str, object]:
    command = payload.get("command")
    scenario_id = payload.get("scenarioId")
    run_id = payload.get("runId")
    status = payload.get("status")
    if not all(isinstance(item, str) and item for item in (command, scenario_id, run_id, status)):
        raise ValueError("Live report identity fields are invalid.")
    raw_result = payload.get("result")
    result = cast(Mapping[str, object], raw_result) if isinstance(raw_result, Mapping) else None
    return safe_output(
        command=cast(str, command),
        scenario_id=cast(str, scenario_id),
        run_id=cast(str, run_id),
        status=cast(str, status),
        result=result,
    )
