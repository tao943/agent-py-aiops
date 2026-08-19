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
    ApplicationDiagnosticAdapter,
    BenchmarkRunError,
    NullKnowledgeRetrievalTool,
    SnapshotBenchmarkRunner,
    _snapshot_decision_vocabulary,  # pyright: ignore[reportPrivateUsage]
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
    assert "workflowVersion" not in payload


def test_application_diagnostic_input_can_request_auditable_v4() -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")

    payload = build_application_diagnostic_input(
        scenario,
        workflow_version="evidence-driven-v4",
    )

    assert payload["workflowVersion"] == "evidence-driven-v4"


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
async def test_runner_keeps_oracle_outside_agent_and_returns_scorecard() -> None:
    adapter = RecordingScriptedDiagnosticAdapter(process_down_artifact())
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=adapter,
    )

    outcome = await runner.run("APY-003")
    result = outcome.result

    assert result.passed is True
    assert "ground_truth" not in json.dumps(adapter.received_context)
    assert "benchmark_container_stopped" not in json.dumps(adapter.received_context)
    assert adapter.received_tools == {"InspectContainer", "InspectNginx"}
    assert outcome.diagnostic_task_id == "diagnostic-benchmark"


@pytest.mark.asyncio
async def test_runner_classifies_adapter_failure_without_leaking_exception_text() -> None:
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RaisingDiagnosticAdapter(),
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            run_id="run-adapter-failure",
        )

    assert captured.value.status == "agent_failed"
    assert captured.value.category == "adapter_error"
    assert "adapter-secret-sentinel" not in str(captured.value)


@pytest.mark.asyncio
async def test_runner_rejects_artifact_for_a_different_scenario() -> None:
    adapter = RecordingScriptedDiagnosticAdapter(
        replace(process_down_artifact(), scenario_id="APY-006")
    )
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=adapter,
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            run_id="run-wrong-scenario",
        )

    assert captured.value.status == "agent_failed"
    assert captured.value.category == "artifact_invalid"


@pytest.mark.asyncio
async def test_runner_rejects_non_snapshot_artifact_as_agent_failure() -> None:
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(
            replace(process_down_artifact(), mode="live")
        ),
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            run_id="run-wrong-mode",
        )

    assert captured.value.status == "agent_failed"
    assert captured.value.category == "artifact_invalid"


@pytest.mark.asyncio
async def test_runner_classifies_evaluator_failure_as_infrastructure_failure() -> None:
    def failing_evaluator(artifact: RunArtifact, scenario_dir: Path) -> EvaluationResult:
        del artifact, scenario_dir
        raise RuntimeError("evaluator-secret-sentinel")

    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(process_down_artifact()),
        evaluator=failing_evaluator,
    )

    with pytest.raises(BenchmarkRunError) as captured:
        await runner.run(
            "APY-003",
            run_id="run-evaluator-failure",
        )

    assert captured.value.status == "infra_failed"
    assert captured.value.category == "evaluation_error"
    assert "evaluator-secret-sentinel" not in str(captured.value)


@pytest.mark.parametrize(
    "scenario_id",
    ("../APY-003", "..\\APY-003", "/tmp/APY-003", "C:\\tmp\\APY-003"),
)
@pytest.mark.asyncio
async def test_runner_rejects_scenario_path_traversal_before_adapter(
    scenario_id: str,
) -> None:
    runner = SnapshotBenchmarkRunner(
        scenario_root=SCENARIOS,
        adapter=RecordingScriptedDiagnosticAdapter(process_down_artifact()),
    )

    with pytest.raises(ValueError, match="single directory name"):
        await runner.run(scenario_id)
