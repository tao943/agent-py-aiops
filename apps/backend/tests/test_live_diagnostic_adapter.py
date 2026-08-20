from __future__ import annotations

import json
from collections.abc import Mapping
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
    merge_live_log_plan_step,
    plan_matches_tool_contracts,
)
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.diagnostics import (
    LivePostgresEvidenceMcpClient,
    append_live_outcome,
    benchmark_investigation_router_policy,
    build_live_diagnostic_input,
    build_live_evidence_client,
    proposal_tool_policies_for_scenario,
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


def test_live_adapter_input_carries_only_internal_benchmark_strategy() -> None:
    scenario = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")

    payload = build_live_diagnostic_input(
        scenario,
        workflow_version="evidence-driven-v4",
        investigation_strategy="multi",
    )

    assert payload["benchmarkMode"] == "live"
    assert payload["investigationStrategyMode"] == "multi"


def test_only_forced_multi_enables_benchmark_multi_agent_policy() -> None:
    assert benchmark_investigation_router_policy("multi").multi_agent_enabled is True
    assert benchmark_investigation_router_policy("single").multi_agent_enabled is False
    assert benchmark_investigation_router_policy("auto").multi_agent_enabled is False


def test_only_nginx_live_scenario_enables_the_proposal_policy() -> None:
    nginx = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-NGINX-TIMEOUT-001")
    postgres = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")
    redis = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-REDIS-MAXCLIENTS-001")

    assert proposal_tool_policies_for_scenario(nginx) == {
        "ProposeNginxTimeoutMitigation": "proposal_only"
    }
    assert proposal_tool_policies_for_scenario(postgres) is None
    assert proposal_tool_policies_for_scenario(redis) is None


def test_diagnostic_service_rejects_unknown_tool_policy_values() -> None:
    with pytest.raises(ValueError, match="Unsupported tool policy"):
        AiopsDiagnosticService(
            repositories=cast(Any, object()),
            llm_provider=cast(Any, object()),
            retrieval_tool=cast(Any, object()),
            mcp_client=cast(Any, object()),
            cls_region="unused",
            cls_topic_id="unused",
            tool_policies=cast(Any, {"DangerousTool": "execute"}),
        )


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


def _search_step() -> dict[str, object]:
    return {"id": "search-cls-logs", "tool": "SearchLog", "arguments": {}}


def test_live_log_step_is_merged_into_a_non_empty_runtime_plan() -> None:
    runtime = [{"id": "runtime-1", "tool": "InspectPostgresSessions"}]

    merged = merge_live_log_plan_step(runtime, search_step=_search_step())

    assert [step["tool"] for step in merged] == [
        "InspectPostgresSessions",
        "SearchLog",
    ]
    assert runtime == [{"id": "runtime-1", "tool": "InspectPostgresSessions"}]


def test_live_log_step_is_not_duplicated_or_added_without_cls() -> None:
    existing = [_search_step()]

    assert merge_live_log_plan_step(existing, search_step=_search_step()) == existing
    assert merge_live_log_plan_step(existing, search_step=None) == existing


def test_live_log_step_does_not_truncate_a_full_initial_plan() -> None:
    runtime = [
        {"id": f"runtime-{index}", "tool": "InspectPostgresSessions"}
        for index in range(4)
    ]

    merged = merge_live_log_plan_step(runtime, search_step=_search_step(), maximum_steps=4)

    assert len(merged) == 4
    assert merged == runtime


def test_live_log_step_rejects_an_invalid_plan_bound() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        merge_live_log_plan_step([], search_step=_search_step(), maximum_steps=0)


class _InvalidPlanChatModel:
    async def ainvoke(self, prompt: object) -> str:
        del prompt
        return "not-json"


class _InvalidPlanLlmProvider:
    def create_chat_model(self) -> _InvalidPlanChatModel:
        return _InvalidPlanChatModel()


def _cls_search_definition() -> McpToolDefinition:
    return McpToolDefinition(
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
        "cls",
    )


async def _postgres_cls_service_and_definitions(
    *, trusted_arguments: Mapping[str, object] | None
) -> tuple[AiopsDiagnosticService, tuple[McpToolDefinition, ...]]:
    runtime = tuple(
        await LivePostgresEvidenceMcpClient(_observation()).discover_tools()
    )
    service = AiopsDiagnosticService(
        repositories=cast(Any, object()),
        llm_provider=cast(Any, _InvalidPlanLlmProvider()),
        retrieval_tool=cast(Any, object()),
        mcp_client=cast(Any, object()),
        cls_region="ap-guangzhou",
        cls_topic_id="topic-live",
        trusted_tool_arguments=(
            {"SearchLog": trusted_arguments}
            if trusted_arguments is not None
            else None
        ),
    )
    return service, (*runtime, _cls_search_definition())


def _trusted_cls_arguments() -> dict[str, object]:
    return {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
        "From": 100,
        "To": 200,
        "Query": 'incident_id:"incident-public"',
        "Limit": 20,
    }


@pytest.mark.asyncio
async def test_postgres_generic_plan_keeps_runtime_and_adds_scoped_cls_log() -> None:
    trusted = _trusted_cls_arguments()
    service, definitions = await _postgres_cls_service_and_definitions(
        trusted_arguments=trusted
    )

    plan, origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Investigate the public incident evidence.",
        alert={"name": "PostgresOrderUpdateLatencyHigh", "severity": "warning"},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=definitions,
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )

    assert origin == "generic"
    assert len(plan) <= 4
    assert any(step["tool"] == "InspectPostgresSessions" for step in plan)
    log_steps = [step for step in plan if step["tool"] == "SearchLog"]
    assert len(log_steps) == 1
    assert log_steps[0]["arguments"] == trusted


@pytest.mark.asyncio
async def test_discovered_cls_tool_without_trusted_scope_is_not_forced_into_plan() -> None:
    service, definitions = await _postgres_cls_service_and_definitions(
        trusted_arguments=None
    )

    plan, origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Investigate public evidence.",
        alert={"name": "PostgresOrderUpdateLatencyHigh", "severity": "warning"},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=definitions,
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )

    assert origin == "generic"
    assert all(step["tool"] != "SearchLog" for step in plan)


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
    assert [step["causalIntent"] for step in plan] == [
        "impact",
        "mechanism",
        "trigger",
    ]


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
                            "causalIntent": "mechanism",
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
                "evidenceIds": ["ev-trigger", "ev-graph", "ev-impact"],
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
                "summary": "A transaction retained a required row lock.",
                "evidenceIds": ["ev-trigger"],
                "causalRole": "trigger",
            },
            {
                "supports": ["postgres_lock_blocking"],
                "refutes": [],
                "summary": "A blocker-to-waiter edge was observed.",
                "evidenceIds": ["ev-graph"],
                "causalRole": "mechanism",
            },
            {
                "supports": ["postgres_lock_blocking"],
                "refutes": [],
                "summary": "A lock wait event affected the waiting transaction.",
                "evidenceIds": ["ev-impact"],
                "causalRole": "impact",
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
    assert decision.trigger == "A transaction retained a required row lock."
    assert decision.causal_chain == (
        "A transaction retained a required row lock.",
        "A blocker-to-waiter edge was observed.",
        "A lock wait event affected the waiting transaction.",
    )
    assert decision.evidence_ids == ("ev-trigger", "ev-graph", "ev-impact")


def test_grounded_fallback_uses_differential_supporting_observation_evidence() -> None:
    decision = build_grounded_fallback_decision(
        public_hypotheses=(
            {"id": "process_down", "description": "The process stopped."},
            {"id": "port_mismatch", "description": "The port is wrong."},
        ),
        hypothesis_states=(
            {
                "id": "process_down",
                "status": "supported",
                "confidence": 0.95,
                "evidenceIds": ["ev-container"],
            },
            {
                "id": "port_mismatch",
                "status": "refuted",
                "confidence": 0.05,
                "evidenceIds": ["ev-container", "ev-nginx"],
            },
        ),
        observation_decisions=(
            {
                "supports": ["process_down"],
                "refutes": [],
                "summary": "The upstream process exited.",
                "evidenceIds": ["ev-container"],
                "causalRole": "trigger",
            },
            {
                "supports": ["process_down"],
                "refutes": ["port_mismatch"],
                "summary": "Nginx reached the matching route but the connection was refused.",
                "evidenceIds": ["ev-container", "ev-nginx"],
                "causalRole": "mechanism",
            },
        ),
        decision_vocabulary={
            "labelsByHypothesis": {
                "process_down": {
                    "component": "checkout-service",
                    "mechanism": "process_unavailable",
                },
                "port_mismatch": {
                    "component": "nginx",
                    "mechanism": "upstream_port_mismatch",
                },
            }
        },
    )

    assert decision is not None
    assert decision.evidence_ids == ("ev-container", "ev-nginx")


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
    assert "workflowVersion" not in payload


def test_live_input_can_request_auditable_v4() -> None:
    scenario = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")

    payload = build_live_diagnostic_input(
        scenario,
        workflow_version="evidence-driven-v4",
    )

    assert payload["workflowVersion"] == "evidence-driven-v4"
    assert payload["graphVersion"] == "aiops-diagnostic-v3"


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
    assert safe_sessions["waitingOperation"] == "order_status_update"
    assert safe_sessions["benchmarkEvidenceId"] == "postgres-wait-event-lock"
    assert safe_lock_graph["blockerEdgeConfirmed"] is True
    assert safe_lock_graph["blockerRole"] == "transaction"
    assert safe_lock_graph["lockedResource"] == "order_row"
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
    assert health["businessProbeTimedOut"] is True
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
