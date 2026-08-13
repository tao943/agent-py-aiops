"""Safe command boundary for manually triggered Docker Live evaluations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from typing import cast

from super_ai.evaluation.live.scenarios import validate_run_id

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
    payload = safe_output(
        command=command,
        scenario_id=scenario_id,
        run_id=identity.run_id,
        status="not_configured",
    )
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 2
