from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.recording import EvaluationRunRecorder
from super_ai.evaluation.runner import AgentVersion, BenchmarkRunError

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


def test_snapshot_output_is_optional_export_not_primary_storage() -> None:
    arguments = MODULE.build_parser().parse_args(["--scenario", "APY-013"])
    assert arguments.output is None


def test_snapshot_cli_accepts_and_validates_campaign_id() -> None:
    parser = MODULE.build_parser()

    arguments = parser.parse_args(
        ["--scenario", "APY-013", "--campaign-id", "full-acceptance-20260818"]
    )
    assert arguments.campaign_id == "full-acceptance-20260818"
    for invalid in ("", "../campaign", "x" * 81):
        with pytest.raises(SystemExit):
            parser.parse_args(["--scenario", "APY-013", "--campaign-id", invalid])


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


class AvailableRepository:
    async def start_envelope(self, envelope: object) -> None:
        del envelope

    async def finalize_envelope(self, envelope: object, *, artifact_checksum: str) -> None:
        del envelope, artifact_checksum


class RaisingRunner:
    async def run(self, scenario_id: str, *, run_id: str) -> None:
        del scenario_id, run_id
        raise BenchmarkRunError("agent_failed", "adapter_error")


@pytest.mark.asyncio
async def test_snapshot_failure_still_finalizes_safe_archive(tmp_path: Path) -> None:
    archive = EvaluationArchive(
        tmp_path / "archive",
        repository_root=tmp_path / "repository",
    )
    recorder = EvaluationRunRecorder(
        archive=archive,
        repository=AvailableRepository(),
    )

    report, pending = await MODULE._run_snapshot_once(
        scenario_id="APY-013",
        suite_version="v1",
        rag_mode="off",
        run_id="eval-cli-failure",
        agent_version=AgentVersion(git_sha="abc", workflow_version="v1"),
        model_configuration={"provider": "offline", "model": "scripted"},
        runner=RaisingRunner(),
        recorder=recorder,
    )

    saved = archive.load("eval-cli-failure")
    assert pending is False
    assert report["status"] == "agent_failed"
    assert saved.status == "agent_failed"
    assert saved.failure_category == "adapter_error"


@pytest.mark.asyncio
async def test_snapshot_campaign_is_saved_only_as_metadata(tmp_path: Path) -> None:
    archive = EvaluationArchive(
        tmp_path / "archive",
        repository_root=tmp_path / "repository",
    )
    recorder = EvaluationRunRecorder(
        archive=archive,
        repository=AvailableRepository(),
    )

    await MODULE._run_snapshot_once(
        scenario_id="APY-013",
        suite_version="v1",
        rag_mode="off",
        run_id="eval-cli-campaign",
        agent_version=AgentVersion(git_sha="abc", workflow_version="v1"),
        model_configuration={"provider": "offline", "model": "scripted"},
        runner=RaisingRunner(),
        recorder=recorder,
        campaign_id="full-acceptance-20260818",
    )

    saved = archive.load("eval-cli-campaign")
    assert saved.metadata["acceptanceCampaignId"] == "full-acceptance-20260818"
    assert "acceptanceCampaignId" not in saved.result_payload
