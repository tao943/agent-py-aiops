from __future__ import annotations

import pytest

from super_ai.api.app import CreateAiopsDiagnosticRequest
from super_ai.evaluation.live.cli import build_parser


def _run_arguments() -> list[str]:
    return [
        "run",
        "--scenario",
        "APY-LIVE-PG-LOCK-001",
        "--run-id",
        "live-strategy-1",
        "--owner-user-id",
        "eval-owner",
        "--knowledge-base-id",
        "kb-30-cards",
    ]


def test_live_run_strategy_defaults_auto_and_accepts_internal_modes() -> None:
    parser = build_parser()

    assert parser.parse_args(_run_arguments()).strategy == "auto"
    assert parser.parse_args([*_run_arguments(), "--strategy", "single"]).strategy == (
        "single"
    )
    assert parser.parse_args([*_run_arguments(), "--strategy", "multi"]).strategy == (
        "multi"
    )
    with pytest.raises(SystemExit):
        parser.parse_args([*_run_arguments(), "--strategy", "unbounded"])


@pytest.mark.parametrize("command", ("verify", "cleanup", "report"))
def test_non_run_commands_do_not_accept_strategy(command: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                command,
                "--scenario",
                "APY-LIVE-PG-LOCK-001",
                "--run-id",
                "live-strategy-1",
                "--strategy",
                "multi",
            ]
        )


def test_public_diagnostic_request_cannot_expose_investigation_strategy() -> None:
    request = CreateAiopsDiagnosticRequest.model_validate(
        {"query": "investigate", "alert": {}, "strategy": "multi"}
    )

    assert "strategy" not in request.model_dump()
    assert not hasattr(request, "strategy")
