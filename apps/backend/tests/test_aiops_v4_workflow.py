from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from super_ai.aiops import AiopsDiagnosticService
from super_ai.aiops.diagnostics import (
    _initial_hypothesis_assessments,  # pyright: ignore[reportPrivateUsage]
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
