"""Safe command boundary for manually triggered Docker Live evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from super_ai.aiops.execution import ExecutionCoordinator, ExecutionIdentity, ExecutionResult
from super_ai.aiops.investigation import StrategyMode
from super_ai.alert_ingestion.metrics import AlertIngestionMetrics
from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.alert_ingestion.sqlalchemy import SQLAlchemyAlertIngestionRepository
from super_ai.chat.aiops_bridge import AiopsBridgeService
from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.artifacts import (
    InvestigationAudit,
    InvestigationBenchmarkMetrics,
    SpecialistRoleAudit,
)
from super_ai.evaluation.history import (
    EvaluationRunEnvelope,
    EvaluationStatus,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.history import (
    validate_run_id as validate_evaluation_id,
)
from super_ai.evaluation.live.auto_closure import (
    RECOVERY_GRAPH_VERSION,
    LiveAutoClosureResult,
    OrderPoolAutoClosureOrchestrator,
    PersistedDiagnosticOutcomeLoader,
)
from super_ai.evaluation.live.auto_closure_state import (
    SQLAlchemyAutoClosureStateRepository,
)
from super_ai.evaluation.live.chat_entry import ChatEntryLiveDiagnosticAdapter
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
from super_ai.evaluation.live.domain import (
    EvidenceSource,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveRunIdentity,
    LiveScenario,
)
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
from super_ai.evaluation.live.scenarios import (
    load_live_oracle,
    load_live_scenario,
    validate_run_id,
)
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
        "closedVerified",
        "correlation",
        "recoveryIntentId",
        "conversationMetrics",
    }
)


def build_live_recovery_coordinator(
    *,
    runtime_provider: AiopsRuntimeRepositoryProvider,
    owner_user_id: str,
    diagnostic_task_id: str,
    run_id: str,
    graph_version: str = "live-eval-v1",
) -> ExecutionCoordinator:
    repository = runtime_provider.execution_repository(
        owner_user_id=owner_user_id,
        task_id=diagnostic_task_id,
        graph_version=graph_version,
    )
    return ExecutionCoordinator(repository, worker_id=f"live-recovery:{run_id}")


class _TaskScopedRecoveryCoordinator:
    def __init__(
        self,
        *,
        runtime_provider: AiopsRuntimeRepositoryProvider,
        owner_user_id: str,
        run_id: str,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._owner_user_id = owner_user_id
        self._run_id = run_id

    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[dict[str, object]]],
        *,
        outcome_known_on_error: bool,
    ) -> ExecutionResult:
        coordinator = build_live_recovery_coordinator(
            runtime_provider=self._runtime_provider,
            owner_user_id=self._owner_user_id,
            diagnostic_task_id=identity.task_id,
            run_id=self._run_id,
            graph_version=RECOVERY_GRAPH_VERSION,
        )
        return await coordinator.run_once(
            identity,
            operation,
            outcome_known_on_error=outcome_known_on_error,
        )


class _ScenarioEvidencePreparer:
    def __init__(
        self,
        preparer: LiveEvidencePreparer,
        scenario: LiveScenario,
    ) -> None:
        self._preparer = preparer
        self._scenario = scenario

    async def prepare(
        self,
        identity: LiveRunIdentity,
        observation: LiveFaultObservation,
    ) -> LiveEvidenceContext:
        return await self._preparer.prepare(
            identity=identity,
            scenario=self._scenario,
            observation=observation,
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
            command.add_argument(
                "--auto-closure",
                action="store_true",
                help="Wait for the real Prometheus/Alertmanager Single-Agent closure.",
            )
            command.add_argument(
                "--resume",
                action="store_true",
                help="Resume the exact persisted automatic-closure run.",
            )
    return parser


def _auto_closure_strategy(arguments: argparse.Namespace) -> StrategyMode:
    automatic = bool(getattr(arguments, "auto_closure", False))
    resume = bool(getattr(arguments, "resume", False))
    strategy = cast(StrategyMode, arguments.strategy)
    if resume and not automatic:
        raise ValueError("--resume requires --auto-closure.")
    if not automatic:
        return strategy
    if strategy == "multi":
        raise ValueError("Automatic closure does not permit Multi Agent strategy.")
    return "single"


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


class _UnavailableIncidentQueries:
    """Report-only bridge dependency; prepared Live incidents are owned by Chat entry."""

    async def list_active(
        self, *, owner_user_id: str, limit: int
    ) -> list[AlertIncidentRecord]:
        del owner_user_id, limit
        return []

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        del owner_user_id, incident_id
        return None


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
    *,
    enter_through_chat: bool = False,
) -> tuple[dict[str, object], int]:
    if enter_through_chat and bool(arguments.auto_closure):
        return (
            safe_output(
                command="run",
                scenario_id=cast(str, arguments.scenario),
                run_id=cast(str, arguments.run_id),
                status="infra_invalid",
                result={
                    "validity": "INFRA_INVALID",
                    "failureCategory": "auto_closure_arguments_invalid",
                },
            ),
            2,
        )
    try:
        effective_strategy = _auto_closure_strategy(arguments)
    except ValueError:
        return (
            safe_output(
                command="run",
                scenario_id=cast(str, arguments.scenario),
                run_id=cast(str, arguments.run_id),
                status="infra_invalid",
                result={
                    "validity": "INFRA_INVALID",
                    "failureCategory": "auto_closure_arguments_invalid",
                },
            ),
            2,
        )
    if bool(arguments.auto_closure):
        return await _run_auto_closure_command(arguments)
    config_path = cast(str | None, arguments.config)
    evidence_source = cast(EvidenceSource, arguments.evidence_source)
    engine = create_memory_engine(config_path=config_path)
    session_factory = create_memory_session_factory(engine)
    recorder = EvaluationRunRecorder(
        archive=EvaluationArchive.from_config(config_path=config_path),
        repository=EvaluationRepository(session_factory),
    )
    conversation_metrics: dict[str, object] = {}

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
        application_diagnostic = ApplicationLiveDiagnosticAdapter(
            repositories=repositories,
            llm_provider=llm_provider,
            retrieval_tool=retrieval_tool,
            accessible_knowledge_base_ids=(cast(str, arguments.knowledge_base_id),),
            owner_user_id=cast(str, arguments.owner_user_id),
            workflow_version="evidence-driven-v4",
            investigation_strategy=effective_strategy,
            cls_mcp_client=cls_mcp_client,
            component_evidence_factory=components.component_evidence_factory,
        )
        diagnostic: ApplicationLiveDiagnosticAdapter | ChatEntryLiveDiagnosticAdapter
        chat_diagnostic: ChatEntryLiveDiagnosticAdapter | None = None
        if enter_through_chat:
            pending_repository = repositories.pending_chat_actions
            if pending_repository is None:
                raise RuntimeError("Pending Chat Action repository is required for Chat Live.")
            chat_diagnostic = ChatEntryLiveDiagnosticAdapter(
                owner_user_id=cast(str, arguments.owner_user_id),
                session_repository=repositories.chat,
                pending_repository=pending_repository,
                report_bridge=AiopsBridgeService(
                    incidents=_UnavailableIncidentQueries(),
                    diagnostics=repositories.diagnostics,
                ),
                diagnostic_delegate=application_diagnostic,
            )
            diagnostic = chat_diagnostic
        else:
            diagnostic = application_diagnostic
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
                investigation_strategy=effective_strategy,
            ),
            recovery_coordinator_factory=recovery_coordinator_factory,
        )
        result = await runner.run(
            cast(str, arguments.scenario),
            run_id=cast(str, arguments.run_id),
        )
        if chat_diagnostic is not None:
            conversation_metrics.update(chat_diagnostic.conversation_metrics())
        return result

    try:
        payload, exit_code = await _run_live_once(
            scenario_id=cast(str, arguments.scenario),
            run_id=cast(str, arguments.run_id),
            evidence_source=evidence_source,
            execute=execute,
            recorder=recorder,
            campaign_id=cast(str | None, arguments.campaign_id),
            investigation_strategy=effective_strategy,
            conversation_metrics=conversation_metrics,
        )
    finally:
        await engine.dispose()
    write_safe_report(LIVE_REPORT_ROOT / f"{arguments.run_id}.json", payload)
    return payload, exit_code


async def _run_auto_closure_command(
    arguments: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    scenario_id = cast(str, arguments.scenario)
    run_id = cast(str, arguments.run_id)
    if scenario_id != "APY-LIVE-ORDER-POOL-LEAK-001":
        return (
            safe_output(
                command="run",
                scenario_id=scenario_id,
                run_id=run_id,
                status="infra_invalid",
                result={
                    "validity": "INFRA_INVALID",
                    "failureCategory": "auto_closure_scenario_unsupported",
                },
            ),
            2,
        )
    config_path = cast(str | None, arguments.config)
    evidence_source = cast(EvidenceSource, arguments.evidence_source)
    owner_user_id = cast(str, arguments.owner_user_id)
    archive = EvaluationArchive.from_config(config_path=config_path)
    existing = None
    if bool(arguments.resume):
        try:
            existing = archive.load(run_id)
        except FileNotFoundError:
            existing = None
        if existing is not None and existing.status != "running":
            payload = safe_output(
                command="run",
                scenario_id=scenario_id,
                run_id=run_id,
                status=existing.status,
                result={
                    "validity": existing.validity or "INFRA_INVALID",
                    "passed": existing.passed is True,
                    "authorizationCode": existing.result_payload.get(
                        "authorizationCode"
                    ),
                    "closedVerified": existing.result_payload.get("closedVerified"),
                    "correlation": existing.result_payload.get("correlation"),
                    "recoveryIntentId": existing.result_payload.get("recoveryIntentId"),
                    "evidenceSource": evidence_source,
                },
            )
            exit_code = (
                0
                if existing.status == "passed"
                else 2
                if existing.status == "infra_invalid"
                else 1
            )
            return payload, exit_code

    engine = create_memory_engine(config_path=config_path)
    session_factory = create_memory_session_factory(engine)
    recorder = EvaluationRunRecorder(
        archive=archive,
        repository=EvaluationRepository(session_factory),
    )
    try:
        components = build_live_scenario_registry().resolve(scenario_id)
        driver = cast(OrderPoolLeakScenarioDriver, components.driver)
        recovery = cast(OrderPoolRecoveryService, components.recovery)
        scenario_path = LIVE_SCENARIO_ROOT / scenario_id
        scenario = load_live_scenario(scenario_path)
        oracle = load_live_oracle(scenario_path)
        base_preparer, _ = build_live_evidence_runtime(
            evidence_source=evidence_source,
            config_path=config_path,
            record_provider=components.cls_record_provider,
        )
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        runtime_provider = repositories.aiops_runtime
        if runtime_provider is None:
            raise RuntimeError("AIOps runtime repository is required for auto closure.")
        closure_metrics = AlertIngestionMetrics()
        orchestrator = OrderPoolAutoClosureOrchestrator(
            owner_user_id=owner_user_id,
            source_id="local-alertmanager",
            driver=driver,
            lifecycles=SQLAlchemyAlertIngestionRepository(session_factory),
            diagnostic_loader=PersistedDiagnosticOutcomeLoader(repositories),
            recovery=recovery,
            recovery_coordinator=_TaskScopedRecoveryCoordinator(
                runtime_provider=runtime_provider,
                owner_user_id=owner_user_id,
                run_id=run_id,
            ),
            evidence_preparer=_ScenarioEvidencePreparer(base_preparer, scenario),
            state_repository=SQLAlchemyAutoClosureStateRepository(session_factory),
            metrics=closure_metrics,
            progress=lambda stage: print(
                f"[auto-closure] {stage}",
                file=sys.stderr,
                flush=True,
            ),
        )

        async def execute() -> LiveAutoClosureResult:
            return await orchestrator.run(
                scenario_id,
                run_id=run_id,
                resume=bool(arguments.resume),
            )

        def score(closure: LiveAutoClosureResult) -> LiveEvaluationResult | None:
            if (
                closure.diagnostic_artifact is None
                or closure.observation is None
                or closure.recovery is None
                or closure.verification is None
            ):
                return None
            return score_live_run(
                closure.diagnostic_artifact,
                oracle,
                observation=closure.observation,
                recovery=closure.recovery,
                verification=closure.verification,
                cleanup_succeeded=bool(
                    closure.cleanup is not None and closure.cleanup.passed
                ),
                evidence_source=evidence_source,
                investigation_strategy="single",
            )

        def timing() -> dict[str, int | float]:
            snapshot = closure_metrics.snapshot().get("autoClosureStageLatencyMs")
            if not isinstance(snapshot, Mapping):
                return {}
            stage_metrics = cast(Mapping[str, object], snapshot)
            result: dict[str, int | float] = {}
            for stage in (
                "detection",
                "diagnosis",
                "recovery",
                "verification",
                "resolved",
                "total",
            ):
                value = stage_metrics.get(stage)
                if isinstance(value, Mapping):
                    total = cast(Mapping[str, object], value).get("sum")
                    if isinstance(total, (int, float)) and not isinstance(total, bool):
                        result[stage] = total
            return result

        payload, exit_code = await _run_auto_closure_once(
            scenario_id=scenario_id,
            run_id=run_id,
            evidence_source=evidence_source,
            execute=execute,
            score=score,
            stage_metrics=timing,
            recorder=recorder,
            running=existing,
        )
    except Exception:
        async def fail_setup() -> LiveAutoClosureResult:
            raise RuntimeError("auto_closure_setup_failed")

        payload, exit_code = await _run_auto_closure_once(
            scenario_id=scenario_id,
            run_id=run_id,
            evidence_source=evidence_source,
            execute=fail_setup,
            score=lambda closure: None,
            stage_metrics=dict,
            recorder=recorder,
            running=existing,
        )
    finally:
        await engine.dispose()
    write_safe_report(LIVE_REPORT_ROOT / f"{run_id}.json", payload)
    return payload, exit_code


async def run_chat_live_command(
    arguments: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    """Run the existing Live lifecycle with Chat confirmation as its diagnostic entry."""

    return await _run_live_command(arguments, enter_through_chat=True)


async def _run_live_once(
    *,
    scenario_id: str,
    run_id: str,
    evidence_source: EvidenceSource,
    execute: Callable[[], Awaitable[LiveEvaluationResult]],
    recorder: EvaluationRunRecorder,
    campaign_id: str | None = None,
    investigation_strategy: StrategyMode = "auto",
    conversation_metrics: Mapping[str, object] | None = None,
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
        failure_metrics = _cleanup_metrics(exc)
        failure_metrics.update(
            _investigation_process_metrics(exc.investigation_audit)
        )
        terminal = terminal_envelope(
            running=running,
            status=cast(EvaluationStatus, status),
            validity=validity,
            passed=False if status == "failed" else None,
            metrics=failure_metrics,
            result_payload=result_payload,
            diagnostic_task_id=exc.diagnostic_task_id,
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
    if conversation_metrics:
        metrics["conversationMetrics"] = dict(conversation_metrics)
        result_section = live_result.get("result")
        if isinstance(result_section, dict):
            result_section["conversationMetrics"] = dict(conversation_metrics)
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
                "toolCallCount": investigation_metrics.tool_call_count,
                "specialistRoleStatuses": dict(
                    investigation_metrics.role_statuses
                ),
                "specialistRoleDurationMs": dict(
                    investigation_metrics.role_duration_ms
                ),
                "specialistRoleModelCallCounts": dict(
                    investigation_metrics.role_model_call_counts
                ),
                "specialistRoleToolCallCounts": dict(
                    investigation_metrics.role_tool_call_counts
                ),
                "specialistRoleEvidenceCounts": dict(
                    investigation_metrics.role_evidence_counts
                ),
                "sourceGroupCount": investigation_metrics.source_group_count,
                "duplicateEvidenceCount": (
                    investigation_metrics.duplicate_evidence_count
                ),
                "conflictCount": investigation_metrics.conflict_count,
                "missingDomains": list(investigation_metrics.missing_domains),
                "aggregationChecksum": (
                    investigation_metrics.aggregation_checksum
                ),
                "terminalFailureCategory": (
                    investigation_metrics.terminal_failure_category
                ),
            }
        )
        metrics.update(_investigation_health_metrics(investigation_metrics))
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


async def _run_auto_closure_once(
    *,
    scenario_id: str,
    run_id: str,
    evidence_source: EvidenceSource,
    execute: Callable[[], Awaitable[LiveAutoClosureResult]],
    score: Callable[[LiveAutoClosureResult], LiveEvaluationResult | None],
    stage_metrics: Callable[[], Mapping[str, int | float]],
    recorder: EvaluationRunRecorder,
    running: EvaluationRunEnvelope | None = None,
) -> tuple[dict[str, object], int]:
    timestamp = datetime.now(timezone.utc)
    active = (
        running
        if running is not None
        else running_envelope(
            run_id=run_id,
            evaluation_kind="live",
            scenario_id=scenario_id,
            suite_version="v1",
            metadata={
                "workflowVersion": "order-pool-auto-closure-v1",
                "evidenceSource": evidence_source,
                "investigationStrategy": "single",
                "investigationPolicyVersion": "investigation-router-v1",
            },
            created_at=timestamp,
            started_at=timestamp,
        )
    )
    if active.status != "running":
        raise ValueError("Automatic closure resume requires a running evaluation.")
    start = await recorder.start(active)
    try:
        closure = await execute()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await recorder.fail(
            terminal_envelope(
                running=active,
                status="interrupted",
                validity=None,
                passed=None,
                metrics={"cleanupSucceeded": False},
                result_payload={"failures": ["interrupted"]},
                diagnostic_task_id=None,
                failure_category=None,
                completed_at=datetime.now(timezone.utc),
            )
        )
        raise
    except Exception:
        terminal = terminal_envelope(
            running=active,
            status="infra_invalid",
            validity="INFRA_INVALID",
            passed=None,
            metrics={"cleanupSucceeded": False},
            result_payload={"failures": ["auto_closure_runtime_error"]},
            diagnostic_task_id=None,
            failure_category="auto_closure_runtime_error",
            completed_at=datetime.now(timezone.utc),
        )
        finish = await recorder.fail(terminal)
        payload = safe_output(
            command="run",
            scenario_id=scenario_id,
            run_id=run_id,
            status="infra_invalid",
            result={
                "validity": "INFRA_INVALID",
                "failureCategory": "auto_closure_runtime_error",
                "evidenceSource": evidence_source,
            },
        )
        return payload, 2

    scored = score(closure)
    passed = bool(
        closure.validity == "VALID_PASS"
        and closure.closed_verified
        and scored is not None
        and scored.passed
    )
    status: EvaluationStatus = (
        "passed"
        if passed
        else "infra_invalid"
        if closure.validity == "INFRA_INVALID"
        else "failed"
    )
    validity = (
        "VALID_FAIL"
        if closure.validity == "VALID_PASS" and not passed
        else closure.validity
    )
    timing = stage_metrics()
    metrics: dict[str, object] = {
        "verificationPassed": bool(
            closure.verification is not None and closure.verification.passed
        ),
        "cleanupSucceeded": bool(
            closure.cleanup is not None and closure.cleanup.passed
        ),
        "closedVerified": closure.closed_verified,
        "mttdMs": _bounded_duration(timing.get("detection")),
        "diagnosisMs": _bounded_duration(timing.get("diagnosis")),
        "recoveryMs": _bounded_duration(timing.get("recovery")),
        "verificationMs": _bounded_duration(timing.get("verification")),
        "resolvedMs": _bounded_duration(timing.get("resolved")),
        "mttrMs": _bounded_duration(timing.get("total")),
    }
    failures = [] if passed else [closure.authorization_code]
    correlation = {
        "incidentId": closure.correlation.incident_id,
        "diagnosticTaskId": closure.correlation.diagnostic_task_id,
        "backgroundJobId": closure.correlation.background_job_id,
        "reportId": closure.correlation.report_id,
    }
    result_payload: dict[str, object] = {
        "failures": failures,
        "authorizationCode": closure.authorization_code,
        "closedVerified": closure.closed_verified,
        "correlation": correlation,
        "recoveryIntentId": closure.recovery_intent_id,
    }
    if scored is not None:
        metrics.update({"total": scored.total, "rawTotal": scored.raw_total})
        result_payload["hardGate"] = scored.hard_gate
        if not passed:
            result_payload["failures"] = list(
                dict.fromkeys([*failures, *scored.failures])
            )
        if scored.investigation_metrics is not None:
            metrics["durationMs"] = scored.investigation_metrics.duration_ms
            metrics["modelCallCount"] = scored.investigation_metrics.model_call_count
    terminal = terminal_envelope(
        running=active,
        status=status,
        validity=validity,
        passed=passed if status != "infra_invalid" else None,
        metrics=metrics,
        result_payload=result_payload,
        diagnostic_task_id=closure.correlation.diagnostic_task_id,
        failure_category=None if passed else closure.authorization_code,
        completed_at=datetime.now(timezone.utc),
    )
    finish = await recorder.finish(terminal) if passed else await recorder.fail(terminal)
    public_result: dict[str, object] = {
        "validity": validity,
        "passed": passed,
        "authorizationCode": closure.authorization_code,
        "closedVerified": closure.closed_verified,
        "correlation": correlation,
        "recoveryIntentId": closure.recovery_intent_id,
        "evidenceSource": evidence_source,
    }
    if scored is not None:
        public_result.update(
            total=scored.total,
            rawTotal=scored.raw_total,
            hardGate=scored.hard_gate,
            failures=result_payload["failures"],
        )
    payload = safe_output(
        command="run",
        scenario_id=scenario_id,
        run_id=run_id,
        status=status,
        result=public_result,
    )
    exit_code = 0 if passed else 2 if status == "infra_invalid" else 1
    if start.database_pending or finish.database_pending:
        exit_code = 2
    return payload, exit_code


def _bounded_duration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(round(value), 86_400_000))


def _cleanup_metrics(error: BaseException) -> dict[str, object]:
    cleanup_succeeded = getattr(error, "cleanup_succeeded", None)
    if isinstance(cleanup_succeeded, bool):
        return {"cleanupSucceeded": cleanup_succeeded}
    return {}


def _investigation_process_metrics(
    audit: InvestigationAudit | None,
) -> dict[str, object]:
    """Project answer-isolated investigation process metrics for failed runs."""
    if audit is None:
        return {}
    roles = audit.roles
    metrics: dict[str, object] = {
        "durationMs": sum(role.duration_ms for role in roles),
        "modelCallCount": sum(role.model_call_count for role in roles),
        "fallbackReason": audit.fallback_reason,
        "effectiveInvestigationStrategy": (
            audit.effective_strategy or audit.strategy
        ),
        "toolCallCount": sum(role.tool_call_count for role in roles),
        "specialistRoleStatuses": {
            role.role: role.status for role in roles
        },
        "specialistRoleDurationMs": {
            role.role: role.duration_ms for role in roles
        },
        "specialistRoleModelCallCounts": {
            role.role: role.model_call_count for role in roles
        },
        "specialistRoleToolCallCounts": {
            role.role: role.tool_call_count for role in roles
        },
        "specialistRoleEvidenceCounts": {
            role.role: len(role.evidence_ids) for role in roles
        },
        "sourceGroupCount": audit.source_group_count,
        "duplicateEvidenceCount": audit.duplicate_evidence_count,
        "conflictCount": audit.conflict_count,
        "missingDomains": list(audit.missing_domains),
        "aggregationChecksum": audit.aggregation_checksum,
        "terminalFailureCategory": audit.terminal_failure_category,
    }
    metrics.update(_audit_specialist_health_metrics(roles))
    return metrics


def _investigation_health_metrics(
    metrics: InvestigationBenchmarkMetrics,
) -> dict[str, object]:
    if not metrics.role_evidence_statuses or not metrics.role_analysis_statuses:
        return {}
    projected: dict[str, object] = {
        "specialistEvidenceStatuses": dict(metrics.role_evidence_statuses),
        "specialistAnalysisStatuses": dict(metrics.role_analysis_statuses),
        "specialistAnalysisErrorCodes": dict(metrics.role_analysis_error_codes),
        "specialistAnalysisAttemptCounts": dict(
            metrics.role_analysis_attempt_counts
        ),
        "specialistFollowUpQuestionCounts": dict(
            metrics.role_follow_up_question_counts
        ),
    }
    for key, value in (
        (
            "specialistEvidenceCompletionBasisPoints",
            metrics.specialist_evidence_completion_basis_points,
        ),
        (
            "specialistAnalysisCompletionBasisPoints",
            metrics.specialist_analysis_completion_basis_points,
        ),
        (
            "specialistDegradationBasisPoints",
            metrics.specialist_degradation_basis_points,
        ),
        (
            "specialistDeadlineHitBasisPoints",
            metrics.specialist_deadline_hit_basis_points,
        ),
        (
            "specialistStructuredRetryBasisPoints",
            metrics.specialist_structured_retry_basis_points,
        ),
    ):
        if value is not None:
            projected[key] = value
    return projected


def _audit_specialist_health_metrics(
    roles: tuple[SpecialistRoleAudit, ...],
) -> dict[str, object]:
    health_roles = tuple(
        role
        for role in roles
        if role.evidence_status is not None
        and role.analysis_status is not None
        and role.analysis_attempt_count is not None
        and role.follow_up_question_count is not None
        and role.soft_deadline_exceeded is not None
        and role.hard_deadline_exceeded is not None
    )
    if not health_roles:
        return {}
    denominator = len(health_roles)

    def basis_points(predicate: Callable[[SpecialistRoleAudit], bool]) -> int:
        return round(
            sum(1 for role in health_roles if predicate(role))
            * 10_000
            / denominator
        )

    return {
        "specialistEvidenceStatuses": {
            role.role: role.evidence_status for role in health_roles
        },
        "specialistAnalysisStatuses": {
            role.role: role.analysis_status for role in health_roles
        },
        "specialistAnalysisErrorCodes": {
            role.role: role.analysis_error_code
            for role in health_roles
            if role.analysis_error_code is not None
        },
        "specialistAnalysisAttemptCounts": {
            role.role: role.analysis_attempt_count for role in health_roles
        },
        "specialistFollowUpQuestionCounts": {
            role.role: role.follow_up_question_count for role in health_roles
        },
        "specialistEvidenceCompletionBasisPoints": basis_points(
            lambda role: role.evidence_status == "complete"
        ),
        "specialistAnalysisCompletionBasisPoints": basis_points(
            lambda role: role.analysis_status == "complete"
        ),
        "specialistDegradationBasisPoints": basis_points(
            lambda role: role.analysis_status != "complete"
        ),
        "specialistDeadlineHitBasisPoints": basis_points(
            lambda role: role.soft_deadline_exceeded is True
            or role.hard_deadline_exceeded is True
        ),
        "specialistStructuredRetryBasisPoints": basis_points(
            lambda role: cast(int, role.analysis_attempt_count) > 1
        ),
    }


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


if __name__ == "__main__":
    raise SystemExit(main())
