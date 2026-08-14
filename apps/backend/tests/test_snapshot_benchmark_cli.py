from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_snapshot_benchmark.py"
SPEC = importlib.util.spec_from_file_location("run_snapshot_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_snapshot_cli_has_explicit_rag_mode_defaulting_on() -> None:
    parser = MODULE.build_parser()

    assert parser.parse_args(["--scenario", "APY-013"]).rag_mode == "on"
    assert (
        parser.parse_args(
            ["--scenario", "APY-013", "--rag-mode", "off"]
        ).rag_mode
        == "off"
    )


def test_snapshot_cli_accepts_explicit_retrieval_scope() -> None:
    arguments = MODULE.build_parser().parse_args(
        [
            "--scenario",
            "APY-013",
            "--owner-user-id",
            "eval-owner",
            "--knowledge-base-id",
            "kb-eval-owner",
        ]
    )

    assert arguments.owner_user_id == "eval-owner"
    assert arguments.knowledge_base_id == "kb-eval-owner"
