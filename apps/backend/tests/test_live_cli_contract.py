from __future__ import annotations

import pytest

from super_ai.api.app import CreateAiopsDiagnosticRequest
from super_ai.evaluation.live.cli import _auto_closure_strategy, build_parser


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


def test_auto_closure_forces_single_and_rejects_multi() -> None:
    parser = build_parser()
    automatic = parser.parse_args([*_run_arguments(), "--auto-closure"])
    resumed = parser.parse_args([*_run_arguments(), "--auto-closure", "--resume"])

    assert automatic.auto_closure is True
    assert resumed.resume is True
    assert _auto_closure_strategy(automatic) == "single"
    with pytest.raises(ValueError, match="Multi"):
        _auto_closure_strategy(
            parser.parse_args(
                [*_run_arguments(), "--auto-closure", "--strategy", "multi"]
            )
        )


def test_resume_is_rejected_without_auto_closure() -> None:
    arguments = build_parser().parse_args([*_run_arguments(), "--resume"])
    with pytest.raises(ValueError, match="auto-closure"):
        _auto_closure_strategy(arguments)


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
