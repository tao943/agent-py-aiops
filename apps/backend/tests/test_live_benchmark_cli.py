from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.artifacts import InvestigationBenchmarkMetrics
from super_ai.evaluation.live import cli as live_cli
from super_ai.evaluation.live.cli import (
    LIVE_SCENARIO_ROOT,
    build_live_evidence_runtime,
    build_live_recovery_coordinator,
    build_live_scenario_registry,
    build_parser,
    classify_live_failure,
    read_safe_report,
    safe_output,
    write_safe_report,
)
from super_ai.evaluation.live.domain import LiveCheck, LiveCleanupResult
from super_ai.evaluation.live.domain import LiveFaultObservation
from super_ai.evaluation.live.failure_diagnostics import LiveFailureDiagnostics
from super_ai.evaluation.live.nginx_timeout import (
    NginxProposalRecoveryService,
    NginxTimeoutScenarioDriver,
)
from super_ai.evaluation.live.order_pool_leak import (
    OrderPoolLeakScenarioDriver,
    OrderPoolRecoveryService,
)
from super_ai.evaluation.live.postgres import (
    PostgresLiveRecoveryService,
    PostgresLockScenarioDriver,
)
from super_ai.evaluation.live.postgres_deadlock import (
    PostgresDeadlockRecoveryService,
    PostgresDeadlockScenarioDriver,
)
from super_ai.evaluation.live.redis_maxclients import (
    RedisMaxclientsRecoveryService,
    RedisMaxclientsScenarioDriver,
)
from super_ai.evaluation.live.runner import LiveBenchmarkError, LocalLiveEvidencePreparer
from super_ai.evaluation.live.scoring import LiveEvaluationResult
from super_ai.evaluation.recording import EvaluationRunRecorder
from super_ai.project_config import ProjectConfigurationError


def test_registry_resolves_order_pool_runtime() -> None:
    components = build_live_scenario_registry().resolve(
        "APY-LIVE-ORDER-POOL-LEAK-001"
    )
    assert isinstance(components.driver, OrderPoolLeakScenarioDriver)
    assert isinstance(components.recovery, OrderPoolRecoveryService)
    assert components.cls_record_provider is not None


class AvailableEvaluationRepository:
    async def start_envelope(self, envelope: object) -> None:
        del envelope

    async def finalize_envelope(self, envelope: object, *, artifact_checksum: str) -> None:
        del envelope, artifact_checksum


class CapturingEvaluationRepository(AvailableEvaluationRepository):
    def __init__(self) -> None:
        self.finalized_envelope: object | None = None

    async def finalize_envelope(self, envelope: object, *, artifact_checksum: str) -> None:
        del artifact_checksum
        self.finalized_envelope = envelope


class RecordingAiopsRuntimeProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.repository = object()

    def execution_repository(
        self, *, owner_user_id: str, task_id: str, graph_version: str
    ) -> object:
        self.calls.append(
            {
                "owner_user_id": owner_user_id,
                "task_id": task_id,
                "graph_version": graph_version,
            }
        )
        return self.repository


def test_live_recovery_coordinator_uses_diagnostic_task_scope() -> None:
    provider = RecordingAiopsRuntimeProvider()

    coordinator = build_live_recovery_coordinator(
        runtime_provider=provider,  # type: ignore[arg-type]
        owner_user_id="eval-user",
        diagnostic_task_id="diagnostic-task-1",
        run_id="live-run-1",
    )

    assert provider.calls == [
        {
            "owner_user_id": "eval-user",
            "task_id": "diagnostic-task-1",
            "graph_version": "live-eval-v1",
        }
    ]
    assert coordinator._repository is provider.repository  # pyright: ignore[reportPrivateUsage]
    assert coordinator._worker_id == "live-recovery:live-run-1"  # pyright: ignore[reportPrivateUsage]


def live_recorder(tmp_path: Path) -> tuple[EvaluationRunRecorder, EvaluationArchive]:
    archive = EvaluationArchive(
        tmp_path / "archive", repository_root=tmp_path / "repository"
    )
    return (
        EvaluationRunRecorder(
            archive=archive,
            repository=AvailableEvaluationRepository(),
        ),
        archive,
    )


@pytest.mark.asyncio
async def test_recovery_denied_is_saved_as_valid_failure(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute():
        error = LiveBenchmarkError("recovery_denied", stage="recover")
        error.cleanup_succeeded = True
        raise error

    payload, exit_code = await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-recovery-denied",
        evidence_source="local",
        execute=execute,
        recorder=recorder,
    )
    envelope = archive.load("live-recovery-denied")
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert envelope.status == "failed"
    assert envelope.validity == "VALID_FAIL"
    assert envelope.metrics["cleanupSucceeded"] is True


def _failure_diagnostics() -> LiveFailureDiagnostics:
    diagnostics = LiveFailureDiagnostics.from_observation(
        LiveFaultObservation(
            scenario_id="APY-LIVE-ORDER-POOL-LEAK-001",
            checks=(
                LiveCheck("pool_at_capacity", True),
                LiveCheck("business_probe_timed_out", False),
            ),
            safe_facts=(("poolCapacity", 3), ("businessProbeTimedOut", False)),
        )
    )
    assert diagnostics is not None
    return diagnostics


@pytest.mark.asyncio
async def test_fault_injection_failure_persists_safe_check_diagnostics(tmp_path: Path) -> None:
    archive = EvaluationArchive(
        tmp_path / "archive", repository_root=tmp_path / "repository"
    )
    repository = CapturingEvaluationRepository()
    recorder = EvaluationRunRecorder(archive=archive, repository=repository)

    async def execute():
        error = LiveBenchmarkError(
            "fault_injection_failed",
            stage="inject",
            diagnostics=_failure_diagnostics(),
        )
        error.cleanup_succeeded = True
        raise error

    payload, exit_code = await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-ORDER-POOL-LEAK-001",
        run_id="live-fault-diagnostics",
        evidence_source="local",
        execute=execute,
        recorder=recorder,
    )
    envelope = archive.load("live-fault-diagnostics")

    assert exit_code == 1
    assert envelope.result_payload == {
        "failures": ["fault_injection_failed"],
        "failureStage": "inject",
        "checkResults": [
            {"name": "pool_at_capacity", "passed": True, "source": "driver"},
            {
                "name": "business_probe_timed_out",
                "passed": False,
                "source": "driver",
            },
        ],
        "failedChecks": ["business_probe_timed_out"],
        "safeFacts": {"poolCapacity": 3, "businessProbeTimedOut": False},
    }
    assert payload["result"] == {
        "evidenceSource": "local",
        "validity": "VALID_FAIL",
        "failureCategory": "fault_injection_failed",
        "failureStage": "inject",
        "failedChecks": ["business_probe_timed_out"],
    }
    assert repository.finalized_envelope == envelope


@pytest.mark.asyncio
async def test_cls_timeout_is_saved_as_infra_invalid(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute():
        error = LiveBenchmarkError("cls_index_timeout", stage="evidence")
        error.cleanup_succeeded = True
        raise error

    payload, exit_code = await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-cls-timeout",
        evidence_source="cls",
        execute=execute,
        recorder=recorder,
    )
    envelope = archive.load("live-cls-timeout")
    assert exit_code == 2
    assert payload["status"] == "infra_invalid"
    assert envelope.metadata["evidenceSource"] == "cls"
    assert envelope.metrics["cleanupSucceeded"] is True


@pytest.mark.asyncio
async def test_live_cancellation_is_saved_as_interrupted(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute():
        error = asyncio.CancelledError()
        error.cleanup_succeeded = True  # type: ignore[attr-defined]
        raise error

    with pytest.raises(asyncio.CancelledError):
        await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
            scenario_id="APY-LIVE-PG-LOCK-001",
            run_id="live-cancelled",
            evidence_source="local",
            execute=execute,
            recorder=recorder,
        )
    envelope = archive.load("live-cancelled")
    assert envelope.status == "interrupted"
    assert envelope.metrics["cleanupSucceeded"] is True


@pytest.mark.asyncio
async def test_live_failure_saves_failed_cleanup_result(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute():
        error = LiveBenchmarkError("diagnostic_failed", stage="diagnose")
        error.cleanup_succeeded = False
        raise error

    await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-cleanup-failed",
        evidence_source="local",
        execute=execute,
        recorder=recorder,
    )

    envelope = archive.load("live-cleanup-failed")
    assert envelope.failure_category == "diagnostic_failed"
    assert envelope.metrics["cleanupSucceeded"] is False


@pytest.mark.asyncio
async def test_live_runtime_error_saves_known_cleanup_result(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute():
        error = RuntimeError("sensitive runtime detail")
        error.cleanup_succeeded = False  # type: ignore[attr-defined]
        raise error

    payload, exit_code = await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-runtime-cleanup-failed",
        evidence_source="local",
        execute=execute,
        recorder=recorder,
    )

    envelope = archive.load("live-runtime-cleanup-failed")
    assert exit_code == 2
    assert payload["status"] == "infra_invalid"
    assert envelope.failure_category == "live_runtime_error"
    assert envelope.metrics["cleanupSucceeded"] is False


def test_cli_resolves_repository_live_scenario_root() -> None:
    assert (LIVE_SCENARIO_ROOT / "APY-LIVE-PG-LOCK-001" / "scenario.yaml").is_file()


def test_cli_builds_existing_postgres_runtime_through_registry() -> None:
    components = build_live_scenario_registry().resolve("APY-LIVE-PG-LOCK-001")

    assert components.driver_name == "postgres_lock_wait"
    assert isinstance(components.driver, PostgresLockScenarioDriver)
    assert isinstance(components.recovery, PostgresLiveRecoveryService)


def test_cli_builds_postgres_deadlock_runtime_through_registry() -> None:
    components = build_live_scenario_registry().resolve("APY-LIVE-PG-DEADLOCK-001")

    assert components.driver_name == "postgres_deadlock"
    assert isinstance(components.driver, PostgresDeadlockScenarioDriver)
    assert isinstance(components.recovery, PostgresDeadlockRecoveryService)


def test_cli_builds_redis_maxclients_runtime_through_registry() -> None:
    components = build_live_scenario_registry().resolve(
        "APY-LIVE-REDIS-MAXCLIENTS-001"
    )

    assert components.driver_name == "redis_maxclients"
    assert isinstance(components.driver, RedisMaxclientsScenarioDriver)
    assert isinstance(components.recovery, RedisMaxclientsRecoveryService)


def test_cli_builds_nginx_timeout_runtime_through_registry() -> None:
    components = build_live_scenario_registry().resolve(
        "APY-LIVE-NGINX-TIMEOUT-001"
    )

    assert components.driver_name == "nginx_timeout"
    assert isinstance(components.driver, NginxTimeoutScenarioDriver)
    assert isinstance(components.recovery, NginxProposalRecoveryService)


@pytest.mark.parametrize("command", ("run", "verify", "cleanup", "report"))
def test_cli_requires_explicit_scenario_and_run_id(command: str) -> None:
    parser = build_parser()
    values = [command, "--scenario", "APY-LIVE-PG-LOCK-001", "--run-id", "live-run-1"]
    if command == "run":
        values.extend(
            ["--owner-user-id", "eval-user", "--knowledge-base-id", "kb-30-cards"]
        )
    args = parser.parse_args(values)

    assert args.command == command
    assert args.scenario == "APY-LIVE-PG-LOCK-001"
    assert args.run_id == "live-run-1"


def test_live_run_cli_accepts_and_validates_campaign_id() -> None:
    parser = build_parser()
    base = [
        "run",
        "--scenario",
        "APY-LIVE-PG-LOCK-001",
        "--run-id",
        "live-run-1",
        "--owner-user-id",
        "eval-user",
        "--knowledge-base-id",
        "kb-30-cards",
        "--campaign-id",
    ]

    arguments = parser.parse_args([*base, "full-acceptance-20260818"])
    assert arguments.campaign_id == "full-acceptance-20260818"
    for invalid in ("", "../campaign", "x" * 81):
        with pytest.raises(SystemExit):
            parser.parse_args([*base, invalid])


@pytest.mark.asyncio
async def test_live_campaign_is_saved_only_as_metadata(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute():
        error = LiveBenchmarkError("diagnostic_failed", stage="diagnose")
        error.cleanup_succeeded = True
        raise error

    await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-campaign",
        evidence_source="local",
        execute=execute,
        recorder=recorder,
        campaign_id="full-acceptance-20260818",
    )

    envelope = archive.load("live-campaign")
    assert envelope.metadata["acceptanceCampaignId"] == "full-acceptance-20260818"
    assert "acceptanceCampaignId" not in envelope.result_payload


@pytest.mark.asyncio
async def test_successful_live_run_persists_diagnostic_task_id(tmp_path: Path) -> None:
    recorder, archive = live_recorder(tmp_path)

    async def execute() -> LiveEvaluationResult:
        return LiveEvaluationResult(
            fault_confirmation=10,
            required_evidence=20,
            differential_diagnosis=15,
            root_cause=20,
            citation_audit=10,
            recovery_policy=10,
            recovery_verification=15,
            raw_total=100,
            total=100,
            passed=True,
            failures=(),
            hard_gate=None,
            reasons=(),
            diagnostic_task_id="diagnostic-live-1",
            investigation_metrics=InvestigationBenchmarkMetrics(
                strategy="multi",
                effective_strategy="multi_agent",
                policy_version="investigation-router-v1",
                root_cause_top1_correct=True,
                evidence_recall_basis_points=10000,
                duration_ms=1200,
                model_call_count=3,
                duplicate_evidence_basis_points=0,
                fallback_reason=None,
                security_hard_gate_passed=True,
                total_score=100,
            ),
        )

    await live_cli._run_live_once(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-success-task-link",
        evidence_source="local",
        execute=execute,
        recorder=recorder,
        investigation_strategy="multi",
    )

    envelope = archive.load("live-success-task-link")
    assert envelope.diagnostic_task_id == "diagnostic-live-1"
    assert envelope.metadata["investigationStrategy"] == "multi"
    assert envelope.metrics["effectiveInvestigationStrategy"] == "multi_agent"
    assert envelope.metrics["rootCauseTop1Correct"] is True


def test_cli_rejects_missing_identity() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])


def test_run_requires_explicit_rag_owner_and_knowledge_base() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--scenario",
                "APY-LIVE-PG-LOCK-001",
                "--run-id",
                "live-run-1",
            ]
        )

    args = parser.parse_args(
        [
            "run",
            "--scenario",
            "APY-LIVE-PG-LOCK-001",
            "--run-id",
            "live-run-1",
            "--owner-user-id",
            "eval-user",
            "--knowledge-base-id",
            "kb-30-cards",
        ]
    )
    assert args.owner_user_id == "eval-user"
    assert args.knowledge_base_id == "kb-30-cards"


def test_run_evidence_source_defaults_local_and_accepts_cls() -> None:
    parser = build_parser()
    base = [
        "run",
        "--scenario",
        "APY-LIVE-PG-LOCK-001",
        "--run-id",
        "run-1",
        "--owner-user-id",
        "eval-user",
        "--knowledge-base-id",
        "kb-30-cards",
    ]

    assert parser.parse_args(base).evidence_source == "local"
    assert parser.parse_args(base + ["--evidence-source", "cls"]).evidence_source == "cls"
    with pytest.raises(SystemExit):
        parser.parse_args(base + ["--evidence-source", "fake"])


def test_local_runtime_does_not_require_project_config(tmp_path: Path) -> None:
    preparer, cls_client = build_live_evidence_runtime(
        evidence_source="local",
        config_path=tmp_path / "missing.json",
    )

    assert isinstance(preparer, LocalLiveEvidencePreparer)
    assert cls_client is None


def test_cls_runtime_fails_closed_when_project_config_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigurationError):
        build_live_evidence_runtime(
            evidence_source="cls",
            config_path=tmp_path / "missing.json",
        )


def test_cls_runtime_uses_safe_poll_defaults_for_older_config(tmp_path: Path) -> None:
    config_path = tmp_path / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "clsLogUpload": {
                    "region": "ap-guangzhou",
                    "endpoint": "https://ap-guangzhou.cls.tencentcs.com",
                    "topicId": "topic-live",
                },
                "clsMcpServer": {
                    "secretId": "test-secret-id",
                    "secretKey": "test-secret-key",
                },
                "mcp": {
                    "clsSseUrl": "http://127.0.0.1:3000/sse",
                    "timeoutSeconds": 15,
                    "retries": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    with (
        patch("super_ai.evaluation.live.cli.McpClsSearcher") as searcher_factory,
        patch("super_ai.evaluation.live.cli.LiveClsEvidencePreparer") as preparer_factory,
    ):
        preparer, cls_client = build_live_evidence_runtime(
            evidence_source="cls",
            config_path=config_path,
        )

    assert preparer is preparer_factory.return_value
    assert cls_client is not None
    assert searcher_factory.call_args.kwargs["limit"] == 20
    assert preparer_factory.call_args.kwargs["poll_interval_seconds"] == 2.0
    assert preparer_factory.call_args.kwargs["timeout_seconds"] == 90.0


def test_cls_runtime_rejects_malformed_present_live_evidence_config(tmp_path: Path) -> None:
    config_path = tmp_path / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "clsLogUpload": {},
                "clsMcpServer": {},
                "mcp": {},
                "liveClsEvidence": "invalid",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectConfigurationError):
        build_live_evidence_runtime(evidence_source="cls", config_path=config_path)


def test_live_failure_classification_separates_infrastructure_from_agent() -> None:
    assert classify_live_failure("cls_index_timeout", evidence_source="cls") == (
        "infra_invalid",
        "INFRA_INVALID",
        2,
    )


def test_live_failure_payload_keeps_only_bounded_error_metadata() -> None:
    payload, exit_code = live_cli._live_failure_payload(  # pyright: ignore[reportPrivateUsage]
        scenario_id="APY-LIVE-NGINX-TIMEOUT-001",
        run_id="live-run-1",
        evidence_source="cls",
        error=LiveBenchmarkError(
            "recovery_denied",
            stage="recover",
            authorization_code="proposal_denied",
        ),
    )

    assert exit_code == 1
    assert payload == {
        "command": "run",
        "scenarioId": "APY-LIVE-NGINX-TIMEOUT-001",
        "runId": "live-run-1",
        "status": "failed",
        "result": {
            "evidenceSource": "cls",
            "validity": "VALID_FAIL",
            "failureCategory": "recovery_denied",
            "failureStage": "recover",
            "authorizationCode": "proposal_denied",
        },
    }
    assert classify_live_failure("diagnostic_failed", evidence_source="local") == (
        "failed",
        "VALID_FAIL",
        1,
    )


@pytest.mark.parametrize(
    "invalid_failed_checks",
    [
        {"message": "sensitive"},
        [{"message": "sensitive"}],
        ["duplicate", "duplicate"],
        ["ground_truth"],
        ["x" * 81],
        [f"check_{index}" for index in range(65)],
    ],
)
def test_safe_output_drops_malformed_or_forbidden_failed_checks(
    invalid_failed_checks: object,
) -> None:
    payload = safe_output(
        command="run",
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-run-1",
        status="failed",
        result={"failedChecks": invalid_failed_checks},
    )

    assert "result" in payload
    assert "failedChecks" not in payload["result"]


def test_safe_report_revalidates_failed_check_names_after_tampering(tmp_path: Path) -> None:
    report = tmp_path / "live-run-1.json"
    report.write_text(
        json.dumps(
            {
                "command": "run",
                "scenarioId": "APY-LIVE-PG-LOCK-001",
                "runId": "live-run-1",
                "status": "failed",
                "result": {"failedChecks": [{"message": "sensitive"}]},
            }
        ),
        encoding="utf-8",
    )

    restored = read_safe_report(report)

    assert "failedChecks" not in restored["result"]


@pytest.mark.asyncio
async def test_cleanup_command_resolves_the_scenario_driver_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaned_run_ids: list[str] = []

    class RecordingDriver:
        async def cleanup(self, identity: object) -> LiveCleanupResult:
            cleaned_run_ids.append(identity.run_id)  # type: ignore[attr-defined]
            return LiveCleanupResult((LiveCheck("scoped_cleanup", True),))

    driver = RecordingDriver()

    class RecordingRegistry:
        def resolve(self, scenario_id: str) -> object:
            assert scenario_id == "APY-LIVE-REDIS-MAXCLIENTS-001"
            return SimpleNamespace(driver=driver)

    monkeypatch.setattr(
        live_cli,
        "build_live_scenario_registry",
        RecordingRegistry,
    )

    payload, exit_code = await live_cli._run_infrastructure_command(  # pyright: ignore[reportPrivateUsage]
        "cleanup",
        "APY-LIVE-REDIS-MAXCLIENTS-001",
        "live-run-1",
    )

    assert cleaned_run_ids == ["live-run-1"]
    assert exit_code == 0
    assert payload["result"] == {
        "verificationPassed": True,
        "cleanupSucceeded": True,
    }


@pytest.mark.asyncio
async def test_verify_command_reads_the_matching_completed_run_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_id = "APY-LIVE-NGINX-TIMEOUT-001"
    run_id = "live-run-2"
    resolved_scenarios: list[str] = []

    def resolve(value: str) -> object:
        resolved_scenarios.append(value)
        return SimpleNamespace(driver=object())

    monkeypatch.setattr(live_cli, "LIVE_REPORT_ROOT", tmp_path)
    monkeypatch.setattr(
        live_cli,
        "build_live_scenario_registry",
        lambda: SimpleNamespace(resolve=resolve),
    )
    write_safe_report(
        tmp_path / f"{run_id}.json",
        safe_output(
            command="run",
            scenario_id=scenario_id,
            run_id=run_id,
            status="failed",
            result={
                "passed": False,
                "verificationPassed": True,
                "cleanupSucceeded": True,
                "validity": "VALID_FAIL",
            },
        ),
    )

    payload, exit_code = await live_cli._run_infrastructure_command(  # pyright: ignore[reportPrivateUsage]
        "verify",
        scenario_id,
        run_id,
    )

    assert exit_code == 0
    assert resolved_scenarios == [scenario_id]
    assert payload["scenarioId"] == scenario_id
    assert payload["runId"] == run_id
    assert payload["result"] == {
        "verificationPassed": True,
        "cleanupSucceeded": None,
    }


def test_safe_json_output_drops_sensitive_and_oracle_fields() -> None:
    payload = safe_output(
        command="run",
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-run-1",
        status="passed",
        result={
            "total": 100,
            "passed": True,
            "evidenceSource": "cls",
            "validity": "VALID_PASS",
            "failureStage": "recover",
            "authorizationCode": "proposal_denied",
            "password": "secret",
            "dsn": "postgresql://secret",
            "rawLogs": ["secret"],
            "oracle": {"primary_cause": "secret"},
        },
    )

    serialized = json.dumps(payload)
    assert payload == {
        "command": "run",
        "scenarioId": "APY-LIVE-PG-LOCK-001",
        "runId": "live-run-1",
        "status": "passed",
        "result": {
            "total": 100,
            "passed": True,
            "evidenceSource": "cls",
            "validity": "VALID_PASS",
            "failureStage": "recover",
            "authorizationCode": "proposal_denied",
        },
    }
    assert "secret" not in serialized


def test_report_round_trip_reapplies_output_allowlist(tmp_path: Path) -> None:
    payload = safe_output(
        command="run",
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-run-1",
        status="passed",
        result={
            "total": 100,
            "passed": True,
            "failedChecks": ["business_probe_timed_out"],
        },
    )
    path = tmp_path / "report.json"

    write_safe_report(path, payload)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    parsed["password"] = "secret"
    parsed["result"]["oracle"] = "secret"
    path.write_text(json.dumps(parsed), encoding="utf-8")

    report = read_safe_report(path)
    serialized = json.dumps(report)
    assert report["result"] == {
        "total": 100,
        "passed": True,
        "failedChecks": ["business_probe_timed_out"],
    }
    assert "secret" not in serialized
