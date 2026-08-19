from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from super_ai.aiops import AiopsDiagnosticService
from super_ai.aiops import diagnostics as diagnostics_module
from super_ai.aiops.adjudication import DiagnosticFact, HypothesisAssessment
from super_ai.aiops.diagnostics import (
    _initial_hypothesis_assessments,  # pyright: ignore[reportPrivateUsage]
    _project_adjudicated_observations,  # pyright: ignore[reportPrivateUsage]
)
from super_ai.aiops.model_budget import ExecutionDeadlines
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.repositories import JsonDict
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalToolInput, KnowledgeRetrievalToolResult


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


class UnusedMcpClient:
    pass


def _service(repositories: object, provider: object = object()) -> AiopsDiagnosticService:
    return AiopsDiagnosticService(
        repositories=cast(Any, repositories),
        llm_provider=cast(Any, provider),
        retrieval_tool=EmptyRetrieval(),
        mcp_client=cast(Any, UnusedMcpClient()),
        cls_region="unused",
        cls_topic_id="unused",
    )


def test_v4_graph_removes_per_observation_model_nodes() -> None:
    graph = _service(object())._build_graph(  # pyright: ignore[reportPrivateUsage]
        workflow_version="evidence-driven-v4"
    )

    nodes = set(graph.get_graph().nodes)

    assert {"fact_adapter", "hypothesis_adjudicator", "deterministic_validator"} <= nodes
    assert "evidence_evaluator" not in nodes
    assert "decision_validator" not in nodes
    assert {"validator_router", "llm_validator", "manual_review"} <= nodes


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
        ],
    )

    derived = [
        item
        for item in projected
        if item.get("causalRoleOrigin") == "coverage_repair"
    ]
    assert [item["causalRole"] for item in derived] == ["trigger", "impact"]
    assert [item["evidenceIds"] for item in derived] == [["ev-info"], ["ev-info"]]
    assert all(item["supports"] == ["redis_maxclients"] for item in derived)


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
async def test_fact_adapter_records_cross_evidence_differential_support(
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
    assert observation["supports"] == ["upstream_process_down"]
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
        for step, (evidence_id, summary, output) in zip(plan, outputs, strict=True):
            update = await service._fact_adapter(  # pyright: ignore[reportPrivateUsage]
                cast(
                    Any,
                    {
                        **state,
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
    assert all(item["supports"] == [hypothesis] for item in observations)
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
