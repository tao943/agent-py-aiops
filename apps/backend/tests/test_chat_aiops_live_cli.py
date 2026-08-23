from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_chat_aiops_live_eval.py"
SPEC = importlib.util.spec_from_file_location("run_chat_aiops_live_eval", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def parse(*extra: str):  # type: ignore[no-untyped-def]
    return CLI.build_parser().parse_args(
        [
            "--scenario",
            "APY-LIVE-PG-LOCK-001",
            "--owner-user-id",
            "owner-live",
            "--knowledge-base-id",
            "kb-live",
            *extra,
        ]
    )


@pytest.mark.asyncio
async def test_chat_live_cli_requires_both_explicit_external_call_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(_arguments: object) -> tuple[dict[str, object], int]:
        raise AssertionError("Live runtime must not start")

    monkeypatch.setattr(CLI.live_cli, "run_chat_live_command", forbidden)

    assert await CLI.run_command(parse()) == 2
    assert await CLI.run_command(parse("--confirm-real-model")) == 2
    assert await CLI.run_command(parse("--confirm-live-cls")) == 2


@pytest.mark.asyncio
async def test_chat_live_cli_forwards_valid_request_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[argparse.Namespace] = []

    async def execute(arguments: argparse.Namespace) -> tuple[dict[str, object], int]:
        captured.append(arguments)
        return {
            "status": "passed",
            "result": {
                "total": 100,
                "conversationMetrics": {"confirmationAccuracy": 1.0},
            },
        }, 0

    monkeypatch.setattr(CLI.live_cli, "run_chat_live_command", execute)

    exit_code = await CLI.run_command(
        parse("--confirm-real-model", "--confirm-live-cls", "--run-id", "chat-live-1")
    )

    assert exit_code == 0
    assert len(captured) == 1
    forwarded = captured[0]
    assert forwarded.evidence_source == "cls"
    assert forwarded.run_id == "chat-live-1"


def test_chat_live_cli_rejects_scenario_path_traversal() -> None:
    arguments = parse("--confirm-real-model", "--confirm-live-cls")
    arguments.scenario = "../APY-LIVE-PG-LOCK-001"

    with pytest.raises(ValueError, match="scenario"):
        CLI.validate_scenario_id(arguments.scenario)


def test_chat_live_cli_maps_interrupt_to_130(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(_arguments: object) -> int:
        raise KeyboardInterrupt

    class Parser:
        def parse_args(self) -> object:
            return object()

    monkeypatch.setattr(CLI, "run_command", interrupted)
    monkeypatch.setattr(CLI, "build_parser", Parser)

    assert CLI.main() == 130
