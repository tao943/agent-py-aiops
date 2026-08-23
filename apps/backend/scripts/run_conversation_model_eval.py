"""Run the explicit six-scenario Conversation Model Eval."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from super_ai.chat.model_evaluation import (
    FixtureConversationModelEvalBridge,
    run_conversation_model_eval,
)
from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.recording import EvaluationRunRecorder
from super_ai.llm import build_default_llm_provider
from super_ai.memory.database import create_memory_engine, create_memory_session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the manual real-model Conversation Agent evaluation."
    )
    parser.add_argument(
        "--confirm-real-model",
        action="store_true",
        help="Explicitly allow calls to the configured paid model.",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--git-sha", default="unknown")
    return parser


async def run_command(arguments: argparse.Namespace) -> int:
    if not arguments.confirm_real_model:
        print(json.dumps({"error": "real_model_confirmation_required"}))
        return 2

    config_path = str(arguments.config) if arguments.config is not None else None
    engine = create_memory_engine(config_path=config_path)
    try:
        provider = build_default_llm_provider(config_path=config_path)
        result = await run_conversation_model_eval(
            model=provider.create_chat_model(),
            bridge=FixtureConversationModelEvalBridge(),
            recorder=EvaluationRunRecorder(
                archive=EvaluationArchive.from_config(config_path=arguments.config),
                repository=EvaluationRepository(create_memory_session_factory(engine)),
            ),
            run_id=arguments.run_id,
            git_sha=arguments.git_sha,
        )
    finally:
        await engine.dispose()

    payload = result.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{serialized}\n", encoding="utf-8")
    if result.database_pending:
        return 2
    return 0 if result.passed else 1


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return asyncio.run(run_command(arguments))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"error": "conversation_model_eval_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
