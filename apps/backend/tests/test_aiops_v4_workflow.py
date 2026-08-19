from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import pytest

from super_ai.aiops import AiopsDiagnosticService
from super_ai.aiops.diagnostics import (
    _initial_hypothesis_assessments,  # pyright: ignore[reportPrivateUsage]
)
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
