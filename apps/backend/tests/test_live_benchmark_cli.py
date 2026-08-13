from __future__ import annotations

import json

import pytest

from super_ai.evaluation.live.cli import build_parser, safe_output


@pytest.mark.parametrize("command", ("run", "verify", "cleanup", "report"))
def test_cli_requires_explicit_scenario_and_run_id(command: str) -> None:
    parser = build_parser()

    args = parser.parse_args(
        [command, "--scenario", "APY-LIVE-PG-LOCK-001", "--run-id", "live-run-1"]
    )

    assert args.command == command
    assert args.scenario == "APY-LIVE-PG-LOCK-001"
    assert args.run_id == "live-run-1"


def test_cli_rejects_missing_identity() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])


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
