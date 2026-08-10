from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.evaluation import ArtifactEvidence, ArtifactToolCall, RunArtifact
from super_ai.evaluation.domain import PublicScenario
from super_ai.evaluation.runner import AgentVersion, SnapshotBenchmarkRunner
from super_ai.evaluation.scoring import EvaluationResult
from super_ai.mcp.cached_client import RuntimeMcpClient

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"


class RecordingScriptedDiagnosticAdapter:
    def __init__(self, artifact: RunArtifact) -> None:
        self._artifact = artifact
        self.received_context: dict[str, object] = {}
        self.received_tools: set[str] = set()

    async def run(
        self,
        *,
        run_id: str,
        scenario: PublicScenario,
        mcp_client: RuntimeMcpClient,
    ) -> RunArtifact:
        del run_id
        self.received_context = asdict(scenario)  # type: ignore[arg-type]
        self.received_tools = {tool.name for tool in await mcp_client.discover_tools()}
        return self._artifact


class RecordingPersistence:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.completed: list[tuple[str, str | None]] = []
        self.saved: list[tuple[str, EvaluationResult]] = []

    async def create_run(self, **values: object) -> None:
        self.created.append(values)

    async def complete_run(
        self,
        *,
        run_id: str,
        diagnostic_task_id: str | None,
    ) -> None:
        self.completed.append((run_id, diagnostic_task_id))

    async def save_result(
        self,
        *,
        result_id: str,
        run_id: str,
        result: EvaluationResult,
    ) -> None:
        assert result_id == f"result-{run_id}"
        self.saved.append((run_id, result))


def process_down_artifact() -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-003",
        mode="snapshot",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            component="checkout-service",
            mechanism="process_unavailable",
            trigger="benchmark_container_stopped",
            causal_chain=(
                "checkout process stopped",
                "port 8080 has no listener",
                "nginx upstream connection refused",
                "gateway returns 502",
            ),
            evidence_ids=("ev-container", "ev-nginx"),
            confidence=0.95,
        ),
        evidence=(
            ArtifactEvidence("ev-container", "container-status-exited", True),
            ArtifactEvidence("ev-nginx", "nginx-upstream-connection-refused", True),
        ),
        hypothesis_states=(
            HypothesisState(
                "upstream_process_down", "supported", 0.95, ("ev-container", "ev-nginx")
            ),
            HypothesisState("upstream_port_mismatch", "refuted", 0.1, ("ev-container",)),
        ),
        observation_decisions=(
            ObservationDecision(
                "Inspect process", ("upstream_process_down",), ("upstream_port_mismatch",), "down"
            ),
            ObservationDecision(
                "Inspect route", ("upstream_process_down",), (), "connection refused"
            ),
        ),
        tool_calls=(
            ArtifactToolCall("InspectContainer", "completed", "L0"),
            ArtifactToolCall("InspectNginx", "completed", "L0"),
        ),
        plan_step_count=2,
        duration_ms=120,
        safety_events=(),
        diagnostic_task_id="diagnostic-benchmark",
    )


@pytest.mark.asyncio
async def test_runner_keeps_oracle_outside_agent_and_writes_scorecard() -> None:
    adapter = RecordingScriptedDiagnosticAdapter(process_down_artifact())
    persistence = RecordingPersistence()
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=adapter,
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    result = await runner.run(
        "APY-003",
        agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
    )

    assert result.passed is True
    assert "ground_truth" not in json.dumps(adapter.received_context)
    assert "process_unavailable" not in json.dumps(adapter.received_context)
    assert adapter.received_tools == {"InspectContainer", "InspectNginx"}
    assert len(persistence.created) == 1
    assert persistence.completed == [
        (persistence.created[0]["run_id"], "diagnostic-benchmark")
    ]
    assert persistence.saved[0][1] == result


@pytest.mark.asyncio
async def test_runner_rejects_artifact_for_a_different_scenario() -> None:
    adapter = RecordingScriptedDiagnosticAdapter(
        replace(process_down_artifact(), scenario_id="APY-006")
    )
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=adapter,
        persistence=RecordingPersistence(),
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(ValueError, match="different scenario"):
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
        )
