from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from super_ai.evaluation.live.cli import (
    LIVE_SCENARIO_ROOT,
    build_live_evidence_runtime,
    build_live_scenario_registry,
    build_parser,
    classify_live_failure,
    read_safe_report,
    safe_output,
    write_safe_report,
)
from super_ai.evaluation.live.postgres import (
    PostgresLiveRecoveryService,
    PostgresLockScenarioDriver,
)
from super_ai.evaluation.live.postgres_deadlock import (
    PostgresDeadlockRecoveryService,
    PostgresDeadlockScenarioDriver,
)
from super_ai.evaluation.live.runner import LocalLiveEvidencePreparer
from super_ai.project_config import ProjectConfigurationError


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
    assert classify_live_failure("diagnostic_failed", evidence_source="local") == (
        "failed",
        "VALID_FAIL",
        1,
    )


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
        },
    }
    assert "secret" not in serialized


def test_report_round_trip_reapplies_output_allowlist(tmp_path: Path) -> None:
    payload = safe_output(
        command="run",
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-run-1",
        status="passed",
        result={"total": 100, "passed": True},
    )
    path = tmp_path / "report.json"

    write_safe_report(path, payload)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    parsed["password"] = "secret"
    parsed["result"]["oracle"] = "secret"
    path.write_text(json.dumps(parsed), encoding="utf-8")

    report = read_safe_report(path)
    serialized = json.dumps(report)
    assert report["result"] == {"total": 100, "passed": True}
    assert "secret" not in serialized
