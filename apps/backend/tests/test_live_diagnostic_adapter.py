from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.aiops.diagnostics import (
    AiopsDiagnosticService,
    bind_trusted_tool_arguments,
    build_generic_live_plan,
    build_grounded_fallback_decision,
    plan_matches_tool_contracts,
)
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.diagnostics import (
    LivePostgresEvidenceMcpClient,
    append_live_outcome,
    build_live_diagnostic_input,
    build_live_evidence_client,
)
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)
from super_ai.evaluation.live.postgres_deadlock import PostgresDeadlockEvidenceMcpClient
from super_ai.evaluation.live.scenarios import load_live_scenario
from super_ai.mcp_client import McpClientError, McpToolDefinition

LIVE_SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "live"


@pytest.mark.asyncio
async def test_live_diagnostic_client_factory_preserves_local_toolset() -> None:
    client = build_live_evidence_client(
        observation=LiveFaultObservation(
            "APY-LIVE-PG-LOCK-001",
            (
                LiveCheck("waiter_has_lock_event", True),
                LiveCheck("blocker_edge_confirmed", True),
            ),
        ),
        evidence_context=LiveEvidenceContext.local(
            incident_id="APY-LIVE-PG-LOCK-001-run-1"
        ),
        cls_client=None,
    )

    assert {item.name for item in await client.discover_tools()} == {
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "VerifyServiceHealth",
    }


@pytest.mark.asyncio
async def test_live_diagnostic_client_factory_uses_registered_component_tools() -> None:
    observation = LiveFaultObservation(
        "APY-LIVE-PG-DEADLOCK-001",
        (
            LiveCheck("postgres_40p01", True),
            LiveCheck("deadlock_cycle_audited", True),
        ),
        safe_facts=(
            ("sqlstate", "40P01"),
            ("cycleLength", 2),
            ("victimRef", "transaction-b"),
            ("postgresHealthy", True),
        ),
    )
    client = build_live_evidence_client(
        observation=observation,
        evidence_context=LiveEvidenceContext.local(
            incident_id="APY-LIVE-PG-DEADLOCK-001-run-1"
        ),
        cls_client=None,
        component_client=PostgresDeadlockEvidenceMcpClient(observation),
    )

    assert {item.name for item in await client.discover_tools()} == {
        "InspectPostgresDeadlockAudit",
        "InspectPostgresTransactionResult",
        "VerifyPostgresHealth",
    }


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
    return LiveFaultObservation(
        "APY-LIVE-PG-LOCK-001",
        (LiveCheck("waiter_has_lock_event", True), LiveCheck("blocker_edge_confirmed", True)),
    )


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


def test_trusted_tool_arguments_replace_only_the_execution_scope() -> None:
    model_plan: list[dict[str, object]] = [
        {
            "id": "search-current-incident",
            "tool": "SearchLog",
            "arguments": {"Query": "*"},
            "purpose": "Correlate request failures with the component evidence.",
            "testsHypotheses": ["postgres_lock_blocking"],
        },
        {
            "id": "inspect-locks",
            "tool": "InspectPostgresLockGraph",
            "arguments": {"detect_deadlocks": True},
            "purpose": "Inspect blocking edges.",
            "testsHypotheses": ["postgres_lock_blocking"],
        },
    ]
    trusted = {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
        "From": 100,
        "To": 200,
        "Query": (
            'run_id:"run-1" AND scenario_id:"APY-LIVE-PG-LOCK-001" '
            'AND incident_id:"incident-1"'
        ),
        "Limit": 20,
    }

    bound = bind_trusted_tool_arguments(model_plan, {"SearchLog": trusted})

    assert bound[0] == {
        **model_plan[0],
        "arguments": trusted,
    }
    assert bound[1] == model_plan[1]
    assert model_plan[0]["arguments"] == {"Query": "*"}


@pytest.mark.asyncio
async def test_model_search_plan_is_bound_before_contract_validation() -> None:
    class StaticChatModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "search-current-incident",
                            "tool": "SearchLog",
                            "arguments": {"Query": "*"},
                            "purpose": "Correlate current incident logs.",
                            "testsHypotheses": ["postgres_lock_blocking"],
                        }
                    ]
                }
            )

    class StaticLlmProvider:
        def create_chat_model(self) -> StaticChatModel:
            return StaticChatModel()

    trusted = {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
        "From": 100,
        "To": 200,
        "Query": (
            'run_id:"run-1" AND scenario_id:"APY-LIVE-PG-LOCK-001" '
            'AND incident_id:"incident-1"'
        ),
        "Limit": 20,
    }
    service = AiopsDiagnosticService(
        repositories=cast(Any, object()),
        llm_provider=cast(Any, StaticLlmProvider()),
        retrieval_tool=cast(Any, object()),
        mcp_client=cast(Any, object()),
        cls_region="ap-guangzhou",
        cls_topic_id="topic-live",
        trusted_tool_arguments={"SearchLog": trusted},
    )
    definitions = (
        McpToolDefinition(
            "SearchLog",
            "Search scoped CLS logs.",
            {
                "type": "object",
                "properties": {
                    "Region": {"type": "string"},
                    "TopicId": {"type": "string"},
                    "From": {"type": "integer"},
                    "To": {"type": "integer"},
                    "Query": {"type": "string"},
                    "Limit": {"type": "integer"},
                },
                "required": ["Region", "TopicId", "From", "To", "Query", "Limit"],
                "additionalProperties": False,
            },
        ),
    )

    plan, origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Investigate the current incident.",
        alert={},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=definitions,
        known_hypotheses=("postgres_lock_blocking",),
    )

    assert origin == "model"
    assert plan[0]["arguments"] == trusted
    assert plan[0]["purpose"] == "Correlate current incident logs."


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


def test_grounded_fallback_requires_one_high_confidence_cause_and_two_evidence() -> None:
    decision = build_grounded_fallback_decision(
        public_hypotheses=(
            {
                "id": "postgres_lock_blocking",
                "description": "A transaction holds a required row lock.",
            },
            {
                "id": "postgres_connectivity_failure",
                "description": "The database cannot be reached.",
            },
        ),
        hypothesis_states=(
            {
                "id": "postgres_lock_blocking",
                "status": "supported",
                "confidence": 1.0,
                "evidenceIds": ["ev-wait", "ev-graph"],
            },
            {
                "id": "postgres_connectivity_failure",
                "status": "refuted",
                "confidence": 0.0,
                "evidenceIds": ["ev-health"],
            },
        ),
        observation_decisions=(
            {
                "supports": ["postgres_lock_blocking"],
                "refutes": [],
                "summary": "A lock wait event was observed.",
                "evidenceIds": ["ev-wait"],
            },
            {
                "supports": ["postgres_lock_blocking"],
                "refutes": [],
                "summary": "A blocker-to-waiter edge was observed.",
                "evidenceIds": ["ev-graph"],
            },
        ),
        decision_vocabulary={
            "labelsByHypothesis": {
                "postgres_lock_blocking": {
                    "component": "postgresql",
                    "mechanism": "row_lock_blocking",
                },
                "postgres_connectivity_failure": {
                    "component": "postgresql",
                    "mechanism": "connectivity_failure",
                },
            }
        },
    )

    assert decision is not None
    assert decision.component == "postgresql"
    assert decision.mechanism == "row_lock_blocking"
    assert decision.trigger == "A transaction holds a required row lock."
    assert decision.causal_chain == (
        "A lock wait event was observed.",
        "A blocker-to-waiter edge was observed.",
    )
    assert decision.evidence_ids == ("ev-wait", "ev-graph")


def test_grounded_fallback_refuses_ambiguous_high_confidence_causes() -> None:
    assert (
        build_grounded_fallback_decision(
            public_hypotheses=(
                {"id": "cause-a", "description": "Cause A."},
                {"id": "cause-b", "description": "Cause B."},
            ),
            hypothesis_states=(
                {
                    "id": "cause-a",
                    "status": "supported",
                    "confidence": 1.0,
                    "evidenceIds": ["ev-1", "ev-2"],
                },
                {
                    "id": "cause-b",
                    "status": "supported",
                    "confidence": 0.95,
                    "evidenceIds": ["ev-3", "ev-4"],
                },
            ),
            observation_decisions=(),
            decision_vocabulary={
                "labelsByHypothesis": {
                    "cause-a": {"component": "service", "mechanism": "cause_a"},
                    "cause-b": {"component": "service", "mechanism": "cause_b"},
                }
            },
        )
        is None
    )


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
    assert vocabulary["labelsByHypothesis"] == {
        "postgres_lock_blocking": {
            "component": "postgresql",
            "mechanism": "row_lock_blocking",
        },
        "postgres_slow_query_without_lock": {
            "component": "postgresql",
            "mechanism": "slow_query_without_lock",
        },
        "postgres_connectivity_failure": {
            "component": "postgresql",
            "mechanism": "connectivity_failure",
        },
    }
    assert "ground_truth" not in serialized
    assert "synthetic_transaction_holds_order_row_lock" not in serialized
    assert "agent_py_live_eval" not in serialized
    assert "run_id" not in serialized


@pytest.mark.parametrize(
    ("scenario_id", "hypothesis_id", "component", "mechanism"),
    (
        (
            "APY-LIVE-PG-DEADLOCK-001",
            "postgres_deadlock",
            "postgresql",
            "opposite_order_transaction_deadlock",
        ),
        (
            "APY-LIVE-REDIS-MAXCLIENTS-001",
            "redis_maxclients",
            "live-eval-redis",
            "benchmark_clients_exhausted_maxclients",
        ),
        (
            "APY-LIVE-NGINX-TIMEOUT-001",
            "nginx_upstream_response_timeout",
            "live-eval-upstream",
            "upstream_response_exceeded_proxy_read_timeout",
        ),
    ),
)
def test_live_input_uses_candidate_wide_vocabulary_for_every_driver(
    scenario_id: str,
    hypothesis_id: str,
    component: str,
    mechanism: str,
) -> None:
    scenario = load_live_scenario(LIVE_SCENARIOS / scenario_id)

    payload = build_live_diagnostic_input(scenario)
    vocabulary = cast(dict[str, Any], payload["decisionVocabulary"])
    labels = cast(dict[str, dict[str, str]], vocabulary["labelsByHypothesis"])

    assert labels[hypothesis_id] == {
        "component": component,
        "mechanism": mechanism,
    }
    assert set(labels) == {item.id for item in scenario.hypotheses}


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
async def test_health_evidence_separates_reachability_from_business_probe() -> None:
    health = await LivePostgresEvidenceMcpClient(_observation()).call_tool(
        "VerifyServiceHealth",
        {"target": "postgres_cluster", "check_connection_pool": True},
    )

    assert isinstance(health, dict)
    assert health["databaseReachable"] is True
    assert health["connectivityStatus"] == "healthy"
    assert health["businessProbeSucceeded"] is False
    assert "probeSucceeded" not in health


@pytest.mark.asyncio
async def test_live_collector_rejects_unknown_or_mutating_arguments() -> None:
    client = LivePostgresEvidenceMcpClient(_observation())

    with pytest.raises(McpClientError, match="arguments are invalid"):
        await client.call_tool("InspectPostgresSessions", {"raw_sql": "SELECT 1"})
    with pytest.raises(McpClientError, match="arguments are invalid"):
        await client.call_tool("VerifyServiceHealth", {"restart": True})


def test_runner_boundary_appends_authorized_and_verified_recovery_facts() -> None:
    recovery = LiveRecoveryRecord(
        "terminate_postgres_backend",
        "synthetic_blocker",
        "executed_recovery",
        True,
        True,
        "authorized",
    )
    verification = LiveVerification(
        (
            LiveCheck("blocker_gone", True),
            LiveCheck("waiter_unblocked", True),
            LiveCheck("lock_graph_clear", True),
            LiveCheck("probe_succeeded", True),
            LiveCheck("postgres_healthy", True),
            LiveCheck("unrelated_sessions_untouched", True),
        )
    )

    enriched = append_live_outcome(_artifact(), recovery=recovery, verification=verification)

    assert enriched.live_recovery is not None
    assert enriched.live_recovery.action == "terminate_postgres_backend"
    assert enriched.live_recovery.approved is True
    assert enriched.live_recovery.verified is True
    assert enriched.live_recovery.target_ref == "synthetic_blocker"
    assert "101" not in json.dumps(asdict(enriched.live_recovery))
