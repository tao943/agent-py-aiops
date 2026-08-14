from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.aiops.diagnostics import (
    build_generic_live_plan,
    plan_matches_tool_contracts,
)
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.diagnostics import (
    LivePostgresEvidenceMcpClient,
    append_live_outcome,
    build_live_diagnostic_input,
)
from super_ai.evaluation.live.domain import (
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)
from super_ai.evaluation.live.scenarios import load_live_scenario
from super_ai.mcp_client import McpClientError

LIVE_SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "live"


def _artifact() -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-LIVE-PG-LOCK-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            component="postgresql",
            mechanism="row_lock_blocking",
            trigger="concurrent_transaction",
            causal_chain=("request waits", "row lock blocks update"),
            evidence_ids=("ev-session", "ev-lock-graph"),
            confidence=0.95,
        ),
        evidence=(),
        hypothesis_states=(),
        observation_decisions=(),
        tool_calls=(),
        plan_step_count=2,
        duration_ms=10,
        safety_events=(),
    )


def _observation() -> LiveFaultObservation:
    return LiveFaultObservation(101, 102, True, True)


def test_generic_fallback_uses_all_live_evidence_tools() -> None:
    plan = build_generic_live_plan(
        available_tools=(
            "VerifyServiceHealth",
            "InspectPostgresSessions",
            "InspectPostgresLockGraph",
        ),
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )

    assert [step["tool"] for step in plan] == [
        "VerifyServiceHealth",
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
    ]
    assert plan[0]["testsHypotheses"] == ["postgres_connectivity_failure"]
    assert plan[1]["testsHypotheses"] == [
        "postgres_slow_query_without_lock",
        "postgres_lock_blocking",
    ]
    assert plan[2]["testsHypotheses"] == ["postgres_lock_blocking"]


@pytest.mark.asyncio
async def test_model_plan_arguments_are_checked_against_discovered_tool_contracts() -> None:
    definitions = await LivePostgresEvidenceMcpClient(_observation()).discover_tools()
    valid_plan = build_generic_live_plan(
        available_tools=tuple(item.name for item in definitions),
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )
    invalid_model_plan: list[dict[str, object]] = [
        {
            "id": "step_1",
            "tool": "InspectPostgresSessions",
            "purpose": "Inspect waiting sessions.",
            "arguments": {
                "filters": {
                    "state": ["active", "idle in transaction"],
                    "include_wait_events": True,
                }
            },
            "testsHypotheses": ["postgres_lock_blocking"],
        }
    ]

    assert plan_matches_tool_contracts(valid_plan, definitions) is True
    assert plan_matches_tool_contracts(invalid_model_plan, definitions) is False


def test_live_input_contains_candidate_wide_vocabulary_without_oracle() -> None:
    scenario = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")

    payload = build_live_diagnostic_input(scenario)
    serialized = json.dumps(payload)

    assert payload["benchmarkMode"] == "live"
    assert payload["benchmarkScenarioId"] == scenario.id
    vocabulary = cast(dict[str, Any], payload["decisionVocabulary"])
    assert vocabulary["componentAliases"] == {
        "postgres": "postgresql",
        "postgresql": "postgresql",
    }
    assert vocabulary["mechanismAliases"] == {
        "postgres_lock_blocking": "row_lock_blocking",
        "row_lock_blocking": "row_lock_blocking",
        "postgres_slow_query_without_lock": "slow_query_without_lock",
        "slow_query_without_lock": "slow_query_without_lock",
        "postgres_connectivity_failure": "connectivity_failure",
        "connectivity_failure": "connectivity_failure",
    }
    assert "ground_truth" not in serialized
    assert "synthetic_transaction_holds_order_row_lock" not in serialized
    assert "agent_py_live_eval" not in serialized
    assert "run_id" not in serialized


@pytest.mark.asyncio
async def test_live_collector_exposes_only_read_only_safe_evidence() -> None:
    client = LivePostgresEvidenceMcpClient(_observation())

    assert {item.name for item in await client.discover_tools()} == {
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "VerifyServiceHealth",
    }
    sessions = await client.call_tool("InspectPostgresSessions", {})
    lock_graph = await client.call_tool("InspectPostgresLockGraph", {})
    assert isinstance(sessions, dict)
    assert isinstance(lock_graph, dict)
    safe_sessions = cast(dict[str, Any], sessions)
    safe_lock_graph = cast(dict[str, Any], lock_graph)
    serialized = json.dumps((sessions, lock_graph))
    assert safe_sessions["waitEventType"] == "Lock"
    assert safe_sessions["benchmarkEvidenceId"] == "postgres-wait-event-lock"
    assert safe_lock_graph["blockerEdgeConfirmed"] is True
    assert safe_lock_graph["benchmarkEvidenceId"] == "postgres-blocking-pid-edge"
    assert "password" not in serialized.lower()
    assert "dsn" not in serialized.lower()
    assert "sql" not in serialized.lower()
    assert "application_name" not in serialized
    with pytest.raises(McpClientError, match="not available"):
        await client.call_tool("ReadGroundTruth", {})


@pytest.mark.asyncio
async def test_live_collector_accepts_declared_read_only_filters() -> None:
    client = LivePostgresEvidenceMcpClient(_observation())

    health = await client.call_tool(
        "VerifyServiceHealth",
        {"target": "postgres_cluster", "check_connection_pool": True},
    )
    sessions = await client.call_tool(
        "InspectPostgresSessions",
        {
            "state_filter": ["active", "idle in transaction"],
            "include_wait_events": True,
        },
    )
    graph = await client.call_tool(
        "InspectPostgresLockGraph",
        {"detect_deadlocks": True, "analyze_blocking_chains": True},
    )

    assert isinstance(health, dict)
    assert isinstance(sessions, dict)
    assert isinstance(graph, dict)


@pytest.mark.asyncio
async def test_live_collector_rejects_unknown_or_mutating_arguments() -> None:
    client = LivePostgresEvidenceMcpClient(_observation())

    with pytest.raises(McpClientError, match="arguments are invalid"):
        await client.call_tool("InspectPostgresSessions", {"raw_sql": "SELECT 1"})
    with pytest.raises(McpClientError, match="arguments are invalid"):
        await client.call_tool("VerifyServiceHealth", {"restart": True})


def test_runner_boundary_appends_authorized_and_verified_recovery_facts() -> None:
    recovery = LiveRecoveryRecord("terminate_postgres_backend", 101, True, True, "allowed")
    verification = LiveVerification(True, True, True, True, True, True)

    enriched = append_live_outcome(_artifact(), recovery=recovery, verification=verification)

    assert enriched.live_recovery is not None
    assert enriched.live_recovery.action == "terminate_postgres_backend"
    assert enriched.live_recovery.approved is True
    assert enriched.live_recovery.verified is True
    assert enriched.live_recovery.target_ref == "synthetic_blocker"
    assert "101" not in json.dumps(asdict(enriched.live_recovery))
