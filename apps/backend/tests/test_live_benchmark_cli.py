from __future__ import annotations

import json
from pathlib import Path

import pytest

from super_ai.evaluation.live.cli import (
    LIVE_SCENARIO_ROOT,
    build_parser,
    read_safe_report,
    safe_output,
    write_safe_report,
)


def test_cli_resolves_repository_live_scenario_root() -> None:
    assert (LIVE_SCENARIO_ROOT / "APY-LIVE-PG-LOCK-001" / "scenario.yaml").is_file()


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


def test_safe_json_output_drops_sensitive_and_oracle_fields() -> None:
    payload = safe_output(
        command="run",
        scenario_id="APY-LIVE-PG-LOCK-001",
        run_id="live-run-1",
        status="passed",
        result={
            "total": 100,
            "passed": True,
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
        "result": {"total": 100, "passed": True},
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
