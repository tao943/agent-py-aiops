from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import pytest

from super_ai.aiops import HypothesisState, ObservationDecision, RootCauseDecision
from super_ai.evaluation import (
    ArtifactEvidence,
    ArtifactToolCall,
    RunArtifact,
    SnapshotMcpClient,
)
from super_ai.evaluation.domain import PublicDecisionLabel, PublicHypothesis, PublicScenario
from super_ai.evaluation.runner import (
    AgentVersion,
    ApplicationDiagnosticAdapter,
    BenchmarkRunError,
    NullKnowledgeRetrievalTool,
    SnapshotBenchmarkRunner,
    _snapshot_decision_vocabulary,
    build_application_diagnostic_input,
)
from super_ai.evaluation.scenarios import load_public_scenario
from super_ai.evaluation.scoring import EvaluationResult
from super_ai.mcp.cached_client import RuntimeMcpClient
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalToolInput

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"


@pytest.mark.asyncio
async def test_rag_off_returns_no_hits_or_citations() -> None:
    result = await NullKnowledgeRetrievalTool().run(
        KnowledgeRetrievalToolInput(query="public symptom", top_k=3),
        owner_user_id="owner-a",
        accessible_knowledge_base_ids=("kb-owner-a",),
    )

    assert result.results == []
    assert result.citations == []


@pytest.mark.asyncio
async def test_application_adapter_forwards_snapshot_argument_contracts(
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingDiagnosticService:
        def __init__(self, **values: object) -> None:
            captured.update(values)

        async def stream(self, **values: object) -> AsyncIterator[dict[str, object]]:
            del values
            if False:
                yield {}

    monkeypatch.setattr(
        "super_ai.evaluation.runner.AiopsDiagnosticService",
        CapturingDiagnosticService,
    )
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        adapter = ApplicationDiagnosticAdapter(
            repositories=repositories,
            llm_provider=cast(Any, object()),
            retrieval_tool=NullKnowledgeRetrievalTool(),
        )

        await adapter.run(
            run_id="contract-forwarding",
            scenario=scenario,
            mcp_client=snapshot,
        )
    finally:
        await engine.dispose()

    contracts = captured["tool_argument_contracts"]
    assert isinstance(contracts, Mapping)
    assert set(cast(Mapping[str, object], contracts)) == set(
        snapshot.tool_argument_contracts
    )


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
    def __init__(
        self,
        *,
        finalize_error: BaseException | None = None,
        fail_error: BaseException | None = None,
    ) -> None:
        self.created: list[dict[str, object]] = []
        self.failed: list[tuple[str, str, str]] = []
        self.finalized: list[tuple[str, str | None, EvaluationResult]] = []
        self._finalize_error = finalize_error
        self._fail_error = fail_error

    async def create_run(self, **values: object) -> None:
        self.created.append(values)

    async def fail_run(
        self,
        *,
        run_id: str,
        status: str,
        failure_category: str,
    ) -> None:
        if self._fail_error is not None:
            raise self._fail_error
        self.failed.append((run_id, status, failure_category))

    async def finalize_run(
        self,
        *,
        run_id: str,
        result_id: str,
        result: EvaluationResult,
        diagnostic_task_id: str | None,
    ) -> None:
        assert result_id == f"result-{run_id}"
        if self._finalize_error is not None:
            raise self._finalize_error
        self.finalized.append((run_id, diagnostic_task_id, result))


class RaisingDiagnosticAdapter:
    async def run(
        self,
        *,
        run_id: str,
        scenario: PublicScenario,
        mcp_client: RuntimeMcpClient,
    ) -> RunArtifact:
        del run_id, scenario, mcp_client
        raise RuntimeError("adapter-secret-sentinel")


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


def test_application_diagnostic_input_contains_only_public_scenario_fields() -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")

    payload = build_application_diagnostic_input(scenario)
    serialized = json.dumps(payload)
    vocabulary = cast(dict[str, object], payload["decisionVocabulary"])
    labels = cast(dict[str, dict[str, str]], vocabulary["labelsByHypothesis"])
    mechanisms = cast(dict[str, str], vocabulary["mechanismAliases"])
    components = cast(dict[str, str], vocabulary["componentAliases"])

    assert payload["benchmarkScenarioId"] == "APY-013"
    assert payload["benchmarkMode"] == "snapshot"
    assert payload["query"] == "Order transactions are rolling back during concurrent updates."
    assert set(labels) == {item.id for item in scenario.hypotheses}
    assert labels["postgres_deadlock"] == {
        "component": "order-service",
        "mechanism": "opposite_order_transaction_deadlock",
    }
    assert mechanisms["postgres_deadlock"] == "opposite_order_transaction_deadlock"
    assert components == {"order-service": "order-service"}
    assert "ground_truth" not in serialized
    assert "evidence" not in json.dumps(vocabulary).lower()
    assert "oracle" not in json.dumps(vocabulary).lower()
    assert "concurrent_updates_acquired_order_and_inventory_rows_in_reverse_order" not in serialized


def test_snapshot_decision_vocabulary_rejects_conflicting_mechanism_aliases() -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    conflicting = replace(
        scenario,
        hypotheses=(
            PublicHypothesis(
                id="shared",
                description="First candidate.",
                decision_label=PublicDecisionLabel("service-a", "mechanism-a"),
            ),
            PublicHypothesis(
                id="mechanism-a",
                description="Second candidate.",
                decision_label=PublicDecisionLabel("service-b", "mechanism-b"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="conflicting mechanism alias"):
        _snapshot_decision_vocabulary(conflicting)


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
    assert "benchmark_container_stopped" not in json.dumps(adapter.received_context)
    assert adapter.received_tools == {"InspectContainer", "InspectNginx"}
    assert len(persistence.created) == 1
    assert persistence.failed == []
    assert persistence.finalized == [
        (persistence.created[0]["run_id"], "diagnostic-benchmark", result)
    ]


@pytest.mark.asyncio
async def test_runner_classifies_adapter_failure_without_leaking_exception_text() -> None:
    persistence = RecordingPersistence()
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RaisingDiagnosticAdapter(),
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
            run_id="run-adapter-failure",
        )

    assert captured.value.status == "agent_failed"
    assert captured.value.category == "adapter_error"
    assert "adapter-secret-sentinel" not in str(captured.value)
    assert persistence.failed == [
        ("run-adapter-failure", "agent_failed", "adapter_error")
    ]
    assert persistence.finalized == []


@pytest.mark.asyncio
async def test_runner_rejects_artifact_for_a_different_scenario() -> None:
    adapter = RecordingScriptedDiagnosticAdapter(
        replace(process_down_artifact(), scenario_id="APY-006")
    )
    persistence = RecordingPersistence()
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=adapter,
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
            run_id="run-wrong-scenario",
        )

    assert captured.value.status == "agent_failed"
    assert captured.value.category == "artifact_invalid"
    assert persistence.failed == [
        ("run-wrong-scenario", "agent_failed", "artifact_invalid")
    ]


@pytest.mark.asyncio
async def test_runner_rejects_non_snapshot_artifact_as_agent_failure() -> None:
    persistence = RecordingPersistence()
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(
            replace(process_down_artifact(), mode="live")
        ),
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
            run_id="run-wrong-mode",
        )

    assert captured.value.status == "agent_failed"
    assert captured.value.category == "artifact_invalid"
    assert persistence.failed == [
        ("run-wrong-mode", "agent_failed", "artifact_invalid")
    ]


@pytest.mark.asyncio
async def test_runner_classifies_evaluator_failure_as_infrastructure_failure() -> None:
    persistence = RecordingPersistence()

    def failing_evaluator(artifact: RunArtifact, scenario_dir: Path) -> EvaluationResult:
        del artifact, scenario_dir
        raise RuntimeError("evaluator-secret-sentinel")

    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(process_down_artifact()),
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
        evaluator=failing_evaluator,
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
            run_id="run-evaluator-failure",
        )

    assert captured.value.status == "infra_failed"
    assert captured.value.category == "evaluation_error"
    assert "evaluator-secret-sentinel" not in str(captured.value)
    assert persistence.failed == [
        ("run-evaluator-failure", "infra_failed", "evaluation_error")
    ]


@pytest.mark.asyncio
async def test_runner_classifies_finalize_failure_and_records_safe_terminal_state() -> None:
    persistence = RecordingPersistence(
        finalize_error=RuntimeError("persistence-secret-sentinel")
    )
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(process_down_artifact()),
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
            run_id="run-finalize-failure",
        )

    assert captured.value.status == "infra_failed"
    assert captured.value.category == "persistence_error"
    assert "persistence-secret-sentinel" not in str(captured.value)
    assert persistence.failed == [
        ("run-finalize-failure", "infra_failed", "persistence_error")
    ]


@pytest.mark.asyncio
async def test_runner_reports_persistence_error_when_failure_state_cannot_be_saved() -> None:
    persistence = RecordingPersistence(
        fail_error=RuntimeError("failure-persistence-secret-sentinel")
    )
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RaisingDiagnosticAdapter(),
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
            run_id="run-failure-persistence-failure",
        )

    assert captured.value.status == "infra_failed"
    assert captured.value.category == "persistence_error"
    assert "failure-persistence-secret-sentinel" not in str(captured.value)
    assert persistence.failed == []


@pytest.mark.parametrize(
    "scenario_id",
    ("../APY-003", "..\\APY-003", "/tmp/APY-003", "C:\\tmp\\APY-003"),
)
@pytest.mark.asyncio
async def test_runner_rejects_scenario_path_traversal_before_persistence(
    scenario_id: str,
) -> None:
    persistence = RecordingPersistence()
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(process_down_artifact()),
        persistence=persistence,
        suite_version="v1",
        model_configuration={"provider": "offline", "model": "scripted"},
    )

    with pytest.raises(ValueError, match="single directory name"):
        await runner.run(
            scenario_id,
            agent_version=AgentVersion(git_sha="abc123", workflow_version="v1"),
        )

    assert persistence.created == []
