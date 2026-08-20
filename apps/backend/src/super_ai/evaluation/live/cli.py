"""Safe command boundary for manually triggered Docker Live evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from super_ai.aiops.execution import ExecutionCoordinator
from super_ai.aiops.investigation import StrategyMode
from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history import (
    EvaluationStatus,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.history import (
    validate_run_id as validate_evaluation_id,
)
from super_ai.evaluation.live.cls_evidence import (
    LiveClsEvidencePreparer,
    LiveClsLogUploader,
    LiveClsRecordProvider,
    McpClsSearcher,
)
from super_ai.evaluation.live.diagnostics import (
    ApplicationLiveDiagnosticAdapter,
    LivePostgresEvidenceMcpClient,
)
from super_ai.evaluation.live.domain import EvidenceSource
from super_ai.evaluation.live.evidence_client import LiveMcpClient
from super_ai.evaluation.live.failure_diagnostics import normalize_public_failed_checks
from super_ai.evaluation.live.nginx_timeout import (
    NginxProposalRecoveryService,
    NginxTimeoutEvidenceMcpClient,
    NginxTimeoutLiveConfig,
    NginxTimeoutScenarioDriver,
)
from super_ai.evaluation.live.order_pool_leak import (
    ComposeServiceRestarter,
    HttpOrderApiControl,
    OrderPoolClsRecordProvider,
    OrderPoolLeakScenarioDriver,
    OrderPoolLiveConfig,
    OrderPoolRecoveryService,
    OrderPoolRuntimeEvidenceMcpClient,
    PostgresOrderPoolObserver,
)
from super_ai.evaluation.live.postgres import (
    PostgresConnectionConfig,
    PostgresLiveRecoveryService,
    PostgresLockScenarioDriver,
)
from super_ai.evaluation.live.postgres_deadlock import (
    PostgresDeadlockEvidenceMcpClient,
    PostgresDeadlockRecoveryService,
    PostgresDeadlockScenarioDriver,
)
from super_ai.evaluation.live.redis_maxclients import (
    RedisLiveConfig,
    RedisMaxclientsEvidenceMcpClient,
    RedisMaxclientsRecoveryService,
    RedisMaxclientsScenarioDriver,
)
from super_ai.evaluation.live.registry import (
    LiveScenarioComponents,
    LiveScenarioRegistry,
)
from super_ai.evaluation.live.runner import (
    LiveBenchmarkError,
    LiveBenchmarkRunner,
    LiveEvidencePreparer,
    LocalLiveEvidencePreparer,
)
from super_ai.evaluation.live.scenarios import validate_run_id
from super_ai.evaluation.live.scoring import LiveEvaluationResult, score_live_run
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.recording import EvaluationRunRecorder
from super_ai.llm import build_default_llm_provider
from super_ai.mcp_client import LocalMcpClient
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.repositories import AiopsRuntimeRepositoryProvider
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.project_config import (
    load_project_config,
    project_config_section,
    required_int,
    required_str,
)
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
        "evidenceSource",
        "validity",
        "failureCategory",
        "failureStage",
        "authorizationCode",
    }
)


def build_live_recovery_coordinator(
    *,
    runtime_provider: AiopsRuntimeRepositoryProvider,
    owner_user_id: str,
    diagnostic_task_id: str,
    run_id: str,
) -> ExecutionCoordinator:
    repository = runtime_provider.execution_repository(
        owner_user_id=owner_user_id,
        task_id=diagnostic_task_id,
        graph_version="live-eval-v1",
    )
    return ExecutionCoordinator(repository, worker_id=f"live-recovery:{run_id}")


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
            command.add_argument(
                "--campaign-id",
                type=validate_evaluation_id,
            )
            command.add_argument(
                "--evidence-source",
                choices=("local", "cls"),
                default="local",
            )
            command.add_argument(
                "--strategy",
                choices=("auto", "single", "multi"),
                default="auto",
            )
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
        safe_result = {
            key: value for key, value in result.items() if key in _SAFE_RESULT_FIELDS
        }
        failed_checks = normalize_public_failed_checks(result.get("failedChecks"))
        if failed_checks is not None:
            safe_result["failedChecks"] = failed_checks
        payload["result"] = safe_result
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
        exit_code = 0 if payload.get("status") == "passed" else (
            2 if payload.get("status") == "infra_invalid" else 1
        )
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


async def _run_live_command(
    arguments: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config_path = cast(str | None, arguments.config)
    evidence_source = cast(EvidenceSource, arguments.evidence_source)
    engine = create_memory_engine(config_path=config_path)
    session_factory = create_memory_session_factory(engine)
    recorder = EvaluationRunRecorder(
        archive=EvaluationArchive.from_config(config_path=config_path),
        repository=EvaluationRepository(session_factory),
    )

    async def execute() -> LiveEvaluationResult:
        components = build_live_scenario_registry().resolve(
            cast(str, arguments.scenario)
        )
        evidence_preparer, cls_mcp_client = build_live_evidence_runtime(
            evidence_source=evidence_source,
            config_path=config_path,
            record_provider=components.cls_record_provider,
        )
        repositories = create_sqlalchemy_memory_repositories(session_factory)
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
            workflow_version="evidence-driven-v4",
            investigation_strategy=cast(StrategyMode, arguments.strategy),
            cls_mcp_client=cls_mcp_client,
            component_evidence_factory=components.component_evidence_factory,
        )
        runtime_provider = repositories.aiops_runtime
        if runtime_provider is None:
            raise RuntimeError("AIOps runtime repository is required for Live recovery.")

        def recovery_coordinator_factory(task_id: str) -> ExecutionCoordinator:
            return build_live_recovery_coordinator(
                runtime_provider=runtime_provider,
                owner_user_id=cast(str, arguments.owner_user_id),
                diagnostic_task_id=task_id,
                run_id=cast(str, arguments.run_id),
            )

        runner = LiveBenchmarkRunner[LiveEvaluationResult](
            scenario_root=LIVE_SCENARIO_ROOT,
            driver=components.driver,
            evidence_preparer=evidence_preparer,
            diagnostic=diagnostic,
            recovery=components.recovery,
            evaluator=_LiveScoringEvaluator(
                evidence_source,
                investigation_strategy=cast(StrategyMode, arguments.strategy),
            ),
            recovery_coordinator_factory=recovery_coordinator_factory,
        )
        return await runner.run(
            cast(str, arguments.scenario),
            run_id=cast(str, arguments.run_id),
        )

    try:
        payload, exit_code = await _run_live_once(
            scenario_id=cast(str, arguments.scenario),
            run_id=cast(str, arguments.run_id),
            evidence_source=evidence_source,
            execute=execute,
            recorder=recorder,
            campaign_id=cast(str | None, arguments.campaign_id),
            investigation_strategy=cast(StrategyMode, arguments.strategy),
        )
    finally:
        await engine.dispose()
    write_safe_report(LIVE_REPORT_ROOT / f"{arguments.run_id}.json", payload)
    return payload, exit_code


async def _run_live_once(
    *,
    scenario_id: str,
    run_id: str,
    evidence_source: EvidenceSource,
    execute: Callable[[], Awaitable[LiveEvaluationResult]],
    recorder: EvaluationRunRecorder,
    campaign_id: str | None = None,
    investigation_strategy: StrategyMode = "auto",
) -> tuple[dict[str, object], int]:
    timestamp = datetime.now(timezone.utc)
    metadata: dict[str, object] = {
        "workflowVersion": "live-v1",
        "evidenceSource": evidence_source,
        "investigationStrategy": investigation_strategy,
        "investigationPolicyVersion": "investigation-router-v1",
    }
    if campaign_id is not None:
        metadata["acceptanceCampaignId"] = campaign_id
    running = running_envelope(
        run_id=run_id,
        evaluation_kind="live",
        scenario_id=scenario_id,
        suite_version="v1",
        metadata=metadata,
        created_at=timestamp,
        started_at=timestamp,
    )
    start = await recorder.start(running)
    try:
        result = await execute()
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        await recorder.fail(
            terminal_envelope(
                running=running,
                status="interrupted",
                validity=None,
                passed=None,
                metrics=_cleanup_metrics(exc),
                result_payload={},
                diagnostic_task_id=None,
                failure_category=None,
                completed_at=datetime.now(timezone.utc),
            )
        )
        raise
    except LiveBenchmarkError as exc:
        payload, exit_code = _live_failure_payload(
            scenario_id=scenario_id,
            run_id=run_id,
            evidence_source=evidence_source,
            error=exc,
        )
        status, validity, _ = classify_live_failure(
            exc.category, evidence_source=evidence_source
        )
        result_payload: dict[str, object] = {"failures": [exc.category]}
        if exc.stage is not None:
            result_payload["failureStage"] = exc.stage
        if exc.authorization_code is not None:
            result_payload["authorizationCode"] = exc.authorization_code
        result_payload.update(_failure_diagnostic_payload(exc))
        terminal = terminal_envelope(
            running=running,
            status=cast(EvaluationStatus, status),
            validity=validity,
            passed=False if status == "failed" else None,
            metrics=_cleanup_metrics(exc),
            result_payload=result_payload,
            diagnostic_task_id=None,
            failure_category=exc.category,
            completed_at=datetime.now(timezone.utc),
        )
        finish = await recorder.fail(terminal)
        return payload, 2 if start.database_pending or finish.database_pending else exit_code
    except Exception as exc:
        terminal = terminal_envelope(
            running=running,
            status="infra_invalid",
            validity="INFRA_INVALID",
            passed=None,
            metrics=_cleanup_metrics(exc),
            result_payload={"failures": ["live_runtime_error"]},
            diagnostic_task_id=None,
            failure_category="live_runtime_error",
            completed_at=datetime.now(timezone.utc),
        )
        await recorder.fail(terminal)
        return (
            safe_output(
                command="run",
                scenario_id=scenario_id,
                run_id=run_id,
                status="infra_invalid",
                result={
                    "validity": "INFRA_INVALID",
                    "failureCategory": "live_runtime_error",
                    "evidenceSource": evidence_source,
                },
            ),
            2,
        )

    live_result = _live_result_payload(result, evidence_source=evidence_source)
    status = "passed" if result.passed else "failed"
    metrics: dict[str, object] = {
        "total": result.total,
        "rawTotal": result.raw_total,
        "verificationPassed": result.recovery_verification == 15,
        "cleanupSucceeded": True,
    }
    investigation_metrics = result.investigation_metrics
    if investigation_metrics is not None:
        if investigation_metrics.strategy != investigation_strategy:
            raise ValueError("Investigation metric strategy does not match the run request.")
        metrics.update(
            {
                "rootCauseTop1Correct": investigation_metrics.root_cause_top1_correct,
                "evidenceRecallBasisPoints": (
                    investigation_metrics.evidence_recall_basis_points
                ),
                "durationMs": investigation_metrics.duration_ms,
                "modelCallCount": investigation_metrics.model_call_count,
                "duplicateEvidenceBasisPoints": (
                    investigation_metrics.duplicate_evidence_basis_points
                ),
                "fallbackReason": investigation_metrics.fallback_reason,
                "effectiveInvestigationStrategy": (
                    investigation_metrics.effective_strategy
                ),
                "securityHardGatePassed": (
                    investigation_metrics.security_hard_gate_passed
                ),
            }
        )
    terminal = terminal_envelope(
        running=running,
        status=cast(EvaluationStatus, status),
        validity="VALID_PASS" if result.passed else "VALID_FAIL",
        passed=result.passed,
        metrics=metrics,
        result_payload={
            "failures": list(result.failures),
            "hardGate": result.hard_gate,
        },
        diagnostic_task_id=result.diagnostic_task_id,
        failure_category=None,
        completed_at=datetime.now(timezone.utc),
    )
    finish = await recorder.finish(terminal)
    payload = safe_output(
        command="run",
        scenario_id=scenario_id,
        run_id=run_id,
        status=status,
        result=live_result,
    )
    exit_code = 2 if start.database_pending or finish.database_pending else (
        0 if result.passed else 1
    )
    return payload, exit_code


def _cleanup_metrics(error: BaseException) -> dict[str, object]:
    cleanup_succeeded = getattr(error, "cleanup_succeeded", None)
    if isinstance(cleanup_succeeded, bool):
        return {"cleanupSucceeded": cleanup_succeeded}
    return {}


def _failure_diagnostic_payload(error: LiveBenchmarkError) -> dict[str, object]:
    diagnostics = error.diagnostics
    return diagnostics.to_result_payload() if diagnostics is not None else {}


def _public_failure_diagnostic_payload(error: LiveBenchmarkError) -> dict[str, object]:
    diagnostics = error.diagnostics
    if diagnostics is None:
        return {}
    return {"failedChecks": list(diagnostics.failed_checks)}


class _LiveScoringEvaluator:
    def __init__(
        self,
        evidence_source: EvidenceSource,
        *,
        investigation_strategy: StrategyMode = "auto",
    ) -> None:
        self._evidence_source: EvidenceSource = evidence_source
        self._investigation_strategy: StrategyMode = investigation_strategy

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
            evidence_source=self._evidence_source,
            investigation_strategy=self._investigation_strategy,
        )


def _live_result_payload(
    result: LiveEvaluationResult,
    *,
    evidence_source: EvidenceSource,
) -> dict[str, object]:
    raw = asdict(result)
    return {
        "total": raw["total"],
        "rawTotal": raw["raw_total"],
        "passed": raw["passed"],
        "hardGate": raw["hard_gate"],
        "failures": raw["failures"],
        "verificationPassed": result.recovery_verification == 15,
        "cleanupSucceeded": True,
        "evidenceSource": evidence_source,
        "validity": "VALID_PASS" if result.passed else "VALID_FAIL",
    }


def classify_live_failure(
    category: str,
    *,
    evidence_source: EvidenceSource,
) -> tuple[str, str, int]:
    infrastructure = category.startswith("cls_") or (
        evidence_source == "cls" and category == "evidence_preparation_failed"
    )
    if infrastructure:
        return "infra_invalid", "INFRA_INVALID", 2
    return "failed", "VALID_FAIL", 1


def _live_failure_payload(
    *,
    scenario_id: str,
    run_id: str,
    evidence_source: EvidenceSource,
    error: LiveBenchmarkError,
) -> tuple[dict[str, object], int]:
    status, validity, exit_code = classify_live_failure(
        error.category,
        evidence_source=evidence_source,
    )
    result: dict[str, object] = {
        "evidenceSource": evidence_source,
        "validity": validity,
        "failureCategory": error.category,
    }
    if error.stage is not None:
        result["failureStage"] = error.stage
    if error.authorization_code is not None:
        result["authorizationCode"] = error.authorization_code
    result.update(_public_failure_diagnostic_payload(error))
    return (
        safe_output(
            command="run",
            scenario_id=scenario_id,
            run_id=run_id,
            status=status,
            result=result,
        ),
        exit_code,
    )


def build_live_evidence_runtime(
    *,
    evidence_source: EvidenceSource,
    config_path: str | Path | None,
    record_provider: LiveClsRecordProvider | None = None,
) -> tuple[LiveEvidencePreparer, LiveMcpClient | None]:
    if evidence_source == "local":
        return LocalLiveEvidencePreparer(), None
    upload = project_config_section("clsLogUpload", config_path=config_path)
    credentials = project_config_section("clsMcpServer", config_path=config_path)
    mcp = project_config_section("mcp", config_path=config_path)
    project_config = load_project_config(config_path)
    live: Mapping[str, object]
    if "liveClsEvidence" in project_config:
        live = project_config_section("liveClsEvidence", config_path=config_path)
    else:
        live = {
            "pollIntervalSeconds": 2,
            "indexWaitSeconds": 90,
            "queryLimit": 20,
        }
    cls_client = LocalMcpClient(
        required_str(mcp, "clsSseUrl"),
        timeout_seconds=required_int(mcp, "timeoutSeconds"),
        retries=required_int(mcp, "retries"),
    )
    preparer = LiveClsEvidencePreparer(
        region=required_str(upload, "region"),
        topic_id=required_str(upload, "topicId"),
        uploader=LiveClsLogUploader(
            endpoint=required_str(upload, "endpoint"),
            topic_id=required_str(upload, "topicId"),
            secret_id=required_str(credentials, "secretId"),
            secret_key=required_str(credentials, "secretKey"),
        ),
        searcher=McpClsSearcher(cls_client, limit=required_int(live, "queryLimit")),
        timeout_seconds=float(required_int(live, "indexWaitSeconds")),
        poll_interval_seconds=float(required_int(live, "pollIntervalSeconds")),
        record_provider=record_provider,
    )
    return preparer, cls_client


def build_live_scenario_registry() -> LiveScenarioRegistry:
    """Build explicitly supported Live runtimes without a default fallback."""
    registry = LiveScenarioRegistry()

    def postgres_lock_components() -> LiveScenarioComponents:
        driver = PostgresLockScenarioDriver(_postgres_config_from_environment())
        return LiveScenarioComponents(
            driver_name="postgres_lock_wait",
            driver=driver,
            recovery=PostgresLiveRecoveryService(driver),
            component_evidence_factory=LivePostgresEvidenceMcpClient,
        )

    registry.register("APY-LIVE-PG-LOCK-001", postgres_lock_components)

    def postgres_deadlock_components() -> LiveScenarioComponents:
        driver = PostgresDeadlockScenarioDriver(_postgres_config_from_environment())
        return LiveScenarioComponents(
            driver_name="postgres_deadlock",
            driver=driver,
            recovery=PostgresDeadlockRecoveryService(driver),
            component_evidence_factory=PostgresDeadlockEvidenceMcpClient,
        )

    registry.register("APY-LIVE-PG-DEADLOCK-001", postgres_deadlock_components)

    def redis_maxclients_components() -> LiveScenarioComponents:
        driver = RedisMaxclientsScenarioDriver(_redis_config_from_environment())
        return LiveScenarioComponents(
            driver_name="redis_maxclients",
            driver=driver,
            recovery=RedisMaxclientsRecoveryService(driver),
            component_evidence_factory=RedisMaxclientsEvidenceMcpClient,
        )

    registry.register("APY-LIVE-REDIS-MAXCLIENTS-001", redis_maxclients_components)

    def nginx_timeout_components() -> LiveScenarioComponents:
        driver = NginxTimeoutScenarioDriver(_nginx_config_from_environment())
        return LiveScenarioComponents(
            driver_name="nginx_timeout",
            driver=driver,
            recovery=NginxProposalRecoveryService(),
            component_evidence_factory=NginxTimeoutEvidenceMcpClient,
        )

    registry.register("APY-LIVE-NGINX-TIMEOUT-001", nginx_timeout_components)

    def order_pool_components() -> LiveScenarioComponents:
        config = _order_pool_config_from_environment()
        driver = OrderPoolLeakScenarioDriver(
            config,
            api=HttpOrderApiControl(config),
            postgres=PostgresOrderPoolObserver(_postgres_config_from_environment()),
        )
        return LiveScenarioComponents(
            driver_name="order_pool_leak",
            driver=driver,
            recovery=OrderPoolRecoveryService(
                driver,
                ComposeServiceRestarter(config),
            ),
            component_evidence_factory=OrderPoolRuntimeEvidenceMcpClient,
            cls_record_provider=OrderPoolClsRecordProvider(driver),
        )

    registry.register("APY-LIVE-ORDER-POOL-LEAK-001", order_pool_components)
    return registry


async def _run_infrastructure_command(
    command: str,
    scenario_id: str,
    run_id: str,
) -> tuple[dict[str, object], int]:
    identity = validate_run_id(run_id)
    components = build_live_scenario_registry().resolve(scenario_id)
    if command == "cleanup":
        cleanup = await components.driver.cleanup(identity)
        verification_passed = cleanup.passed
        cleanup_succeeded: bool | None = cleanup.passed
    else:
        report = read_safe_report(LIVE_REPORT_ROOT / f"{identity.run_id}.json")
        result = report.get("result")
        typed_result: Mapping[str, object]
        if isinstance(result, Mapping):
            typed_result = cast(Mapping[str, object], result)
        else:
            typed_result = cast(Mapping[str, object], {})
        report_matches = (
            report.get("scenarioId") == scenario_id
            and report.get("runId") == identity.run_id
        )
        verification_passed = (
            report_matches
            and typed_result.get("verificationPassed") is True
            and typed_result.get("cleanupSucceeded") is True
        )
        cleanup_succeeded = None
    payload = safe_output(
        command=command,
        scenario_id=scenario_id,
        run_id=run_id,
        status="clean" if verification_passed else "residual_detected",
        result={
            "verificationPassed": verification_passed,
            "cleanupSucceeded": cleanup_succeeded,
        },
    )
    return payload, 0 if verification_passed else 1


def _postgres_config_from_environment() -> PostgresConnectionConfig:
    return PostgresConnectionConfig(
        host=os.getenv("LIVE_POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("LIVE_POSTGRES_PORT", "5432")),
        user=os.getenv("LIVE_POSTGRES_USER", "agent_py"),
        password=os.getenv("LIVE_POSTGRES_PASSWORD", "agent_py_dev"),
        database="agent_py_live_eval",
    )


def _redis_config_from_environment() -> RedisLiveConfig:
    return RedisLiveConfig(
        url=os.getenv("LIVE_REDIS_URL", "redis://127.0.0.1:16379/0")
    )


def _nginx_config_from_environment() -> NginxTimeoutLiveConfig:
    return NginxTimeoutLiveConfig(
        gateway_url=os.getenv(
            "LIVE_NGINX_GATEWAY_URL", "http://127.0.0.1:18080"
        ),
        upstream_url=os.getenv(
            "LIVE_NGINX_UPSTREAM_URL", "http://127.0.0.1:18081"
        ),
    )


def _order_pool_config_from_environment() -> OrderPoolLiveConfig:
    return OrderPoolLiveConfig(
        base_url=os.getenv("LIVE_ORDER_API_URL", "http://127.0.0.1:18082"),
        control_token=os.getenv(
            "LIVE_ORDER_API_CONTROL_TOKEN", "agentpy-live-eval-control"
        ),
        compose_file=REPOSITORY_ROOT / "infra" / "compose.yaml",
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
