"""Run Docker Live through a confirmation-gated Conversation Agent entry."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from uuid import uuid4

from super_ai.evaluation.live import cli as live_cli

_SCENARIO_ID = re.compile(r"^APY-LIVE-[A-Z0-9-]{1,64}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one Chat-to-AIOps Docker Live evaluation."
    )
    parser.set_defaults(auto_closure=False, resume=False)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--config")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--campaign-id")
    parser.add_argument("--strategy", choices=("auto", "single", "multi"), default="auto")
    parser.add_argument("--confirm-real-model", action="store_true")
    parser.add_argument("--confirm-live-cls", action="store_true")
    return parser


def validate_scenario_id(value: str) -> str:
    if not _SCENARIO_ID.fullmatch(value) or ".." in value:
        raise ValueError("Chat Live scenario ID is invalid.")
    live_cli.build_live_scenario_registry().resolve(value)
    return value


async def run_command(arguments: argparse.Namespace) -> int:
    if not arguments.confirm_real_model or not arguments.confirm_live_cls:
        print(json.dumps({"error": "real_model_and_live_cls_confirmation_required"}))
        return 2
    scenario_id = validate_scenario_id(arguments.scenario)
    arguments.scenario = scenario_id
    arguments.run_id = arguments.run_id or f"chat-live-{uuid4().hex}"
    arguments.command = "run"
    arguments.evidence_source = "cls"
    payload, exit_code = await live_cli.run_chat_live_command(arguments)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{serialized}\n", encoding="utf-8")
    return exit_code


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return asyncio.run(run_command(arguments))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"error": "chat_aiops_live_eval_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
