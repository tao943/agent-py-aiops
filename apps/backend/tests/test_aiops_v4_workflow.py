from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from langgraph.types import Send

from super_ai.aiops import AiopsDiagnosticService
from super_ai.aiops import diagnostics as diagnostics_module
from super_ai.aiops.adjudication import DiagnosticFact, HypothesisAssessment
from super_ai.aiops.causal_intents import supported_causal_coverage
from super_ai.aiops.diagnostics import (
    _initial_hypothesis_assessments,  # pyright: ignore[reportPrivateUsage]
    _project_adjudicated_observations,  # pyright: ignore[reportPrivateUsage]
)
from super_ai.aiops.investigation import InvestigationRouterPolicy
from super_ai.aiops.model_budget import ExecutionDeadlines
from super_ai.mcp_client import McpToolDefinition
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.repositories import JsonDict
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolResult,
)


class EmptyRetrieval:
    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        del owner_user_id, accessible_knowledge_base_ids
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=input.top_k or 3,
            results=[],
            citations=[],
        )


class CountingEmptyRetrieval(EmptyRetrieval):
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        self.calls += 1
        return await super().run(
            input,
            owner_user_id=owner_user_id,
            accessible_knowledge_base_ids=accessible_knowledge_base_ids,
        )


class UnusedMcpClient:
    pass


def _nginx_timeout_hypotheses() -> list[dict[str, object]]:
    return [
        {"id": hypothesis_id, "description": hypothesis_id.replace("_", " ")}
        for hypothesis_id in (
            "nginx_gateway_pressure",
            "nginx_route_mismatch",
            "nginx_upstream_response_timeout",
            "nginx_upstream_unavailable",
        )
    ]


def _nginx_timeout_steps() -> list[dict[str, object]]:
    return [
        {
            "id": f"nginx-{index}",
            "tool": tool,
            "arguments": {},
            "purpose": purpose,
            "testsHypotheses": [
                item["id"] for item in _nginx_timeout_hypotheses()
            ],
            "causalIntent": causal_intent,
            "evidenceRules": [],
        }
        for index, (tool, purpose, causal_intent) in enumerate(
            (
                (
                    "InspectNginxRequestTimeline",
                    "Inspect the affected request timeline.",
                    "impact",
                ),
                (
                    "ReadNginxTimeoutSummary",
                    "Inspect the configured read-timeout outcome.",
                    "mechanism",
                ),
                (
                    "ProbeLiveEvalUpstream",
                    "Probe the upstream independently.",
                    "context",
                ),
                ("SearchLog", "Search current-incident logs.", "context"),
            ),
            start=1,
        )
    ]


def _nginx_timeout_outputs() -> list[tuple[str, dict[str, object]]]:
    return [
        (
            "ev-timeline",
            {
                "gatewayStatus": 504,
                "requestDurationMs": 913,
                "upstreamConnectSucceeded": True,
            },
        ),
        (
            "ev-summary",
            {"gatewayTimeoutObserved": True, "readDeadlineElapsed": True},
        ),
        (
            "ev-upstream",
            {
                "status": 200,
                "healthy": True,
                "gatewayStatus": 200,
                "gatewayHealthy": True,
                "gatewayLatencyMs": 19,
            },
        ),
        (
            "ev-cls",
            {
                "recordCount": 2,
                "records": [
                    {"event": "request_received"},
                    {"event": "upstream_timeout"},
                ],
            },
        ),
    ]


def _service(repositories: object, provider: object = object()) -> AiopsDiagnosticService:
    return AiopsDiagnosticService(
        repositories=cast(Any, repositories),
        llm_provider=cast(Any, provider),
        retrieval_tool=EmptyRetrieval(),
        mcp_client=cast(Any, UnusedMcpClient()),
        cls_region="unused",
        cls_topic_id="unused",
    )


def _order_pool_hypotheses() -> list[dict[str, object]]:
    return [
        {"id": hypothesis_id, "description": hypothesis_id.replace("_", " ")}
        for hypothesis_id in (
            "order_connection_lifecycle_failure",
            "order_traffic_capacity_exceeded",
            "order_slow_statement",
            "order_database_lock_wait",
            "order_database_unreachable",
        )
    ]


def _order_pool_steps() -> list[dict[str, object]]:
    return [
        {
            "id": f"order-pool-{index}",
            "tool": tool,
            "arguments": {},
            "purpose": purpose,
            "testsHypotheses": [item["id"] for item in _order_pool_hypotheses()],
            "causalIntent": causal_intent,
            "evidenceRules": [],
        }
        for index, (tool, purpose, causal_intent) in enumerate(
            (
                ("InspectOrderPoolState", "Inspect pool capacity.", "mechanism"),
                (
                    "InspectOrderDatabaseSessions",
                    "Inspect current-run database sessions.",
                    "mechanism",
                ),
                (
                    "VerifyOrderDatabaseReachability",
                    "Verify database and business acquisition health.",
                    "impact",
                ),
                ("SearchLog", "Inspect the current incident lifecycle.", "trigger"),
            ),
            start=1,
        )
    ]


def _order_pool_outputs() -> list[tuple[str, dict[str, object]]]:
    return [
        (
            "ev-order-pool",
            {
                "poolAtCapacity": True,
                "freeConnections": 0,
                "waiterObserved": True,
            },
        ),
        (
            "ev-order-sessions",
            {
                "runScopedSessionsPresent": True,
                "databaseReachable": True,
                "lockWaitObserved": False,
            },
        ),
        (
            "ev-order-health",
            {"databaseReachable": True, "businessProbeTimedOut": True},
        ),
        (
            "ev-order-cls",
            {
                "recordCount": 4,
                "records": [
                    {"event": "request_received"},
                    {"event": "connection_checkout"},
                    {"event": "order_update_failed"},
                    {"event": "pool_acquire_timeout"},
                ],
            },
        ),
    ]


def test_v4_graph_removes_per_observation_model_nodes() -> None:
    graph = _service(object())._build_graph(  # pyright: ignore[reportPrivateUsage]
        workflow_version="evidence-driven-v4"
    )

    nodes = set(graph.get_graph().nodes)

    assert {"fact_adapter", "hypothesis_adjudicator", "deterministic_validator"} <= nodes
    assert "evidence_evaluator" not in nodes
    assert "decision_validator" not in nodes
    assert {"validator_router", "llm_validator", "manual_review"} <= nodes
    assert "knowledge_investigator" in nodes
    assert {"strategy_router", "investigator_dispatch", "evidence_aggregator"} <= nodes


def test_strategy_route_fans_out_stably_or_preserves_safe_chain() -> None:
    service = _service(object())
    dispatches = [
        {"dispatchId": "dispatch-log", "investigatorType": "log"},
        {"dispatchId": "dispatch-runtime", "investigatorType": "runtime"},
    ]

    multi = service._route_after_strategy(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {
                "investigation_route": {"strategy": "multi_agent"},
                "investigation_dispatches": dispatches,
                "owner_user_id": "owner-1",
                "task_id": "diagnostic-1",
            },
        )
    )
    single = service._route_after_strategy(  # pyright: ignore[reportPrivateUsage]
        cast(Any, {"investigation_route": {"strategy": "single_agent"}})
    )
    fast = service._route_after_strategy(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {"investigation_route": {"strategy": "deterministic_fast_path"}},
        )
    )

    assert isinstance(multi, list)
    assert all(isinstance(item, Send) for item in multi)
    investigator_types = [
        cast(Any, item).arg["investigation_dispatch"]["investigatorType"]
        for item in multi
    ]
    assert investigator_types == [
        "runtime",
        "log",
    ]
    assert single == "executor"
    assert fast == "sufficiency_gate"


@pytest.mark.asyncio
async def test_investigator_branch_cannot_write_shared_diagnostic_state() -> None:
    update = await _service(object())._investigator_dispatch(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {
                "owner_user_id": "owner-1",
                "task_id": "diagnostic-1",
                "investigation_dispatch": {
                    "dispatchId": "dispatch-runtime",
                    "investigatorType": "runtime",
                    "steps": [],
                },
            },
        )
    )

    assert set(update) == {"investigation_packets", "events"}
    packet = cast(list[dict[str, object]], update["investigation_packets"])[0]
    assert packet["status"] == "failed"
    assert not {
        "diagnostic_facts",
        "hypothesis_assessments",
        "observation_decisions",
    } & set(update)


def test_aggregation_fallback_is_budgeted_and_fail_closed() -> None:
    service = _service(object())

    assert service._route_after_aggregation(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {
                "investigation_aggregation": {
                    "completedPacketCount": 1,
                    "failedPacketCount": 1,
                }
            },
        )
    ) == "fact_adapter"
    assert service._route_after_aggregation(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {
                "investigation_aggregation": {
                    "completedPacketCount": 0,
                    "failedPacketCount": 2,
                    "fallbackPermitted": True,
                }
            },
        )
    ) == "executor"
    assert service._route_after_aggregation(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {
                "investigation_aggregation": {
                    "completedPacketCount": 0,
                    "failedPacketCount": 2,
                    "fallbackPermitted": False,
                }
            },
        )
    ) == "manual_review"


@pytest.mark.asyncio
async def test_strategy_starts_single_then_escalates_after_stagnation(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-dynamic-escalation",
            status="running",
            query="Investigate cross-source symptoms.",
            input_payload={
                "workflowVersion": "evidence-driven-v4",
                "graphVersion": "aiops-diagnostic-v3",
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, object()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=cast(Any, UnusedMcpClient()),
            cls_region="unused",
            cls_topic_id="unused",
            investigation_router_policy=InvestigationRouterPolicy(
                multi_agent_enabled=True
            ),
        )
        deadlines = ExecutionDeadlines.start()
        plan: list[dict[str, object]] = [
            {
                "id": "initial-1",
                "tool": "InspectPostgresSessions",
                "arguments": {},
                "sourceDomain": "runtime",
                "sourceDomainStatus": "trusted_registry",
                "causalIntent": "context",
                "targetComponent": "gateway",
            },
            {
                "id": "initial-2",
                "tool": "SearchLog",
                "arguments": {},
                "sourceDomain": "log",
                "sourceDomainStatus": "trusted_registry",
                "causalIntent": "context",
                "targetComponent": "gateway",
            },
            {
                "id": "remaining-runtime",
                "tool": "InspectPostgresSessions",
                "arguments": {"scope": "remaining"},
                "sourceDomain": "runtime",
                "sourceDomainStatus": "trusted_registry",
                "causalIntent": "mechanism",
                "targetComponent": "postgres",
            },
            {
                "id": "remaining-log",
                "tool": "SearchLog",
                "arguments": {"Query": "remaining"},
                "sourceDomain": "log",
                "sourceDomainStatus": "trusted_registry",
                "causalIntent": "trigger",
                "targetComponent": "order-service",
            },
        ]
        state = cast(
            Any,
            {
                "workflow_version": "evidence-driven-v4",
                "graph_version": "aiops-diagnostic-v3",
                "owner_user_id": task.owner_user_id,
                "task_id": task.id,
                "plan": plan,
                "plan_index": 0,
                "tool_definitions": (
                    McpToolDefinition(
                        "InspectPostgresSessions",
                        "Inspect sessions.",
                        {"type": "object"},
                        "default",
                    ),
                    McpToolDefinition(
                        "SearchLog",
                        "Search logs.",
                        {"type": "object"},
                        "cls",
                    ),
                ),
                "knowledge_completed": True,
                "sop_hits": [],
                "evidence_ids": ["evidence-alert"],
                "hypothesis_assessments": [
                    {"id": f"cause-{index}", "disposition": "unresolved"}
                    for index in range(3)
                ],
                "alert": {"severity": "warning"},
                "model_call_count": 0,
                "started_at": deadlines.started_at.isoformat(),
                "soft_deadline_at": deadlines.soft_deadline_at.isoformat(),
                "hard_deadline_at": deadlines.hard_deadline_at.isoformat(),
                "investigation_strategy_mode": "auto",
                "investigation_wave": 0,
            },
        )

        initial = await service._strategy_router(state)  # pyright: ignore[reportPrivateUsage]
        escalated = await service._strategy_router(  # pyright: ignore[reportPrivateUsage]
            cast(Any, {**state, "plan_index": 2})
        )
        second_wave = await service._strategy_router(  # pyright: ignore[reportPrivateUsage]
            cast(Any, {**state, "plan_index": 2, "investigation_wave": 2})
        )
    finally:
        await engine.dispose()

    assert cast(dict[str, object], initial["investigation_route"])["strategy"] == (
        "single_agent"
    )
    assert cast(dict[str, object], escalated["investigation_route"])["strategy"] == (
        "multi_agent"
    )
    assert [
        item["investigatorType"]
        for item in cast(list[dict[str, object]], escalated["investigation_dispatches"])
    ] == ["runtime", "log"]
    assert cast(dict[str, object], second_wave["investigation_route"])["strategy"] == (
        "single_agent"
    )


def test_v4_graph_version_selection_is_explicit_and_legacy_safe() -> None:
    select_graph_version = getattr(
        diagnostics_module,
        "_graph_version_for_task",
        None,
    )
    assert callable(select_graph_version)

    assert (
        select_graph_version(
            {
                "workflowVersion": "evidence-driven-v4",
                "graphVersion": "aiops-diagnostic-v3",
            }
        )
        == "aiops-diagnostic-v3"
    )
    assert (
        select_graph_version({"workflowVersion": "evidence-driven-v4"})
        == "aiops-diagnostic-v2"
    )
    service = _service(object())
    legacy_graph = service._build_graph(  # pyright: ignore[reportPrivateUsage]
        workflow_version="evidence-driven-v4",
        graph_version="aiops-diagnostic-v2",
    )
    current_graph = service._build_graph(  # pyright: ignore[reportPrivateUsage]
        workflow_version="evidence-driven-v4",
        graph_version="aiops-diagnostic-v3",
    )
    assert "knowledge_investigator" not in legacy_graph.get_graph().nodes
    assert "knowledge_investigator" in current_graph.get_graph().nodes
    assert (
        select_graph_version(
            {
                "workflowVersion": "evidence-driven-v4",
                "graphVersion": "aiops-diagnostic-v2",
            }
        )
        == "aiops-diagnostic-v2"
    )


@pytest.mark.asyncio
async def test_knowledge_investigator_runs_retrieval_once_before_planner(
    migrated_database_url: str,
) -> None:
    class EmptyDiscoveryMcpClient:
        async def discover_tools(self) -> list[McpToolDefinition]:
            return []

    class InvalidPlanModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            return "[]"

    class Provider:
        def create_chat_model(self) -> InvalidPlanModel:
            return InvalidPlanModel()

    engine = create_memory_engine(migrated_database_url)
    retrieval = CountingEmptyRetrieval()
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-knowledge-once",
            status="running",
            query="Inspect the incident.",
            input_payload={
                "workflowVersion": "evidence-driven-v4",
                "graphVersion": "aiops-diagnostic-v3",
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, Provider()),
            retrieval_tool=retrieval,
            mcp_client=cast(Any, EmptyDiscoveryMcpClient()),
            cls_region="unused",
            cls_topic_id="unused",
        )
        state = cast(
            Any,
            {
                "workflow_version": "evidence-driven-v4",
                "graph_version": "aiops-diagnostic-v3",
                "owner_user_id": task.owner_user_id,
                "task_id": task.id,
                "query": task.query,
                "alert": {},
                "accessible_knowledge_base_ids": ("kb-public",),
                "public_hypotheses": [],
                "hypothesis_states": [],
                "hypothesis_assessments": [],
                "model_call_count": 0,
                "model_call_audits": [],
            },
        )

        knowledge = await service._knowledge_investigator(  # pyright: ignore[reportPrivateUsage]
            state
        )
        repeated_knowledge = await service._knowledge_investigator(  # pyright: ignore[reportPrivateUsage]
            state
        )
        planner = await service._planner(  # pyright: ignore[reportPrivateUsage]
            cast(Any, {**state, **knowledge})
        )
    finally:
        await engine.dispose()

    assert retrieval.calls == 1
    assert repeated_knowledge["knowledge_context"] == knowledge["knowledge_context"]
    assert knowledge["knowledge_completed"] is True
    assert knowledge["knowledge_context"] == {
        "retrievalAvailable": True,
        "retrievalError": None,
        "sopHits": [],
        "noSopMatched": True,
    }
    assert planner["sop_hits"] == []


@pytest.mark.asyncio
async def test_knowledge_investigator_persists_citations_as_reference_evidence(
    migrated_database_url: str,
) -> None:
    class CitationRetrieval:
        async def run(
            self,
            input: KnowledgeRetrievalToolInput,
            *,
            owner_user_id: str,
            accessible_knowledge_base_ids: Sequence[str],
        ) -> KnowledgeRetrievalToolResult:
            del accessible_knowledge_base_ids
            hit = KnowledgeRetrievalHit(
                chunk_id="chunk-1",
                document_id="document-1",
                knowledge_base_id="kb-public",
                owner_user_id=owner_user_id,
                tenant_id=owner_user_id,
                content="Inspect the lock graph before considering recovery.",
                source="public-runbook",
                metadata={"knowledgeType": "diagnostic_card"},
                score=0.91,
            )
            citation = KnowledgeRetrievalCitationSource(
                id="citation-1",
                title="PostgreSQL lock investigation",
                source_type="knowledge_chunk",
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                knowledge_base_id=hit.knowledge_base_id,
                source=hit.source,
                metadata=hit.metadata,
                score=hit.score,
                excerpt=hit.content,
                knowledge_type="diagnostic_card",
            )
            return KnowledgeRetrievalToolResult(
                query=input.query,
                top_k=input.top_k or 3,
                results=[hit],
                citations=[citation],
            )

    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-knowledge-reference",
            status="running",
            query="Inspect database locks.",
            input_payload={
                "workflowVersion": "evidence-driven-v4",
                "graphVersion": "aiops-diagnostic-v3",
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, object()),
            retrieval_tool=CitationRetrieval(),
            mcp_client=cast(Any, UnusedMcpClient()),
            cls_region="unused",
            cls_topic_id="unused",
        )
        update = await service._knowledge_investigator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "workflow_version": "evidence-driven-v4",
                    "graph_version": "aiops-diagnostic-v3",
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "query": task.query,
                    "accessible_knowledge_base_ids": ("kb-public",),
                },
            )
        )
        evidence = await repositories.diagnostics.list_evidence(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert len(cast(list[str], update["knowledge_evidence_ids"])) == 1
    assert any(
        event.get("type") == "reference.source"
        for event in cast(list[dict[str, object]], update["events"])
    )
    assert len(evidence) == 1
    assert evidence[0].kind == "knowledge_reference"


def test_v4_replanner_uses_public_upstream_service_for_deadline_probe() -> None:
    derive_steps = getattr(
        diagnostics_module,
        "_deterministic_gap_replan_steps",
        None,
    )
    assert callable(derive_steps)

    steps = derive_steps(
        {
            "hypothesis_assessments": [
                {
                    "hypothesisId": "nginx_upstream_response_timeout",
                    "disposition": "supported",
                    "evidenceIds": ["ev-timeline", "ev-timeout"],
                    "reasonCode": "upstream_read_timeout",
                    "assessmentSource": "llm_adjudicated",
                    "hasHighQualityConflict": False,
                    "transitions": [],
                }
            ],
            "diagnostic_facts": [
                {
                    "key": "InspectGatewayRequestTimeline.upstreamService",
                    "value": "checkout-slow-endpoint",
                    "evidenceId": "ev-timeline",
                    "sourceTool": "InspectGatewayRequestTimeline",
                    "quality": "context",
                    "public": True,
                }
            ],
            "evidence_sufficiency": {"missingCausalRoles": ["trigger"]},
        },
        available_tools={"ProbeUpstreamHealth"},
    )

    assert steps == [
        {
            "id": "refine_upstream_deadline",
            "tool": "ProbeUpstreamHealth",
            "arguments": {"service": "checkout-slow-endpoint"},
            "purpose": "Probe the public upstream endpoint against its gateway deadline.",
            "testsHypotheses": ["nginx_upstream_response_timeout"],
            "causalIntent": "mechanism",
            "causalIntentOrigin": "coverage_repair",
            "evidenceRules": [],
        }
    ]


def test_v4_decision_gap_does_not_reject_mechanism_refinement() -> None:
    targets_gap = getattr(
        diagnostics_module,
        "_step_targets_replan_gap",
        None,
    )
    assert callable(targets_gap)

    mechanism_step = {"causalIntent": "mechanism"}

    assert targets_gap(
        mechanism_step,
        replan_reason="decision_validation_gap",
        missing_roles={"impact"},
    )
    assert not targets_gap(
        mechanism_step,
        replan_reason="evidence_gap",
        missing_roles={"impact"},
    )


def test_v4_readjudicates_only_after_new_public_facts() -> None:
    can_adjudicate = getattr(
        diagnostics_module,
        "_can_adjudicate_new_evidence",
        None,
    )
    assert callable(can_adjudicate)

    assert can_adjudicate({"adjudication_count": 0}, fact_count=4)
    assert not can_adjudicate(
        {"adjudication_count": 1, "adjudicated_fact_count": 4},
        fact_count=4,
    )
    assert can_adjudicate(
        {"adjudication_count": 1, "adjudicated_fact_count": 4},
        fact_count=5,
    )
    assert not can_adjudicate(
        {"adjudication_count": 2, "adjudicated_fact_count": 4},
        fact_count=5,
    )


def test_v4_normalizes_postgres_lock_chain_from_public_facts() -> None:
    normalize = getattr(
        diagnostics_module,
        "_normalize_postgres_lock_observations",
        None,
    )
    assert callable(normalize)
    assessment = HypothesisAssessment(
        hypothesis_id="postgres_lock_blocking",
        disposition="supported",
        evidence_ids=("ev-graph", "ev-session"),
        reason_code="evidence_supported",
        assessment_source="llm_adjudicated",
    )
    observations: list[JsonDict] = [
        {
            "summary": "raw graph",
            "supports": ["postgres_lock_blocking"],
            "refutes": [],
            "evidenceIds": ["ev-graph"],
            "causalRole": "trigger",
        },
        {
            "summary": "raw session",
            "supports": ["postgres_lock_blocking"],
            "refutes": [],
            "evidenceIds": ["ev-session"],
            "causalRole": "mechanism",
        },
        {
            "summary": "raw health",
            "supports": [],
            "refutes": ["postgres_connectivity_failure"],
            "evidenceIds": ["ev-health"],
            "causalRole": "impact",
        },
        {
            "summary": "raw cls",
            "supports": [],
            "refutes": [],
            "evidenceIds": ["ev-cls"],
            "causalRole": "context",
        },
    ]
    facts = (
        DiagnosticFact(
            "InspectPostgresLockGraph.blockerRole",
            "transaction",
            "ev-graph",
            "InspectPostgresLockGraph",
            "direct",
        ),
        DiagnosticFact(
            "InspectPostgresLockGraph.lockedResource",
            "order_row",
            "ev-graph",
            "InspectPostgresLockGraph",
            "direct",
        ),
        DiagnosticFact(
            "InspectPostgresSessions.waitingOperation",
            "order_status_update",
            "ev-session",
            "InspectPostgresSessions",
            "direct",
        ),
        DiagnosticFact(
            "InspectPostgresSessions.waitEventType",
            "Lock",
            "ev-session",
            "InspectPostgresSessions",
            "direct",
        ),
        DiagnosticFact(
            "VerifyServiceHealth.businessProbeTimedOut",
            True,
            "ev-health",
            "VerifyServiceHealth",
            "direct",
        ),
        DiagnosticFact(
            "VerifyServiceHealth.databaseReachable",
            True,
            "ev-health",
            "VerifyServiceHealth",
            "direct",
        ),
        DiagnosticFact(
            "SearchLog.records.event",
            ("database_contention", "alert_fired"),
            "ev-cls",
            "SearchLog",
            "direct",
        ),
    )

    normalized = cast(
        list[dict[str, object]],
        normalize(observations, assessment=assessment, facts=facts),
    )

    assert [item["summary"] for item in normalized] == [
        "A blocker transaction holds the PostgreSQL row lock required by the order status update.",
        "The order status update waits on the held PostgreSQL row lock.",
        "The blocked business probe times out while PostgreSQL remains reachable.",
        "Database contention causes the blocked business probe to time out with a request timeout.",
    ]
    assert [item["causalRole"] for item in normalized] == [
        "trigger",
        "mechanism",
        "impact",
        "impact",
    ]
    assert normalized[-1]["supports"] == ["postgres_lock_blocking"]
    assert normalized[-1]["evidenceIds"] == ["ev-cls", "ev-health"]


def test_v4_derives_postgres_deadlock_chain_from_public_audit_facts() -> None:
    assessment = HypothesisAssessment(
        hypothesis_id="postgres_deadlock",
        disposition="supported",
        evidence_ids=("ev-audit", "ev-result"),
        reason_code="evidence_supported",
        assessment_source="llm_adjudicated",
    )
    observations: list[JsonDict] = [
        {
            "summary": "raw audit",
            "supports": ["postgres_deadlock"],
            "refutes": [],
            "evidenceIds": ["ev-audit"],
            "causalRole": "trigger",
        },
        {
            "summary": "raw result",
            "supports": ["postgres_deadlock"],
            "refutes": [],
            "evidenceIds": ["ev-result"],
            "causalRole": "impact",
        },
    ]
    facts = (
        DiagnosticFact(
            "InspectPostgresDeadlockAudit.transactionAFirstResource",
            "order_row_1",
            "ev-audit",
            "InspectPostgresDeadlockAudit",
            "context",
        ),
        DiagnosticFact(
            "InspectPostgresDeadlockAudit.transactionASecondResource",
            "order_row_2",
            "ev-audit",
            "InspectPostgresDeadlockAudit",
            "context",
        ),
        DiagnosticFact(
            "InspectPostgresDeadlockAudit.transactionBFirstResource",
            "order_row_2",
            "ev-audit",
            "InspectPostgresDeadlockAudit",
            "context",
        ),
        DiagnosticFact(
            "InspectPostgresDeadlockAudit.transactionBSecondResource",
            "order_row_1",
            "ev-audit",
            "InspectPostgresDeadlockAudit",
            "context",
        ),
        DiagnosticFact(
            "InspectPostgresDeadlockAudit.cycleDetected",
            True,
            "ev-audit",
            "InspectPostgresDeadlockAudit",
            "context",
        ),
        DiagnosticFact(
            "InspectPostgresDeadlockAudit.sqlstate",
            "40P01",
            "ev-audit",
            "InspectPostgresDeadlockAudit",
            "context",
        ),
        DiagnosticFact(
            "InspectPostgresTransactionResult.aborted",
            True,
            "ev-result",
            "InspectPostgresTransactionResult",
            "context",
        ),
    )

    projected = _project_adjudicated_observations(
        observations=observations,
        assessments=(assessment,),
        facts=facts,
    )
    derived = projected[-3:]

    assert sum(item.get("causalRole") == "trigger" for item in projected) == 1
    assert [item["causalRole"] for item in derived] == [
        "trigger",
        "mechanism",
        "impact",
    ]
    assert "reverse order" in str(derived[0]["summary"])
    assert "cyclic wait" in str(derived[1]["summary"])
    assert "40P01" in str(derived[2]["summary"])
    assert derived[2]["evidenceIds"] == ["ev-audit", "ev-result"]


@pytest.mark.asyncio
async def test_v4_model_runtime_resumes_count_and_records_only_safe_audit_fields() -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, prompt: object) -> str:
            del prompt
            self.calls += 1
            return "ok"

    class Provider:
        def __init__(self) -> None:
            self.model = CountingModel()

        def create_chat_model(self) -> CountingModel:
            return self.model

    provider = Provider()
    service = _service(object(), provider)
    deadlines = ExecutionDeadlines.start()
    runtime = service._model_runtime(  # pyright: ignore[reportPrivateUsage]
        cast(
            Any,
            {
                "model_call_count": 7,
                "started_at": deadlines.started_at.isoformat(),
                "soft_deadline_at": deadlines.soft_deadline_at.isoformat(),
                "hard_deadline_at": deadlines.hard_deadline_at.isoformat(),
            },
        )
    )

    first = await service._invoke_v4_model(  # pyright: ignore[reportPrivateUsage]
        runtime,
        role="validator",
        prompt="private prompt must not be audited",
    )
    second = await service._invoke_v4_model(  # pyright: ignore[reportPrivateUsage]
        runtime,
        role="report",
        prompt="another private prompt",
    )

    assert first == "ok"
    assert second is None
    assert provider.model.calls == 1
    assert runtime.budget.used == 8
    assert all(
        set(item)
        == {"role", "attempt", "durationMs", "cacheHit", "safeErrorCode"}
        for item in runtime.audits
    )
    assert "private prompt" not in json.dumps(runtime.audits)


@pytest.mark.asyncio
async def test_v4_report_uses_template_after_hard_deadline_without_model_call() -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, prompt: object) -> str:
            del prompt
            self.calls += 1
            return "# should not run"

    class Provider:
        def __init__(self) -> None:
            self.model = CountingModel()

        def create_chat_model(self) -> CountingModel:
            return self.model

    provider = Provider()
    service = _service(object(), provider)
    now = datetime.now(timezone.utc)
    expired = ExecutionDeadlines(
        started_at=now - timedelta(minutes=10),
        soft_deadline_at=now - timedelta(minutes=5),
        hard_deadline_at=now - timedelta(minutes=2),
    )
    state = cast(
        Any,
        {
            "model_call_count": 3,
            "started_at": expired.started_at.isoformat(),
            "soft_deadline_at": expired.soft_deadline_at.isoformat(),
            "hard_deadline_at": expired.hard_deadline_at.isoformat(),
            "alert": {},
            "sop_hits": [],
            "evidence": [],
        },
    )
    runtime = service._model_runtime(state)  # pyright: ignore[reportPrivateUsage]

    content, origin = await service._generate_report_content(  # pyright: ignore[reportPrivateUsage]
        state,
        model_runtime=runtime,
    )

    assert content.startswith("# 告警分析报告")
    assert origin == "fallback"
    assert provider.model.calls == 0
    assert runtime.budget.used == 3
    assert runtime.audits[-1]["safeErrorCode"] == "hard_deadline_exceeded"


@pytest.mark.asyncio
async def test_v4_adjudicator_batches_all_unresolved_hypotheses_in_one_call(
    migrated_database_url: str,
) -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, prompt: object) -> str:
            del prompt
            self.calls += 1
            return json.dumps(
                {
                    "assessments": [
                        {
                            "hypothesisId": "cause-a",
                            "disposition": "supported",
                            "evidenceIds": ["ev-a"],
                            "reasonCode": "public_signal_supports_cause_a",
                        },
                        {
                            "hypothesisId": "cause-b",
                            "disposition": "causally_inactive",
                            "evidenceIds": ["ev-b"],
                            "reasonCode": "public_signal_excludes_cause_b",
                        },
                    ]
                }
            )

    class Provider:
        def __init__(self) -> None:
            self.model = CountingModel()

        def create_chat_model(self) -> CountingModel:
            return self.model

    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-batch-adjudicator",
            status="running",
            query="Resolve causes.",
            input_payload={},
        )
        provider = Provider()
        update = await _service(repositories, provider)._hypothesis_adjudicator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": [
                        {"id": "cause-a", "description": "First cause."},
                        {"id": "cause-b", "description": "Second cause."},
                    ],
                    "hypothesis_assessments": _initial_hypothesis_assessments(
                        cast(
                            list[JsonDict],
                            [{"id": "cause-a"}, {"id": "cause-b"}],
                        )
                    ),
                    "diagnostic_facts": [
                        {
                            "key": "InspectA.signal",
                            "value": True,
                            "evidenceId": "ev-a",
                            "sourceTool": "InspectA",
                            "quality": "direct",
                            "public": True,
                        },
                        {
                            "key": "InspectB.signal",
                            "value": False,
                            "evidenceId": "ev-b",
                            "sourceTool": "InspectB",
                            "quality": "direct",
                            "public": True,
                        },
                    ],
                },
            )
        )
    finally:
        await engine.dispose()

    assert provider.model.calls == 1
    assert update["adjudication_count"] == 1
    assert [
        item["disposition"]
        for item in cast(list[dict[str, object]], update["hypothesis_assessments"])
    ] == ["supported", "causally_inactive"]


@pytest.mark.asyncio
async def test_v4_adjudicator_projects_citations_back_to_observations(
    migrated_database_url: str,
) -> None:
    class AdjudicationModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            return json.dumps(
                {
                    "assessments": [
                        {
                            "hypothesisId": "slow_database_work",
                            "disposition": "supported",
                            "evidenceIds": ["ev-pool", "ev-postgres"],
                            "reasonCode": "database_work_exhausts_pool",
                        },
                        {
                            "hypothesisId": "traffic_capacity",
                            "disposition": "refuted",
                            "evidenceIds": ["ev-pool"],
                            "reasonCode": "pool_pressure_not_traffic_driven",
                        },
                    ]
                }
            )

    class Provider:
        def create_chat_model(self) -> AdjudicationModel:
            return AdjudicationModel()

    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-adjudicated-observations",
            status="running",
            query="Inspect database pool pressure.",
            input_payload={},
        )
        update = await _service(
            repositories, Provider()
        )._hypothesis_adjudicator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": [
                        {"id": "slow_database_work", "description": "Work is slow."},
                        {"id": "traffic_capacity", "description": "Traffic is high."},
                    ],
                    "hypothesis_assessments": _initial_hypothesis_assessments(
                        [
                            {"id": "slow_database_work"},
                            {"id": "traffic_capacity"},
                        ]
                    ),
                    "diagnostic_facts": [
                        {
                            "key": "InspectDatabasePool.waiting",
                            "value": 12,
                            "evidenceId": "ev-pool",
                            "sourceTool": "InspectDatabasePool",
                            "quality": "direct",
                            "public": True,
                        },
                        {
                            "key": "InspectPostgres.longTransactions",
                            "value": 3,
                            "evidenceId": "ev-postgres",
                            "sourceTool": "InspectPostgres",
                            "quality": "direct",
                            "public": True,
                        },
                    ],
                    "observation_decisions": [
                        {
                            "purpose": "Inspect pool pressure.",
                            "supports": [],
                            "refutes": [],
                            "summary": "The pool has waiting requests.",
                            "evidenceIds": ["ev-pool"],
                            "causalRole": "context",
                            "causalRoleOrigin": "plan_contract",
                            "assessmentSource": "deterministic",
                        },
                        {
                            "purpose": "Inspect database work.",
                            "supports": [],
                            "refutes": [],
                            "summary": "Long transactions retain connections.",
                            "evidenceIds": ["ev-postgres"],
                            "causalRole": "mechanism",
                            "causalRoleOrigin": "plan_contract",
                            "assessmentSource": "deterministic",
                        },
                    ],
                },
            )
        )
    finally:
        await engine.dispose()

    observations = cast(list[dict[str, object]], update["observation_decisions"])
    assert observations[0]["supports"] == ["slow_database_work"]
    assert observations[0]["refutes"] == ["traffic_capacity"]
    assert observations[1]["supports"] == ["slow_database_work"]
    assert observations[1]["causalRole"] == "trigger"
    assert observations[1]["causalRoleOrigin"] == "coverage_repair"
    assert all(item["assessmentSource"] == "llm_adjudicated" for item in observations)


def test_v4_adjudicator_reuses_trusted_order_pool_facts_for_causal_roles() -> None:
    hypothesis_id = "order_connection_lifecycle_failure"
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect incident-scoped CLS events.",
                "supports": [],
                "refutes": [],
                "summary": "Failed updates check out connections before acquisition times out.",
                "evidenceIds": ["ev-cls"],
                "causalRole": "trigger",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
                "testsHypotheses": [hypothesis_id],
            },
            {
                "purpose": "Inspect the order connection pool.",
                "supports": [],
                "refutes": [],
                "summary": "The pool is at capacity.",
                "evidenceIds": ["ev-pool"],
                "causalRole": "mechanism",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
                "testsHypotheses": [hypothesis_id],
            },
            {
                "purpose": "Inspect database sessions.",
                "supports": [],
                "refutes": [],
                "summary": "Run-scoped sessions are present without lock waits.",
                "evidenceIds": ["ev-sessions"],
                "causalRole": "context",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
                "testsHypotheses": [hypothesis_id],
            },
            {
                "purpose": "Verify database and business reachability.",
                "supports": [],
                "refutes": [],
                "summary": "The business probe times out while PostgreSQL is reachable.",
                "evidenceIds": ["ev-health"],
                "causalRole": "impact",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
                "testsHypotheses": [hypothesis_id],
            },
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id=hypothesis_id,
                disposition="supported",
                evidence_ids=("ev-cls",),
                reason_code="incident_logs_support_connection_lifecycle",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="SearchLog.records.event",
                value=(
                    "connection_checkout",
                    "order_update_failed",
                    "connection_checkout",
                    "order_update_failed",
                    "pool_acquire_timeout",
                ),
                evidence_id="ev-cls",
                source_tool="SearchLog",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectOrderPoolState.poolAtCapacity",
                value=True,
                evidence_id="ev-pool",
                source_tool="InspectOrderPoolState",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectOrderPoolState.freeConnections",
                value=0,
                evidence_id="ev-pool",
                source_tool="InspectOrderPoolState",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectOrderPoolState.waiterObserved",
                value=True,
                evidence_id="ev-pool",
                source_tool="InspectOrderPoolState",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectOrderDatabaseSessions.databaseReachable",
                value=True,
                evidence_id="ev-sessions",
                source_tool="InspectOrderDatabaseSessions",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectOrderDatabaseSessions.runScopedSessionsPresent",
                value=True,
                evidence_id="ev-sessions",
                source_tool="InspectOrderDatabaseSessions",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectOrderDatabaseSessions.lockWaitObserved",
                value=False,
                evidence_id="ev-sessions",
                source_tool="InspectOrderDatabaseSessions",
                quality="direct",
            ),
            DiagnosticFact(
                key="VerifyOrderDatabaseReachability.databaseReachable",
                value=True,
                evidence_id="ev-health",
                source_tool="VerifyOrderDatabaseReachability",
                quality="direct",
            ),
            DiagnosticFact(
                key="VerifyOrderDatabaseReachability.businessProbeTimedOut",
                value=True,
                evidence_id="ev-health",
                source_tool="VerifyOrderDatabaseReachability",
                quality="direct",
            ),
        ],
    )

    derived = [
        item
        for item in projected
        if item.get("causalRoleOrigin") == "trusted_fact_projection"
    ]

    assert [item["causalRole"] for item in derived] == ["mechanism", "impact"]
    assert derived[0]["evidenceIds"] == ["ev-cls", "ev-pool", "ev-sessions"]
    assert derived[1]["evidenceIds"] == ["ev-cls", "ev-health"]
    assert all(item["supports"] == [hypothesis_id] for item in derived)
    coverage = supported_causal_coverage(
        hypothesis_states=(
            {
                "id": hypothesis_id,
                "status": "supported",
                "evidenceIds": ["ev-cls"],
            },
        ),
        observation_decisions=projected,
    )
    assert coverage.missing_roles == ()


def test_v4_adjudicator_does_not_promote_order_pool_summary_without_trusted_facts() -> None:
    hypothesis_id = "order_connection_lifecycle_failure"
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect a bounded observation.",
                "supports": [],
                "refutes": [],
                "summary": "The pool is probably exhausted and requests time out.",
                "evidenceIds": ["ev-neutral"],
                "causalRole": "mechanism",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
                "testsHypotheses": [hypothesis_id],
            }
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id=hypothesis_id,
                disposition="supported",
                evidence_ids=("ev-cls",),
                reason_code="incident_logs_support_connection_lifecycle",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[],
    )

    assert all(
        item.get("causalRoleOrigin") != "trusted_fact_projection"
        for item in projected
    )
    assert projected[0]["supports"] == []


def test_v4_adjudicator_derives_grounded_nginx_port_mismatch_chain() -> None:
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect the checkout container.",
                "supports": [],
                "refutes": ["upstream_process_down"],
                "summary": "The container is healthy and listens on port 8080.",
                "evidenceIds": ["ev-container"],
                "causalRole": "context",
                "causalRoleOrigin": "trusted_evidence_rule",
                "assessmentSource": "deterministic",
            },
            {
                "purpose": "Inspect the Nginx upstream route.",
                "supports": [],
                "refutes": ["dns_resolution_failure"],
                "summary": "Nginx targets port 8081 and returns HTTP 502.",
                "evidenceIds": ["ev-nginx"],
                "causalRole": "context",
                "causalRoleOrigin": "trusted_evidence_rule",
                "assessmentSource": "deterministic",
            },
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id="upstream_port_mismatch",
                disposition="supported",
                evidence_ids=("ev-container", "ev-nginx"),
                reason_code="public_facts_show_port_mismatch",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="InspectContainer.listeningPorts",
                value=(8080,),
                evidence_id="ev-container",
                source_tool="InspectContainer",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectNginx.upstreamPort",
                value=8081,
                evidence_id="ev-nginx",
                source_tool="InspectNginx",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectNginx.error",
                value="connect() failed (111 Connection refused)",
                evidence_id="ev-nginx",
                source_tool="InspectNginx",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectNginx.responseStatus",
                value=502,
                evidence_id="ev-nginx",
                source_tool="InspectNginx",
                quality="direct",
            ),
        ],
    )

    derived = [
        item
        for item in projected
        if item.get("causalRoleOrigin") == "coverage_repair"
    ]
    assert [item["causalRole"] for item in derived] == [
        "trigger",
        "mechanism",
        "impact",
    ]
    assert derived[0]["evidenceIds"] == ["ev-container", "ev-nginx"]
    assert all(item["supports"] == ["upstream_port_mismatch"] for item in derived)


def test_v4_adjudicator_promotes_supported_redis_server_stop_to_trigger() -> None:
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect Redis availability.",
                "supports": [],
                "refutes": [],
                "summary": "Redis is stopped and is not listening.",
                "evidenceIds": ["ev-redis"],
                "causalRole": "context",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            }
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id="redis_server_availability",
                disposition="supported",
                evidence_ids=("ev-redis",),
                reason_code="redis_process_stopped",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="InspectRedis.processStatus",
                value="stopped",
                evidence_id="ev-redis",
                source_tool="InspectRedis",
                quality="context",
            )
        ],
    )

    assert projected[0]["supports"] == ["redis_server_availability"]
    assert projected[0]["causalRole"] == "trigger"
    assert projected[0]["causalRoleOrigin"] == "coverage_repair"


def test_v4_adjudicator_derives_redis_pool_recovery_trigger() -> None:
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect Redis client-pool recovery.",
                "supports": [],
                "refutes": [],
                "summary": "The pool retains stale connections after recovery.",
                "evidenceIds": ["ev-pool"],
                "causalRole": "mechanism",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            },
            {
                "purpose": "Inspect dependency impact.",
                "supports": [],
                "refutes": [],
                "summary": "Redis pool waiters increased.",
                "evidenceIds": ["ev-impact"],
                "causalRole": "impact",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            },
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id="redis_client_connection_lifecycle",
                disposition="supported",
                evidence_ids=("ev-pool", "ev-impact"),
                reason_code="stale_pool_survived_recovery",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="InspectRedisClientPool.staleConnections",
                value=24,
                evidence_id="ev-pool",
                source_tool="InspectRedisClientPool",
                quality="direct",
            ),
            DiagnosticFact(
                key="InspectRedisClientPool.waitingRequests",
                value=26,
                evidence_id="ev-pool",
                source_tool="InspectRedisClientPool",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectRedisClientPool.poolGenerationChangedAfterRecovery",
                value=False,
                evidence_id="ev-pool",
                source_tool="InspectRedisClientPool",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectRedisClientPool.directNewConnectionPing",
                value="PONG",
                evidence_id="ev-pool",
                source_tool="InspectRedisClientPool",
                quality="context",
            ),
            DiagnosticFact(
                key="GetServiceMetrics.redisPoolWaiters",
                value=26,
                evidence_id="ev-impact",
                source_tool="GetServiceMetrics",
                quality="context",
            ),
        ],
    )

    triggers = [
        item
        for item in projected
        if item.get("causalRole") == "trigger"
        and item.get("causalRoleOrigin") == "coverage_repair"
    ]
    assert len(triggers) == 1
    assert triggers[0]["supports"] == ["redis_client_connection_lifecycle"]
    assert triggers[0]["evidenceIds"] == ["ev-pool"]


def test_v4_adjudicator_derives_redis_maxclients_trigger_and_impact() -> None:
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect Redis server capacity.",
                "supports": [],
                "refutes": [],
                "summary": "Connected clients equal maxclients.",
                "evidenceIds": ["ev-capacity"],
                "causalRole": "context",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            },
            {
                "purpose": "Inspect Redis rejection counters.",
                "supports": [],
                "refutes": [],
                "summary": "Rejected connections increased.",
                "evidenceIds": ["ev-rejections"],
                "causalRole": "mechanism",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            },
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id="redis_maxclients",
                disposition="supported",
                evidence_ids=("ev-capacity", "ev-rejections"),
                reason_code="redis_reached_client_limit",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="InspectRedisServer.connectedClients",
                value=16,
                evidence_id="ev-capacity",
                source_tool="InspectRedisServer",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectRedisServer.maxclients",
                value=16,
                evidence_id="ev-capacity",
                source_tool="InspectRedisServer",
                quality="context",
            ),
            DiagnosticFact(
                key="GetRedisConnectionMetrics.rejectedConnectionsDelta",
                value=83,
                evidence_id="ev-rejections",
                source_tool="GetRedisConnectionMetrics",
                quality="context",
            ),
            DiagnosticFact(
                key="GetRedisConnectionMetrics.successfulExistingOperations",
                value=4201,
                evidence_id="ev-rejections",
                source_tool="GetRedisConnectionMetrics",
                quality="context",
            ),
            DiagnosticFact(
                key="GetRedisConnectionMetrics.connectionRefusedAtNetworkLayer",
                value=0,
                evidence_id="ev-rejections",
                source_tool="GetRedisConnectionMetrics",
                quality="context",
            ),
        ],
    )

    derived = [
        item
        for item in projected
        if item.get("causalRoleOrigin") == "coverage_repair"
    ]
    assert [item["causalRole"] for item in derived] == ["trigger", "impact"]
    assert derived[0]["evidenceIds"] == ["ev-capacity"]
    assert derived[1]["evidenceIds"] == ["ev-rejections"]
    assert all(item["supports"] == ["redis_maxclients"] for item in derived)


def test_v4_adjudicator_derives_redis_chain_from_live_server_info() -> None:
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect live Redis server capacity.",
                "supports": [],
                "refutes": [],
                "summary": "Connected clients reached maxclients and a connection was rejected.",
                "evidenceIds": ["ev-info"],
                "causalRole": "mechanism",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            }
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id="redis_maxclients",
                disposition="supported",
                evidence_ids=("ev-info", "ev-cls"),
                reason_code="evidence_supports",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="InspectRedisServerInfo.connectedClients",
                value=16,
                evidence_id="ev-info",
                source_tool="InspectRedisServerInfo",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectRedisServerInfo.maxclients",
                value=16,
                evidence_id="ev-info",
                source_tool="InspectRedisServerInfo",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectRedisServerInfo.rejectedConnectionsDelta",
                value=1,
                evidence_id="ev-info",
                source_tool="InspectRedisServerInfo",
                quality="context",
            ),
            DiagnosticFact(
                key="ListBenchmarkRedisClients.currentRunClientCount",
                value=15,
                evidence_id="ev-clients",
                source_tool="ListBenchmarkRedisClients",
                quality="context",
            ),
            DiagnosticFact(
                key="VerifyRedisPing.establishedConnectionHealthy",
                value=True,
                evidence_id="ev-ping",
                source_tool="VerifyRedisPing",
                quality="context",
            ),
        ],
    )

    derived = [
        item
        for item in projected
        if item.get("causalRoleOrigin") == "coverage_repair"
    ]
    assert [item["causalRole"] for item in derived] == [
        "trigger",
        "context",
        "impact",
    ]
    assert derived[0]["evidenceIds"] == ["ev-info", "ev-clients"]
    assert "current-run benchmark clients" in str(derived[0]["summary"]).lower()
    assert "maxclients limit" in str(derived[0]["summary"]).lower()
    assert derived[1]["evidenceIds"] == ["ev-info", "ev-ping"]
    assert "ping succeeds" in str(derived[1]["summary"]).lower()
    assert derived[2]["evidenceIds"] == ["ev-info"]
    assert "causing new connections to fail" in str(derived[2]["summary"]).lower()
    assert all(item["supports"] == ["redis_maxclients"] for item in derived)


def test_v4_adjudicator_derives_nginx_chain_from_live_timeout_facts() -> None:
    projected = _project_adjudicated_observations(
        observations=[
            {
                "purpose": "Inspect the Nginx request timeline.",
                "supports": [],
                "refutes": [],
                "summary": "The gateway returned 504 after the read deadline.",
                "evidenceIds": ["ev-timeline"],
                "causalRole": "mechanism",
                "causalRoleOrigin": "plan_contract",
                "assessmentSource": "deterministic",
            }
        ],
        assessments=[
            HypothesisAssessment(
                hypothesis_id="nginx_upstream_response_timeout",
                disposition="supported",
                evidence_ids=("ev-timeline", "ev-cls"),
                reason_code="evidence_supports",
                assessment_source="llm_adjudicated",
            )
        ],
        facts=[
            DiagnosticFact(
                key="InspectNginxRequestTimeline.gatewayStatus",
                value=504,
                evidence_id="ev-timeline",
                source_tool="InspectNginxRequestTimeline",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectNginxRequestTimeline.requestDurationMs",
                value=757,
                evidence_id="ev-timeline",
                source_tool="InspectNginxRequestTimeline",
                quality="context",
            ),
            DiagnosticFact(
                key="InspectNginxRequestTimeline.upstreamConnectSucceeded",
                value=True,
                evidence_id="ev-timeline",
                source_tool="InspectNginxRequestTimeline",
                quality="context",
            ),
            DiagnosticFact(
                key="ReadNginxTimeoutSummary.gatewayTimeoutObserved",
                value=True,
                evidence_id="ev-summary",
                source_tool="ReadNginxTimeoutSummary",
                quality="context",
            ),
            DiagnosticFact(
                key="ReadNginxTimeoutSummary.readDeadlineElapsed",
                value=True,
                evidence_id="ev-summary",
                source_tool="ReadNginxTimeoutSummary",
                quality="context",
            ),
            DiagnosticFact(
                key="ProbeLiveEvalUpstream.status",
                value=200,
                evidence_id="ev-health",
                source_tool="ProbeLiveEvalUpstream",
                quality="context",
            ),
            DiagnosticFact(
                key="ProbeLiveEvalUpstream.healthy",
                value=True,
                evidence_id="ev-health",
                source_tool="ProbeLiveEvalUpstream",
                quality="context",
            ),
        ],
    )

    derived = [
        item
        for item in projected
        if item.get("causalRoleOrigin") == "coverage_repair"
    ]
    assert [item["causalRole"] for item in derived] == [
        "trigger",
        "context",
        "impact",
    ]
    assert derived[0]["evidenceIds"] == ["ev-timeline", "ev-summary"]
    assert "slow response" in str(derived[0]["summary"]).lower()
    assert "proxy read timeout" in str(derived[0]["summary"]).lower()
    assert derived[1]["evidenceIds"] == ["ev-timeline"]
    assert "upstream connect succeeds" in str(derived[1]["summary"]).lower()
    assert derived[2]["evidenceIds"] == [
        "ev-timeline",
        "ev-summary",
        "ev-health",
    ]
    assert "causes nginx to return http 504" in str(derived[2]["summary"]).lower()
    assert all(
        item["supports"] == ["nginx_upstream_response_timeout"] for item in derived
    )


@pytest.mark.asyncio
async def test_v4_adjudicator_retries_one_invalid_batch_within_model_budget(
    migrated_database_url: str,
) -> None:
    class CorrectingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, prompt: object) -> str:
            del prompt
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "assessments": [
                            {
                                "hypothesisId": "cause-a",
                                "disposition": "supported",
                                "evidenceIds": ["ev-a"],
                                "reasonCode": "public_signal_supports_cause_a",
                                "confidence": 0.9,
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "assessments": [
                        {
                            "hypothesisId": "cause-a",
                            "disposition": "supported",
                            "evidenceIds": ["ev-a"],
                            "reasonCode": "public_signal_supports_cause_a",
                        }
                    ]
                }
            )

    class Provider:
        def __init__(self) -> None:
            self.model = CorrectingModel()

        def create_chat_model(self) -> CorrectingModel:
            return self.model

    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-adjudicator-correction-retry",
            status="running",
            query="Resolve one cause.",
            input_payload={},
        )
        provider = Provider()
        update = await _service(
            repositories, provider
        )._hypothesis_adjudicator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": [
                        {"id": "cause-a", "description": "First cause."}
                    ],
                    "hypothesis_assessments": _initial_hypothesis_assessments(
                        [{"id": "cause-a"}]
                    ),
                    "diagnostic_facts": [
                        {
                            "key": "InspectA.signal",
                            "value": True,
                            "evidenceId": "ev-a",
                            "sourceTool": "InspectPostgres",
                            "quality": "direct",
                            "public": True,
                        }
                    ],
                    "observation_decisions": [],
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        payload = steps[-1].payload
    finally:
        await engine.dispose()

    assert provider.model.calls == 2
    assert update["adjudication_count"] == 1
    assert update["model_call_count"] == 2
    assessments = cast(list[dict[str, object]], update["hypothesis_assessments"])
    assert assessments[0]["disposition"] == "supported"
    assert payload["adjudicationAttempts"] == 2
    assert payload["adjudicationErrorCategory"] == "corrected_invalid_batch"


@pytest.mark.asyncio
async def test_v4_adjudicator_retries_a_grounded_but_unbuildable_batch(
    migrated_database_url: str,
) -> None:
    class CompletingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, prompt: object) -> str:
            del prompt
            self.calls += 1
            evidence_ids = (
                ["ev-trigger"]
                if self.calls == 1
                else ["ev-trigger", "ev-mechanism", "ev-impact"]
            )
            return json.dumps(
                {
                    "assessments": [
                        {
                            "hypothesisId": "redis_server_availability",
                            "disposition": "supported",
                            "evidenceIds": evidence_ids,
                            "reasonCode": "redis_server_is_stopped",
                        }
                    ]
                }
            )

    class Provider:
        def __init__(self) -> None:
            self.model = CompletingModel()

        def create_chat_model(self) -> CompletingModel:
            return self.model

    observations: list[JsonDict] = [
        {
            "purpose": "Inspect Redis availability.",
            "supports": [],
            "refutes": [],
            "summary": "Redis is stopped and is not listening.",
            "evidenceIds": ["ev-trigger"],
            "causalRole": "context",
            "causalRoleOrigin": "plan_contract",
            "assessmentSource": "deterministic",
        },
        {
            "purpose": "Inspect client errors.",
            "supports": [],
            "refutes": [],
            "summary": "The client receives connection refused.",
            "evidenceIds": ["ev-mechanism"],
            "causalRole": "mechanism",
            "causalRoleOrigin": "plan_contract",
            "assessmentSource": "deterministic",
        },
        {
            "purpose": "Inspect dependency impact.",
            "supports": [],
            "refutes": [],
            "summary": "Redis dependency errors increased.",
            "evidenceIds": ["ev-impact"],
            "causalRole": "impact",
            "causalRoleOrigin": "plan_contract",
            "assessmentSource": "deterministic",
        },
    ]
    facts = [
        {
            "key": "InspectRedis.processStatus",
            "value": "stopped",
            "evidenceId": "ev-trigger",
            "sourceTool": "InspectRedis",
            "quality": "context",
            "public": True,
        },
        {
            "key": "InspectRedisClientPool.lastError",
            "value": "connection refused",
            "evidenceId": "ev-mechanism",
            "sourceTool": "InspectRedisClientPool",
            "quality": "context",
            "public": True,
        },
        {
            "key": "GetServiceMetrics.redisErrorRatePercent",
            "value": 74,
            "evidenceId": "ev-impact",
            "sourceTool": "GetServiceMetrics",
            "quality": "context",
            "public": True,
        },
    ]
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-adjudicator-causal-correction",
            status="running",
            query="Resolve Redis availability.",
            input_payload={},
        )
        provider = Provider()
        update = await _service(
            repositories, provider
        )._hypothesis_adjudicator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": [
                        {
                            "id": "redis_server_availability",
                            "description": "Redis is unavailable.",
                        }
                    ],
                    "decision_vocabulary": {
                        "labelsByHypothesis": {
                            "redis_server_availability": {
                                "component": "redis-server",
                                "mechanism": "server_unavailable",
                            }
                        }
                    },
                    "hypothesis_assessments": _initial_hypothesis_assessments(
                        [{"id": "redis_server_availability"}]
                    ),
                    "diagnostic_facts": facts,
                    "observation_decisions": observations,
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        payload = steps[-1].payload
    finally:
        await engine.dispose()

    assert provider.model.calls == 2
    assert update["model_call_count"] == 2
    assert payload["adjudicationAttempts"] == 2
    assert payload["adjudicationErrorCategory"] == "corrected_insufficient_coverage"
    assessments = cast(list[dict[str, object]], update["hypothesis_assessments"])
    assert assessments[0]["evidenceIds"] == [
        "ev-impact",
        "ev-mechanism",
        "ev-trigger",
    ]


@pytest.mark.asyncio
async def test_fact_adapter_reduces_trusted_rules_without_a_model_call(
    migrated_database_url: str,
) -> None:
    public_hypotheses: list[JsonDict] = [
        {"id": "upstream_process_down", "description": "The process stopped."},
        {"id": "upstream_port_mismatch", "description": "The port is wrong."},
    ]
    plan: list[JsonDict] = [
        {
            "id": "container",
            "tool": "InspectContainer",
            "arguments": {},
            "purpose": "Inspect the process state.",
            "testsHypotheses": ["upstream_process_down"],
            "causalIntent": "mechanism",
            "evidenceRules": [
                {
                    "templateId": "container_process_exited",
                    "hypothesisId": "upstream_process_down",
                    "parameters": {"statusFact": "InspectContainer.status"},
                }
            ],
        }
    ]
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-fact-adapter",
            status="running",
            query="Inspect upstream.",
            input_payload={},
        )
        update = await _service(repositories)._fact_adapter(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": public_hypotheses,
                    "hypothesis_assessments": _initial_hypothesis_assessments(
                        public_hypotheses
                    ),
                    "diagnostic_facts": [],
                    "evidence_ids": ["ev-container"],
                    "plan": plan,
                    "current_plan_step": plan[0],
                    "current_evidence_id": "ev-container",
                    "current_evidence_summary": "The upstream process exited.",
                    "current_tool_output": {"status": "exited"},
                },
            )
        )
    finally:
        await engine.dispose()

    assessments = {
        item["hypothesisId"]: item
        for item in cast(list[dict[str, object]], update["hypothesis_assessments"])
    }
    assert assessments["upstream_process_down"]["disposition"] == "supported"
    assert assessments["upstream_process_down"]["evidenceIds"] == ["ev-container"]
    assert assessments["upstream_port_mismatch"]["disposition"] == "unresolved"


@pytest.mark.asyncio
async def test_fact_adapter_does_not_promote_competitor_refutation_to_support(
    migrated_database_url: str,
) -> None:
    hypotheses: list[JsonDict] = [
        {"id": "upstream_process_down", "description": "The process stopped."},
        {"id": "upstream_port_mismatch", "description": "The port is wrong."},
        {"id": "dns_resolution_failure", "description": "DNS failed."},
    ]
    plan: list[JsonDict] = [
        {
            "id": "container",
            "tool": "InspectContainer",
            "arguments": {},
            "purpose": "Inspect the process state.",
            "testsHypotheses": ["upstream_process_down", "upstream_port_mismatch"],
            "causalIntent": "context",
            "evidenceRules": [
                {
                    "templateId": "container_process_exited",
                    "hypothesisId": "upstream_process_down",
                    "parameters": {"statusFact": "InspectContainer.status"},
                }
            ],
        },
        {
            "id": "nginx",
            "tool": "InspectNginx",
            "arguments": {},
            "purpose": "Inspect the resolved upstream route.",
            "testsHypotheses": ["upstream_port_mismatch", "dns_resolution_failure"],
            "causalIntent": "mechanism",
            "evidenceRules": [
                {
                    "templateId": "nginx_upstream_port_matches_container_port",
                    "hypothesisId": "upstream_port_mismatch",
                    "parameters": {
                        "nginxFact": "InspectNginx.upstreamPort",
                        "containerFact": "InspectContainer.configuredPorts",
                    },
                },
                {
                    "templateId": "nginx_resolved_address_present",
                    "hypothesisId": "dns_resolution_failure",
                    "parameters": {
                        "addressesFact": "InspectNginx.resolvedAddresses"
                    },
                },
            ],
        },
    ]
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-cross-evidence-support",
            status="running",
            query="Inspect an unavailable upstream.",
            input_payload={},
        )
        service = _service(repositories)
        first = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": hypotheses,
                    "hypothesis_assessments": _initial_hypothesis_assessments(
                        hypotheses
                    ),
                    "diagnostic_facts": [],
                    "evidence_ids": ["ev-container"],
                    "plan": plan,
                    "current_plan_step": plan[0],
                    "current_evidence_id": "ev-container",
                    "current_evidence_summary": "The upstream process exited.",
                    "current_tool_output": {
                        "status": "exited",
                        "configuredPorts": [8080],
                    },
                },
            )
        )
        second = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": hypotheses,
                    "hypothesis_assessments": first["hypothesis_assessments"],
                    "diagnostic_facts": first["diagnostic_facts"],
                    "evidence_ids": ["ev-container", "ev-nginx"],
                    "plan": plan,
                    "current_plan_step": plan[1],
                    "current_evidence_id": "ev-nginx",
                    "current_evidence_summary": (
                        "Nginx resolved the matching port but the connection was refused."
                    ),
                    "current_tool_output": {
                        "upstreamPort": 8080,
                        "resolvedAddresses": ["192.0.2.10"],
                        "responseStatus": 502,
                    },
                },
            )
        )
    finally:
        await engine.dispose()

    first_observation = cast(
        list[dict[str, object]], first["observation_decisions"]
    )[0]
    assert first_observation["causalRole"] == "trigger"
    observation = cast(list[dict[str, object]], second["observation_decisions"])[0]
    assert observation["supports"] == []
    assert observation["refutes"] == [
        "dns_resolution_failure",
        "upstream_port_mismatch",
    ]
    assert set(cast(list[str], observation["evidenceIds"])) == {
        "ev-container",
        "ev-nginx",
    }


@pytest.mark.asyncio
async def test_fact_adapter_deterministically_closes_redis_availability_chain(
    migrated_database_url: str,
) -> None:
    hypotheses: list[JsonDict] = [
        {"id": "redis_server_availability", "description": "Redis is unavailable."},
        {
            "id": "redis_client_connection_lifecycle",
            "description": "The client retains unusable connections.",
        },
        {"id": "redis_network_path", "description": "The network path failed."},
    ]
    plan: list[JsonDict] = [
        {
            "id": "redis",
            "tool": "InspectRedis",
            "arguments": {},
            "purpose": "Inspect Redis availability.",
            "testsHypotheses": ["redis_server_availability"],
            "causalIntent": "context",
            "evidenceRules": [],
        },
        {
            "id": "pool",
            "tool": "InspectRedisClientPool",
            "arguments": {},
            "purpose": "Inspect the client pool.",
            "testsHypotheses": [
                "redis_client_connection_lifecycle",
                "redis_network_path",
            ],
            "causalIntent": "mechanism",
            "evidenceRules": [],
        },
        {
            "id": "metrics",
            "tool": "GetServiceMetrics",
            "arguments": {},
            "purpose": "Inspect Redis dependency impact.",
            "testsHypotheses": [
                "redis_server_availability",
                "redis_network_path",
            ],
            "causalIntent": "impact",
            "evidenceRules": [],
        },
        {
            "id": "changes",
            "tool": "GetDeploymentChanges",
            "arguments": {},
            "purpose": "Inspect unrelated deployment changes.",
            "testsHypotheses": ["redis_client_connection_lifecycle"],
            "causalIntent": "trigger",
            "evidenceRules": [],
        },
    ]
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-deterministic-redis-availability",
            status="running",
            query="Resolve Redis request failures.",
            input_payload={},
        )
        service = _service(repositories)
        state: dict[str, object] = {
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "public_hypotheses": hypotheses,
            "hypothesis_assessments": _initial_hypothesis_assessments(hypotheses),
            "diagnostic_facts": [],
            "plan": plan,
        }
        outputs = [
            (
                "ev-redis",
                "Redis is stopped and not listening.",
                {"processStatus": "stopped", "listening": False},
            ),
            (
                "ev-pool",
                "No stale connections; the endpoint refuses connections.",
                {
                    "waitingRequests": 0,
                    "staleConnections": 0,
                    "lastError": "connection refused",
                },
            ),
            (
                "ev-impact",
                "Redis dependency errors increased.",
                {"redisErrorRatePercent": 74},
            ),
            (
                "ev-change",
                "Only an unrelated UI release was found.",
                {"changes": [{"component": "storefront-ui"}]},
            ),
        ]
        observations: list[dict[str, object]] = []
        evidence_ids: list[str] = []
        for step, (evidence_id, summary, output) in zip(plan, outputs, strict=True):
            evidence_ids.append(evidence_id)
            update = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
                cast(
                    Any,
                    {
                        **state,
                        "evidence_ids": list(evidence_ids),
                        "current_plan_step": step,
                        "current_evidence_id": evidence_id,
                        "current_evidence_summary": summary,
                        "current_tool_output": output,
                    },
                )
            )
            state["hypothesis_assessments"] = update["hypothesis_assessments"]
            state["diagnostic_facts"] = update["diagnostic_facts"]
            observations.extend(
                cast(list[dict[str, object]], update["observation_decisions"])
            )
    finally:
        await engine.dispose()

    assessments = {
        item["hypothesisId"]: item
        for item in cast(
            list[dict[str, object]], state["hypothesis_assessments"]
        )
    }
    assert assessments["redis_server_availability"]["disposition"] == "supported"
    assert assessments["redis_client_connection_lifecycle"]["disposition"] == "refuted"
    assert assessments["redis_network_path"]["disposition"] == "refuted"
    supported = [
        item
        for item in observations
        if "redis_server_availability" in cast(list[object], item["supports"])
    ]
    assert [item["causalRole"] for item in supported] == [
        "trigger",
        "mechanism",
        "impact",
    ]
    assert observations[-1]["supports"] == []


@pytest.mark.asyncio
async def test_fact_adapter_closes_nginx_timeout_from_current_task_trusted_pattern(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-nginx-trusted-pattern",
            status="running",
            query="Resolve a gateway timeout.",
            input_payload={},
        )
        hypotheses = _nginx_timeout_hypotheses()
        steps = _nginx_timeout_steps()
        state: dict[str, object] = {
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "workflow_version": "evidence-driven-v4",
            "public_hypotheses": hypotheses,
            "hypothesis_assessments": _initial_hypothesis_assessments(hypotheses),
            "diagnostic_facts": [],
            "observation_decisions": [],
            "evidence_ids": [],
            "plan": steps,
            "decision_vocabulary": {
                "labelsByHypothesis": {
                    "nginx_upstream_response_timeout": {
                        "component": "live-eval-upstream",
                        "mechanism": "upstream_response_exceeded_proxy_read_timeout",
                    }
                }
            },
        }
        observations: list[dict[str, object]] = []
        evidence_ids: list[str] = []
        service = _service(repositories)
        for step, (evidence_id, output) in zip(
            steps, _nginx_timeout_outputs(), strict=True
        ):
            evidence_ids.append(evidence_id)
            update = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
                cast(
                    Any,
                    {
                        **state,
                        "current_plan_step": step,
                        "current_evidence_id": evidence_id,
                        "current_evidence_summary": "Bounded current-task evidence.",
                        "current_tool_output": output,
                        "evidence_ids": list(evidence_ids),
                    },
                )
            )
            state["hypothesis_assessments"] = update["hypothesis_assessments"]
            state["diagnostic_facts"] = update["diagnostic_facts"]
            observations.extend(
                cast(list[dict[str, object]], update["observation_decisions"])
            )
            state["observation_decisions"] = observations
        state.update(
            {
                "evidence_ids": evidence_ids,
                "plan_index": len(steps),
                "executor_attempt_count": len(steps),
                "max_total_steps": 6,
                "max_replans": 1,
            }
        )
        sufficiency = await service._sufficiency_gate_v4(  # pyright: ignore[reportPrivateUsage]
            cast(Any, state)
        )
        decision = await service._decision_v4(  # pyright: ignore[reportPrivateUsage]
            cast(Any, state)
        )
    finally:
        await engine.dispose()

    assessments = {
        item["hypothesisId"]: item
        for item in cast(
            list[dict[str, object]], state["hypothesis_assessments"]
        )
    }
    assert assessments["nginx_upstream_response_timeout"]["disposition"] == "supported"
    assert assessments["nginx_route_mismatch"]["disposition"] == "refuted"
    assert assessments["nginx_upstream_unavailable"]["disposition"] == "refuted"
    assert assessments["nginx_gateway_pressure"]["disposition"] == "causally_inactive"
    trusted = [
        item
        for item in observations
        if item.get("causalRoleOrigin") == "trusted_compound_pattern"
    ]
    assert [item["causalRole"] for item in trusted] == [
        "trigger",
        "mechanism",
        "impact",
    ]
    assert cast(dict[str, object], sufficiency["evidence_sufficiency"])["status"] == (
        "sufficient"
    )
    assert sufficiency["next_route"] == "decision"
    root_cause = cast(dict[str, object], decision["root_cause_decision"])
    assert root_cause["component"] == "live-eval-upstream"
    assert root_cause["mechanism"] == (
        "upstream_response_exceeded_proxy_read_timeout"
    )
    assert root_cause["trigger"]
    assert len(cast(list[object], root_cause["causalChain"])) >= 2


@pytest.mark.asyncio
async def test_fact_adapter_closes_order_pool_from_persisted_scoped_provenance(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-order-pool-trusted-pattern",
            status="running",
            query="Resolve an order-api pool acquisition timeout.",
            input_payload={},
        )
        hypotheses = _order_pool_hypotheses()
        steps = _order_pool_steps()
        state: dict[str, object] = {
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "workflow_version": "evidence-driven-v4",
            "public_hypotheses": hypotheses,
            "hypothesis_assessments": _initial_hypothesis_assessments(hypotheses),
            "diagnostic_facts": [],
            "observation_decisions": [],
            "evidence_ids": [],
            "plan": steps,
        }
        service = _service(repositories)
        evidence_ids: list[str] = []
        observations: list[dict[str, object]] = []
        assert repositories.tool_call_audits is not None
        for step, (evidence_id, output) in zip(
            steps,
            _order_pool_outputs(),
            strict=True,
        ):
            tool_name = str(step["tool"])
            audit_id = f"audit-{evidence_id}"
            await repositories.tool_call_audits.create_for_diagnostic_task(
                owner_user_id=task.owner_user_id,
                audit_id=audit_id,
                diagnostic_task_id=task.id,
                tool_name=tool_name,
                arguments={},
            )
            await repositories.tool_call_audits.finalize(
                owner_user_id=task.owner_user_id,
                audit_id=audit_id,
                status="completed",
                result_summary="Bounded current-task evidence.",
            )
            await repositories.diagnostics.create_evidence(
                owner_user_id=task.owner_user_id,
                evidence_id=evidence_id,
                task_id=task.id,
                kind="tool_observation",
                source=tool_name,
                summary="Bounded current-task evidence.",
                payload={
                    "sourceFingerprint": f"source:{tool_name}",
                    "arguments": {},
                    "output": output,
                },
                tool_call_id=audit_id,
            )
            evidence_ids.append(evidence_id)
            update = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
                cast(
                    Any,
                    {
                        **state,
                        "current_plan_step": step,
                        "current_evidence_id": evidence_id,
                        "current_evidence_summary": "Bounded current-task evidence.",
                        "current_tool_output": output,
                        "evidence_ids": list(evidence_ids),
                    },
                )
            )
            state["hypothesis_assessments"] = update["hypothesis_assessments"]
            state["diagnostic_facts"] = update["diagnostic_facts"]
            observations.extend(
                cast(list[dict[str, object]], update["observation_decisions"])
            )
            state["observation_decisions"] = observations
    finally:
        await engine.dispose()

    assessments = {
        item["hypothesisId"]: item
        for item in cast(
            list[dict[str, object]],
            state["hypothesis_assessments"],
        )
    }
    assert assessments["order_connection_lifecycle_failure"]["disposition"] == (
        "supported"
    )
    assert assessments["order_database_unreachable"]["disposition"] == "refuted"
    assert assessments["order_database_lock_wait"]["disposition"] == "refuted"
    trusted = [
        item
        for item in observations
        if item.get("causalRoleOrigin") == "trusted_compound_pattern"
    ]
    assert [item["causalRole"] for item in trusted] == [
        "trigger",
        "mechanism",
        "impact",
    ]


@pytest.mark.asyncio
async def test_fact_adapter_rejects_foreign_task_fact_from_trusted_pattern(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-nginx-foreign-fact",
            status="running",
            query="Reject foreign evidence.",
            input_payload={},
        )
        hypotheses = _nginx_timeout_hypotheses()
        steps = _nginx_timeout_steps()[:-1]
        outputs = _nginx_timeout_outputs()[:-1]
        state: dict[str, object] = {
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "public_hypotheses": hypotheses,
            "hypothesis_assessments": _initial_hypothesis_assessments(hypotheses),
            "diagnostic_facts": [
                {
                    "key": "SearchLog.records.event",
                    "value": ["upstream_timeout"],
                    "evidenceId": "ev-another-task",
                    "sourceTool": "SearchLog",
                    "quality": "direct",
                    "public": True,
                }
            ],
            "evidence_ids": [],
            "plan": steps,
        }
        evidence_ids: list[str] = []
        service = _service(repositories)
        for step, (evidence_id, output) in zip(steps, outputs, strict=True):
            evidence_ids.append(evidence_id)
            update = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
                cast(
                    Any,
                    {
                        **state,
                        "current_plan_step": step,
                        "current_evidence_id": evidence_id,
                        "current_evidence_summary": "Bounded current-task evidence.",
                        "current_tool_output": output,
                        "evidence_ids": list(evidence_ids),
                    },
                )
            )
            state["hypothesis_assessments"] = update["hypothesis_assessments"]
            state["diagnostic_facts"] = update["diagnostic_facts"]
    finally:
        await engine.dispose()

    assessments = cast(
        list[dict[str, object]], state["hypothesis_assessments"]
    )
    assert all(item["disposition"] == "unresolved" for item in assessments)


@pytest.mark.asyncio
async def test_fact_adapter_derives_upstream_deadline_trigger_from_probe(
    migrated_database_url: str,
) -> None:
    hypothesis = "nginx_upstream_response_timeout"
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-upstream-deadline-trigger",
            status="running",
            query="Resolve an upstream timeout.",
            input_payload={},
        )
        update = await _service(repositories)._fact_adapter(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": [
                        {"id": hypothesis, "description": "Upstream response timed out."}
                    ],
                    "hypothesis_assessments": [
                        {
                            "hypothesisId": hypothesis,
                            "disposition": "supported",
                            "evidenceIds": ["ev-timeout"],
                            "reasonCode": "upstream_response_timed_out",
                            "assessmentSource": "llm_adjudicated",
                            "hasHighQualityConflict": False,
                            "transitions": [],
                        }
                    ],
                    "diagnostic_facts": [],
                    "plan": [],
                    "current_plan_step": {
                        "id": "probe",
                        "tool": "ProbeUpstreamHealth",
                        "arguments": {},
                        "purpose": "Probe the slow upstream endpoint.",
                        "testsHypotheses": [hypothesis],
                        "causalIntent": "mechanism",
                        "evidenceRules": [],
                    },
                    "current_evidence_id": "ev-probe",
                    "current_evidence_summary": (
                        "The upstream first byte exceeds the gateway deadline."
                    ),
                    "current_tool_output": {
                        "tcpConnect": "success",
                        "firstByteMs": 1500,
                        "gatewayReadDeadlineMs": 750,
                    },
                },
            )
        )
    finally:
        await engine.dispose()

    observations = cast(list[dict[str, object]], update["observation_decisions"])
    assert [item["causalRole"] for item in observations] == [
        "mechanism",
        "trigger",
    ]
    assert observations[0]["supports"] == []
    assert observations[1]["supports"] == [hypothesis]
    assert observations[1]["evidenceIds"] == ["ev-probe"]
    assert observations[1]["causalRoleOrigin"] == "coverage_repair"


@pytest.mark.asyncio
async def test_v4_sufficiency_requires_a_buildable_causal_decision(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-causal-sufficiency",
            status="running",
            query="Inspect an unavailable upstream.",
            input_payload={},
        )
        update = await _service(repositories)._sufficiency_gate_v4(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "hypothesis_assessments": [
                        {
                            "hypothesisId": "upstream_process_down",
                            "disposition": "supported",
                            "evidenceIds": ["ev-container"],
                            "reasonCode": "upstream_process_not_running",
                            "assessmentSource": "deterministic",
                            "hasHighQualityConflict": False,
                            "transitions": [],
                        },
                        {
                            "hypothesisId": "upstream_port_mismatch",
                            "disposition": "refuted",
                            "evidenceIds": ["ev-nginx"],
                            "reasonCode": "configured_route_port_matches_service",
                            "assessmentSource": "deterministic",
                            "hasHighQualityConflict": False,
                            "transitions": [],
                        },
                    ],
                    "observation_decisions": [
                        {
                            "supports": ["upstream_process_down"],
                            "refutes": [],
                            "evidenceIds": ["ev-container"],
                            "causalRole": "mechanism",
                            "summary": "The upstream process is not running.",
                        },
                        {
                            "supports": [],
                            "refutes": ["upstream_port_mismatch"],
                            "evidenceIds": ["ev-nginx"],
                            "causalRole": "context",
                            "summary": "The configured upstream port matches.",
                        },
                    ],
                    "plan": [],
                    "plan_index": 0,
                    "executor_attempt_count": 2,
                    "max_total_steps": 6,
                    "replan_count": 0,
                    "max_replans": 1,
                },
            )
        )
    finally:
        await engine.dispose()

    sufficiency = cast(dict[str, object], update["evidence_sufficiency"])
    assert sufficiency["status"] == "insufficient"
    assert sufficiency["decisionReady"] is False
    assert sufficiency["independentPositiveEvidenceCount"] == 1
    assert sufficiency["missingCausalRoles"] == ["trigger", "impact"]
    assert update["next_route"] == "replanner"


@pytest.mark.asyncio
async def test_v4_sufficiency_accepts_a_buildable_differential_decision(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-differential-sufficiency",
            status="running",
            query="Inspect an unavailable upstream.",
            input_payload={},
        )
        update = await _service(repositories)._sufficiency_gate_v4(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "public_hypotheses": [
                        {"id": "process_down", "description": "The process stopped."},
                        {"id": "port_mismatch", "description": "The port is wrong."},
                    ],
                    "decision_vocabulary": {
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
                    "hypothesis_assessments": [
                        {
                            "hypothesisId": "process_down",
                            "disposition": "supported",
                            "evidenceIds": ["ev-container"],
                            "reasonCode": "process_not_running",
                            "assessmentSource": "deterministic",
                            "hasHighQualityConflict": False,
                            "transitions": [],
                        },
                        {
                            "hypothesisId": "port_mismatch",
                            "disposition": "refuted",
                            "evidenceIds": ["ev-container", "ev-nginx"],
                            "reasonCode": "route_port_matches",
                            "assessmentSource": "deterministic",
                            "hasHighQualityConflict": False,
                            "transitions": [],
                        },
                    ],
                    "observation_decisions": [
                        {
                            "supports": ["process_down"],
                            "refutes": [],
                            "evidenceIds": ["ev-container"],
                            "causalRole": "trigger",
                            "summary": "The upstream process exited.",
                        },
                        {
                            "supports": ["process_down"],
                            "refutes": ["port_mismatch"],
                            "evidenceIds": ["ev-container", "ev-nginx"],
                            "causalRole": "mechanism",
                            "summary": "Nginx reached the route but the connection was refused.",
                        },
                    ],
                    "plan": [],
                    "plan_index": 0,
                    "executor_attempt_count": 2,
                    "max_total_steps": 6,
                    "replan_count": 0,
                    "max_replans": 1,
                },
            )
        )
    finally:
        await engine.dispose()

    sufficiency = cast(dict[str, object], update["evidence_sufficiency"])
    assert sufficiency["status"] == "sufficient"
    assert sufficiency["decisionReady"] is True
    assert sufficiency["independentPositiveEvidenceCount"] == 2
    assert update["next_route"] == "decision"


@pytest.mark.asyncio
async def test_v4_decision_and_validator_use_dispositions_without_model_calls(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-deterministic-decision",
            status="running",
            query="Inspect deadlock.",
            input_payload={},
        )
        service = _service(repositories)
        assessments = [
            {
                "hypothesisId": "postgres_deadlock",
                "disposition": "supported",
                "evidenceIds": ["ev-trigger", "ev-mechanism", "ev-impact"],
                "reasonCode": "public_evidence_supports_deadlock",
                "assessmentSource": "deterministic",
                "hasHighQualityConflict": False,
                "transitions": [],
            },
            {
                "hypothesisId": "postgres_slow_query",
                "disposition": "refuted",
                "evidenceIds": ["ev-mechanism"],
                "reasonCode": "wait_cycle_refutes_slow_query",
                "assessmentSource": "deterministic",
                "hasHighQualityConflict": False,
                "transitions": [],
            },
        ]
        observations = [
            {
                "supports": ["postgres_deadlock"],
                "refutes": [],
                "evidenceIds": ["ev-trigger"],
                "causalRole": "trigger",
                "summary": "Transactions acquired resources in opposite order.",
            },
            {
                "supports": ["postgres_deadlock"],
                "refutes": ["postgres_slow_query"],
                "evidenceIds": ["ev-mechanism"],
                "causalRole": "mechanism",
                "summary": "The wait graph contained a two-session cycle.",
            },
            {
                "supports": ["postgres_deadlock"],
                "refutes": [],
                "evidenceIds": ["ev-impact"],
                "causalRole": "impact",
                "summary": "PostgreSQL aborted one transaction with SQLSTATE 40P01.",
            },
        ]
        base_state: dict[str, object] = {
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "public_hypotheses": [
                {"id": "postgres_deadlock", "description": "A wait cycle formed."},
                {"id": "postgres_slow_query", "description": "A query was slow."},
            ],
            "hypothesis_assessments": assessments,
            # Deliberately wrong legacy state: v4 must ignore it and project dispositions.
            "hypothesis_states": [
                {
                    "id": "postgres_deadlock",
                    "status": "open",
                    "confidence": 0.0,
                    "evidenceIds": [],
                }
            ],
            "observation_decisions": observations,
            "decision_vocabulary": {
                "labelsByHypothesis": {
                    "postgres_deadlock": {
                        "component": "order-service",
                        "mechanism": "opposite_order_transaction_deadlock",
                    }
                }
            },
            "evidence_ids": ["ev-trigger", "ev-mechanism", "ev-impact"],
        }

        decision_update = await service._decision_v4(cast(Any, base_state))  # pyright: ignore[reportPrivateUsage]
        validator_update = await service._deterministic_validator_v4(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    **base_state,
                    "root_cause_decision": decision_update["root_cause_decision"],
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert decision_update["root_cause_decision"] is not None
    assert cast(dict[str, object], validator_update["decision_validation"])["status"] == (
        "valid"
    )
    assert [step.payload["decisionAttempts"] for step in steps if step.phase == "decision"] == [
        0
    ]
    validation = next(step for step in steps if step.phase == "decision_validator")
    assert validation.payload["validationOrigin"] == "deterministic"
    assert validation.payload["validationAttempts"] == 0
    check_codes = {
        item["code"]
        for item in cast(list[dict[str, object]], validation.payload["deterministicChecks"])
    }
    assert "no_unresolved_active_competitor" in check_codes
    assert "closed_alternatives_are_grounded" in check_codes
    assert "no_open_competitor" not in check_codes


@pytest.mark.asyncio
async def test_failed_semantic_validator_preserves_diagnosis_but_forces_manual_review(
    migrated_database_url: str,
) -> None:
    class FailingModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            raise TimeoutError

    class Provider:
        def create_chat_model(self) -> FailingModel:
            return FailingModel()

    deadlines = ExecutionDeadlines.start()
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-semantic-validator-fail-closed",
            status="running",
            query="Validate a risky diagnosis.",
            input_payload={},
        )
        service = _service(repositories, Provider())
        update = await service._llm_validator_v4(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "workflow_version": "evidence-driven-v4",
                    "model_call_count": 2,
                    "started_at": deadlines.started_at.isoformat(),
                    "soft_deadline_at": deadlines.soft_deadline_at.isoformat(),
                    "hard_deadline_at": deadlines.hard_deadline_at.isoformat(),
                    "root_cause_decision": {
                        "component": "order-service",
                        "mechanism": "transaction_deadlock",
                        "trigger": "Transactions acquired rows in opposite order.",
                        "causalChain": [
                            "Transactions acquired rows in opposite order.",
                            "The wait graph formed a cycle.",
                            "PostgreSQL aborted one transaction.",
                        ],
                        "evidenceIds": ["ev-1", "ev-2", "ev-3"],
                        "confidence": 0.95,
                    },
                    "evidence_ids": ["ev-1", "ev-2", "ev-3"],
                    "observation_decisions": [],
                    "decision_validation": {
                        "status": "valid",
                        "validationOrigin": "deterministic",
                    },
                    "validator_routing": {
                        "validationReasonCodes": ["elevated_recovery_risk"]
                    },
                    "tool_definitions": (),
                },
            )
        )
        policy = await service._policy_gate(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "recovery_plan": update["recovery_plan"],
                },
            )
        )
    finally:
        await engine.dispose()

    validation = cast(dict[str, object], update["decision_validation"])
    recovery = cast(dict[str, object], update["recovery_plan"])
    audit = cast(list[dict[str, object]], update["model_call_audits"])
    assert validation["status"] == "valid"
    assert validation["validationOrigin"] == "llm_failed"
    assert recovery["mode"] == "manual_review"
    assert audit[-1]["safeErrorCode"] == "timeout"
    assert cast(dict[str, object], policy["recovery_policy"])[
        "executionPermitted"
    ] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovery_mode", "expected_route", "expected_reason_codes"),
    [
        ("proposal_only", "policy_gate", []),
        ("external_policy_required", "llm_validator", ["execution_requested"]),
    ],
)
async def test_validator_router_distinguishes_proposal_from_execution_request(
    migrated_database_url: str,
    recovery_mode: str,
    expected_route: str,
    expected_reason_codes: list[str],
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=f"v4-validator-router-{recovery_mode}",
            status="running",
            query="Route a deterministic recovery decision.",
            input_payload={},
        )
        service = _service(repositories)
        update = await service._validator_router_v4(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "decision_validation": {"status": "valid"},
                    "root_cause_decision": {
                        "component": "live-eval-upstream",
                        "mechanism": "upstream_response_timeout",
                        "trigger": "The gateway read deadline elapsed.",
                        "causalChain": ["The upstream response exceeded the deadline."],
                        "evidenceIds": ["ev-timeline"],
                        "confidence": 1.0,
                    },
                    "recovery_plan": {
                        "mode": recovery_mode,
                        "risk": "L0",
                    },
                    "hypothesis_assessments": [],
                },
            )
        )
    finally:
        await engine.dispose()

    routing = cast(dict[str, object], update["validator_routing"])
    assert update["next_route"] == expected_route
    assert routing["validationRequired"] is (expected_route == "llm_validator")
    assert routing["validationReasonCodes"] == expected_reason_codes
    assert routing["validationSkipReason"] == (
        "no_semantic_risk" if expected_route == "policy_gate" else None
    )


@pytest.mark.asyncio
async def test_invalid_recovery_model_falls_back_to_schema_valid_proposal_only(
    migrated_database_url: str,
) -> None:
    class InvalidModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            return "{}"

    class Provider:
        def create_chat_model(self) -> InvalidModel:
            return InvalidModel()

    class RecordingMcpClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append((name, arguments))
            return {"accepted": True, "humanApprovalRequired": True}

    definition = McpToolDefinition(
        "ProposeNginxTimeoutMitigation",
        "Record a side-effect-free Nginx mitigation proposal.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1},
                "risk": {"type": "string", "minLength": 1},
                "rollback": {"type": "string", "minLength": 1},
                "verificationSteps": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 2,
                },
                "humanApprovalRequired": {"type": "boolean", "const": True},
            },
            "required": [
                "target",
                "risk",
                "rollback",
                "verificationSteps",
                "humanApprovalRequired",
            ],
            "additionalProperties": False,
        },
    )
    mcp = RecordingMcpClient()
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-deterministic-proposal-fallback",
            status="running",
            query="Plan a reviewed Nginx mitigation.",
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, Provider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=cast(Any, mcp),
            cls_region="unused",
            cls_topic_id="unused",
            tool_policies={"ProposeNginxTimeoutMitigation": "proposal_only"},
        )
        state = cast(
            Any,
            {
                "owner_user_id": task.owner_user_id,
                "task_id": task.id,
                "workflow_version": "evidence-driven-v4",
                "root_cause_decision": {
                    "component": "live-eval-upstream",
                    "mechanism": "upstream_response_exceeded_proxy_read_timeout",
                    "trigger": "The incident log records an upstream timeout.",
                    "causalChain": [
                        "The upstream connection succeeded.",
                        "The gateway read deadline elapsed.",
                    ],
                    "evidenceIds": ["ev-timeline", "ev-cls"],
                    "confidence": 1.0,
                },
                "evidence_ids": ["ev-timeline", "ev-cls"],
                "decision_validation": {
                    "status": "valid",
                    "validationOrigin": "deterministic",
                },
                "tool_definitions": (definition,),
            },
        )
        recovery = await service._recovery_planner(  # pyright: ignore[reportPrivateUsage]
            state
        )
        policy = await service._policy_gate(  # pyright: ignore[reportPrivateUsage]
            cast(Any, {**state, "recovery_plan": recovery["recovery_plan"]})
        )
    finally:
        await engine.dispose()

    plan = cast(dict[str, object], recovery["recovery_plan"])
    assert plan["mode"] == "proposal_only"
    assert plan["tool"] == "ProposeNginxTimeoutMitigation"
    assert cast(dict[str, object], plan["arguments"])["target"] == (
        "live-eval-upstream"
    )
    policy_payload = cast(dict[str, object], policy["recovery_policy"])
    assert policy_payload["authorizationCode"] == "proposal_recorded"
    assert policy_payload["executionPermitted"] is False
    assert len(mcp.calls) == 1


@pytest.mark.asyncio
async def test_exhausted_budget_prevents_recovery_model_call_and_executes_nothing(
    migrated_database_url: str,
) -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, prompt: object) -> str:
            del prompt
            self.calls += 1
            return "{}"

    class Provider:
        def __init__(self) -> None:
            self.model = CountingModel()

        def create_chat_model(self) -> CountingModel:
            return self.model

    provider = Provider()
    deadlines = ExecutionDeadlines.start()
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="v4-recovery-budget-exhausted",
            status="running",
            query="Plan a safe recovery.",
            input_payload={},
        )
        service = _service(repositories, provider)
        update = await service._recovery_planner(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "workflow_version": "evidence-driven-v4",
                    "model_call_count": 8,
                    "started_at": deadlines.started_at.isoformat(),
                    "soft_deadline_at": deadlines.soft_deadline_at.isoformat(),
                    "hard_deadline_at": deadlines.hard_deadline_at.isoformat(),
                    "root_cause_decision": {
                        "component": "order-service",
                        "mechanism": "transaction_deadlock",
                        "trigger": "Transactions acquired rows in opposite order.",
                        "causalChain": [
                            "Transactions acquired rows in opposite order.",
                            "The wait graph formed a cycle.",
                        ],
                        "evidenceIds": ["ev-1", "ev-2"],
                        "confidence": 0.95,
                    },
                    "evidence_ids": ["ev-1", "ev-2"],
                    "decision_validation": {
                        "status": "valid",
                        "validationOrigin": "deterministic",
                    },
                    "tool_definitions": (),
                },
            )
        )
        policy = await service._policy_gate(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "recovery_plan": update["recovery_plan"],
                },
            )
        )
    finally:
        await engine.dispose()

    assert provider.model.calls == 0
    assert update["model_call_count"] == 8
    assert cast(list[dict[str, object]], update["model_call_audits"])[-1][
        "safeErrorCode"
    ] == "model_call_budget_exhausted"
    assert cast(dict[str, object], policy["recovery_policy"])[
        "executionPermitted"
    ] is False
