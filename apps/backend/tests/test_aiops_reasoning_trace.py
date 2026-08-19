import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from super_ai.aiops import AiopsDiagnosticService
from super_ai.aiops.adjudication import HypothesisAssessment
from super_ai.aiops.diagnostics import (
    _fallback_evidence_sufficiency,  # pyright: ignore[reportPrivateUsage]
    _next_open_hypothesis_step_index,  # pyright: ignore[reportPrivateUsage]
    _normalize_grounded_decision,  # pyright: ignore[reportPrivateUsage]
    _project_evidence_sufficiency,  # pyright: ignore[reportPrivateUsage]
    _step_fingerprint,  # pyright: ignore[reportPrivateUsage]
    _supporting_decision_evidence_ids,  # pyright: ignore[reportPrivateUsage]
    _update_hypothesis_states,  # pyright: ignore[reportPrivateUsage]
    _validator_chat_model,  # pyright: ignore[reportPrivateUsage]
    _validator_model_name,  # pyright: ignore[reportPrivateUsage]
    _validator_structured_output_method,  # pyright: ignore[reportPrivateUsage]
    build_grounded_fallback_decision,
    normalize_tool_plan_steps,
)
from super_ai.aiops.reasoning import (
    EvidenceSufficiencyDecision,
    RootCauseDecision,
    normalize_root_cause_decision,
    parse_evidence_sufficiency,
    parse_observation_decision,
    parse_plan,
    parse_recovery_plan,
    parse_root_cause_decision,
    parse_root_cause_validation,
    project_hypothesis_assessment,
)
from super_ai.evaluation import SnapshotMcpClient, load_public_scenario
from super_ai.evaluation.runner import build_application_diagnostic_input
from super_ai.llm import LlmProvider
from super_ai.mcp.tool_arguments import constrain_tool_definitions, tool_step_fingerprint
from super_ai.mcp_client import McpToolDefinition
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.repositories import DiagnosticStepRecord, JsonDict
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalToolInput, KnowledgeRetrievalToolResult

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"


def test_causally_inactive_projects_to_legacy_refuted_without_changing_v4_state() -> None:
    assessment = HypothesisAssessment(
        hypothesis_id="port_mismatch",
        disposition="causally_inactive",
        evidence_ids=("e-route",),
        reason_code="route_not_on_failure_path",
        assessment_source="deterministic",
    )

    projected = project_hypothesis_assessment(assessment)

    assert projected.status == "refuted"
    assert projected.confidence == 0.1
    assert assessment.disposition == "causally_inactive"


def test_plan_parser_instantiates_a_trusted_evidence_rule_template() -> None:
    plan = parse_plan(
        json.dumps(
            {
                "steps": [
                    {
                        "id": "inspect-nginx",
                        "tool": "InspectNginx",
                        "arguments": {"route": "checkout"},
                        "purpose": "Compare the upstream and service ports.",
                        "testsHypotheses": ["upstream_port_mismatch"],
                        "causalIntent": "mechanism",
                        "evidenceRules": [
                            {
                                "templateId": "nginx_upstream_port_matches_container_port",
                                "hypothesisId": "upstream_port_mismatch",
                                "parameters": {
                                    "nginxFact": "InspectNginx.upstreamPort",
                                    "containerFact": "InspectContainer.configuredPorts",
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        available_tools={"InspectNginx"},
        known_hypotheses={"upstream_port_mismatch"},
        causal_capabilities={"InspectNginx": {"mechanism"}},
    )

    assert len(plan[0].evidence_rules) == 1
    rule = plan[0].evidence_rules[0]
    assert rule.hypothesis_id == "upstream_port_mismatch"
    assert rule.when_true == "refuted"
    assert rule.reason_code == "configured_route_port_matches_service"


def test_plan_parser_drops_model_defined_disposition_rule() -> None:
    plan = parse_plan(
        json.dumps(
            {
                "steps": [
                    {
                        "id": "inspect-container",
                        "tool": "InspectContainer",
                        "arguments": {"service": "checkout-service"},
                        "purpose": "Inspect the process.",
                        "testsHypotheses": ["upstream_port_mismatch"],
                        "causalIntent": "mechanism",
                        "evidenceRules": [
                            {
                                "hypothesisId": "upstream_port_mismatch",
                                "predicate": {
                                    "leftFact": "InspectContainer.status",
                                    "operator": "eq",
                                    "expected": "exited",
                                },
                                "whenTrue": "refuted",
                                "reasonCode": "model_invented_causal_mapping",
                            }
                        ],
                    }
                ]
            }
        ),
        available_tools={"InspectContainer"},
        known_hypotheses={"upstream_port_mismatch"},
        causal_capabilities={"InspectContainer": {"mechanism"}},
    )

    assert plan[0].evidence_rules == ()


def test_trusted_template_cannot_close_an_unauthorized_hypothesis() -> None:
    plan = parse_plan(
        json.dumps(
            {
                "steps": [
                    {
                        "id": "inspect-nginx",
                        "tool": "InspectNginx",
                        "arguments": {"route": "checkout"},
                        "purpose": "Inspect the upstream route.",
                        "testsHypotheses": ["upstream_process_down"],
                        "causalIntent": "mechanism",
                        "evidenceRules": [
                            {
                                "templateId": "nginx_upstream_port_matches_container_port",
                                "hypothesisId": "upstream_process_down",
                                "parameters": {
                                    "nginxFact": "InspectNginx.upstreamPort",
                                    "containerFact": "InspectContainer.configuredPorts",
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        available_tools={"InspectNginx"},
        known_hypotheses={"upstream_process_down"},
        causal_capabilities={"InspectNginx": {"mechanism"}},
    )

    assert plan[0].evidence_rules == ()


def _model_sufficiency() -> EvidenceSufficiencyDecision:
    return EvidenceSufficiencyDecision(
        status="sufficient",
        evidence_ids=("ev-cycle",),
        supported_hypotheses=("postgres_deadlock",),
        refuted_hypotheses=("postgres_lock_wait", "postgres_slow_query"),
        unresolved_hypotheses=(),
        missing_evidence=(),
        recommended_tools=(),
        summary="The model claims every competitor is closed.",
    )


def test_authoritative_sufficiency_projects_public_persisted_states() -> None:
    public_hypotheses: list[JsonDict] = [
        {"id": "postgres_deadlock"},
        {"id": "postgres_lock_wait"},
        {"id": "postgres_slow_query"},
    ]
    projected = _project_evidence_sufficiency(
        model_decision=_model_sufficiency(),
        public_hypotheses=public_hypotheses,
        hypothesis_states=[
            {"id": "postgres_deadlock", "status": "supported"},
            {"id": "postgres_lock_wait", "status": "open"},
            {"id": "postgres_slow_query", "status": "open"},
        ],
        evidence_ids=("ev-cycle",),
    )

    assert projected.status == "insufficient"
    assert projected.supported_hypotheses == ("postgres_deadlock",)
    assert projected.refuted_hypotheses == ()
    assert projected.unresolved_hypotheses == (
        "postgres_lock_wait",
        "postgres_slow_query",
    )

    complete = _project_evidence_sufficiency(
        model_decision=_model_sufficiency(),
        public_hypotheses=public_hypotheses,
        hypothesis_states=[
            {"id": "postgres_deadlock", "status": "supported"},
            {"id": "postgres_lock_wait", "status": "refuted"},
            {"id": "postgres_slow_query", "status": "refuted"},
        ],
        evidence_ids=("ev-cycle",),
    )
    assert complete.status == "sufficient"


@pytest.mark.parametrize(
    "hypothesis_states",
    [
        [{"id": "postgres_deadlock", "status": "supported"}],
        [
            {"id": "postgres_deadlock", "status": "supported"},
            {"id": "postgres_deadlock", "status": "refuted"},
            {"id": "postgres_lock_wait", "status": "refuted"},
        ],
        [
            {"id": "postgres_deadlock", "status": "supported"},
            {"id": "postgres_lock_wait", "status": "refuted"},
            {"id": "private_oracle", "status": "refuted"},
        ],
    ],
)
def test_authoritative_sufficiency_fails_closed_for_incomplete_or_invalid_state(
    hypothesis_states: list[JsonDict],
) -> None:
    projected = _project_evidence_sufficiency(
        model_decision=_model_sufficiency(),
        public_hypotheses=[
            {"id": "postgres_deadlock"},
            {"id": "postgres_lock_wait"},
        ],
        hypothesis_states=hypothesis_states,
        evidence_ids=("ev-cycle",),
    )

    assert projected.status == "insufficient"
    assert "private_oracle" not in (
        *projected.supported_hypotheses,
        *projected.refuted_hypotheses,
        *projected.unresolved_hypotheses,
    )


def test_sufficiency_model_failure_uses_authoritative_projection() -> None:
    decision = _fallback_evidence_sufficiency(
        public_hypotheses=[
            {"id": "postgres_deadlock"},
            {"id": "postgres_lock_wait"},
        ],
        hypothesis_states=[
            {"id": "postgres_deadlock", "status": "supported"},
            {"id": "postgres_lock_wait", "status": "refuted"},
        ],
        evidence_ids=("ev-cycle",),
    )

    assert decision.status == "sufficient"
    assert decision.supported_hypotheses == ("postgres_deadlock",)
    assert decision.refuted_hypotheses == ("postgres_lock_wait",)


def _refinement_step(
    step_id: str,
    tool: str,
    hypotheses: list[str],
) -> dict[str, object]:
    intents = {
        "GetDatabaseMetrics": "context",
        "InspectPostgresErrors": "impact",
        "InspectPostgresWaitGraph": "mechanism",
        "InspectTransactionResourceOrder": "trigger",
    }
    return {
        "id": step_id,
        "tool": tool,
        "arguments": {"scope": step_id},
        "purpose": f"Inspect {step_id}.",
        "testsHypotheses": hypotheses,
        "causalIntent": intents.get(tool, "context"),
    }


def test_open_hypothesis_route_selects_only_unexecuted_matching_step() -> None:
    metrics = _refinement_step(
        "metrics", "GetDatabaseMetrics", ["postgres_slow_query"]
    )
    resource_order = _refinement_step(
        "resource-order",
        "InspectTransactionResourceOrder",
        ["postgres_deadlock"],
    )

    assert _next_open_hypothesis_step_index(
        plan=[resource_order, metrics],
        plan_index=0,
        open_hypothesis_ids=("postgres_slow_query",),
        executed_fingerprints=(),
    ) == 1
    assert (
        _next_open_hypothesis_step_index(
            plan=[resource_order, metrics],
            plan_index=0,
            open_hypothesis_ids=("postgres_slow_query",),
            executed_fingerprints=(_step_fingerprint(metrics),),
        )
        is None
    )
    assert (
        _next_open_hypothesis_step_index(
            plan=[resource_order],
            plan_index=0,
            open_hypothesis_ids=("postgres_slow_query",),
            executed_fingerprints=(),
        )
        is None
    )
def _one_item_deadlock_decision(
    *,
    component: str = "order-service",
    mechanism: str = "opposite_order_transaction_deadlock",
    evidence_ids: tuple[str, ...] = ("ev-error", "ev-cycle", "ev-order"),
    causal_chain: tuple[str, ...] = ("One combined narrative.",),
) -> RootCauseDecision:
    return RootCauseDecision(
        component=component,
        mechanism=mechanism,
        trigger="Transactions acquired resources in opposite orders.",
        causal_chain=causal_chain,
        evidence_ids=evidence_ids,
        confidence=0.97,
    )


def _normalize_deadlock_decision(
    decision: RootCauseDecision,
    *,
    hypothesis_states: list[dict[str, object]] | None = None,
    observation_decisions: list[dict[str, object]] | None = None,
) -> RootCauseDecision | None:
    return _normalize_grounded_decision(
        decision,
        available_evidence_ids={"ev-error", "ev-cycle", "ev-order"},
        public_hypotheses=[
            {
                "id": "postgres_deadlock",
                "description": "Concurrent transactions formed a cycle.",
            },
            {
                "id": "postgres_lock_wait",
                "description": "A long transaction blocks valid work.",
            },
        ],
        hypothesis_states=hypothesis_states
        or [
            {
                "id": "postgres_deadlock",
                "status": "supported",
                "confidence": 1.0,
                "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
            }
        ],
        observation_decisions=observation_decisions
        or [
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-error"],
                "causalRole": "impact",
                "summary": "PostgreSQL emitted SQLSTATE 40P01.",
            },
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-cycle"],
                "causalRole": "mechanism",
                "summary": "The wait graph contained a two-session cycle.",
            },
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-order"],
                "causalRole": "trigger",
                "summary": "Transactions acquired shared resources in opposite order.",
            },
        ],
        decision_vocabulary={
            "labelsByHypothesis": {
                "postgres_deadlock": {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                },
                "postgres_lock_wait": {
                    "component": "order-service",
                    "mechanism": "long_transaction_lock_blocking",
                },
            }
        },
    )


def test_grounded_normalization_replaces_only_expression_fields() -> None:
    original = _one_item_deadlock_decision()

    repaired = _normalize_deadlock_decision(original)

    assert repaired is not None
    assert repaired.causal_chain == (
        "Transactions acquired shared resources in opposite order.",
        "The wait graph contained a two-session cycle.",
        "PostgreSQL emitted SQLSTATE 40P01.",
    )
    assert repaired.component == original.component
    assert repaired.mechanism == original.mechanism
    assert repaired.trigger == "Transactions acquired shared resources in opposite order."
    assert repaired.evidence_ids == original.evidence_ids
    assert repaired.confidence == original.confidence


def test_grounded_normalization_fails_closed_for_unsafe_inputs() -> None:
    assert _normalize_deadlock_decision(
        _one_item_deadlock_decision(component="postgres")
    ) is None
    assert _normalize_deadlock_decision(
        _one_item_deadlock_decision(),
        hypothesis_states=[
            {
                "id": "postgres_deadlock",
                "status": "supported",
                "confidence": 1.0,
                "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
            },
            {
                "id": "postgres_lock_wait",
                "status": "supported",
                "confidence": 0.95,
                "evidenceIds": ["ev-error", "ev-cycle"],
            },
        ],
    ) is None
    assert _normalize_deadlock_decision(
        _one_item_deadlock_decision(),
        observation_decisions=[
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-error"],
                "summary": "Only one linked summary.",
            }
        ],
    ) is None
    assert _normalize_deadlock_decision(
        _one_item_deadlock_decision(evidence_ids=("ev-error", "ev-cycle"))
    ) is None
    assert _normalize_deadlock_decision(
        _one_item_deadlock_decision(
            component="postgres",
            mechanism="deadlock",
        )
    ) is None
    assert _normalize_deadlock_decision(
        _one_item_deadlock_decision(),
        hypothesis_states=[
            {
                "id": "postgres_deadlock",
                "status": "supported",
                "confidence": 1.0,
                "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
            },
            {
                "id": "postgres_lock_wait",
                "status": "open",
                "confidence": 0.5,
                "evidenceIds": [],
            },
        ],
    ) is None


def test_grounded_fallback_deduplicates_and_preserves_terminal_impact() -> None:
    observations: list[JsonDict] = [
        {
            "supports": ["postgres_deadlock"],
            "evidenceIds": ["ev-order"],
            "causalRole": "trigger",
            "summary": "Transactions acquire resources in opposite order.",
        },
        *[
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-cycle"],
                "causalRole": "mechanism",
                "summary": f"Mechanism fact {index} confirms the wait cycle.",
            }
            for index in range(1, 7)
        ],
        {
            "supports": ["postgres_deadlock"],
            "evidenceIds": ["ev-cycle"],
            "causalRole": "mechanism",
            "summary": "Mechanism fact 1 confirms the wait cycle.",
        },
        {
            "supports": ["postgres_deadlock"],
            "evidenceIds": ["ev-error"],
            "causalRole": "impact",
            "summary": "PostgreSQL aborts one transaction with SQLSTATE 40P01.",
        },
    ]

    decision = build_grounded_fallback_decision(
        public_hypotheses=[
            {"id": "postgres_deadlock", "description": "A cyclic dependency exists."}
        ],
        hypothesis_states=[
            {
                "id": "postgres_deadlock",
                "status": "supported",
                "confidence": 0.99,
                "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
            }
        ],
        observation_decisions=observations,
        decision_vocabulary={
            "labelsByHypothesis": {
                "postgres_deadlock": {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                }
            }
        },
    )

    assert decision is not None
    assert len(decision.causal_chain) == 6
    assert decision.causal_chain[0] == "Transactions acquire resources in opposite order."
    assert decision.causal_chain[-1] == (
        "PostgreSQL aborts one transaction with SQLSTATE 40P01."
    )
    assert len(set(decision.causal_chain)) == len(decision.causal_chain)


def test_grounded_fallback_rejects_ambiguous_triggers() -> None:
    decision = build_grounded_fallback_decision(
        public_hypotheses=[{"id": "postgres_deadlock", "description": "Deadlock."}],
        hypothesis_states=[
            {
                "id": "postgres_deadlock",
                "status": "supported",
                "confidence": 0.99,
                "evidenceIds": ["ev-order", "ev-deploy", "ev-cycle"],
            }
        ],
        observation_decisions=[
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-order"],
                "causalRole": "trigger",
                "summary": "Transactions acquired resources in opposite order.",
            },
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-deploy"],
                "causalRole": "trigger",
                "summary": "A deployment changed the transaction order.",
            },
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["ev-cycle"],
                "causalRole": "mechanism",
                "summary": "The wait graph formed a cycle.",
            },
        ],
        decision_vocabulary={
            "labelsByHypothesis": {
                "postgres_deadlock": {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                }
            }
        },
    )

    assert decision is None


@pytest.mark.asyncio
async def test_plan_normalizer_binds_snapshot_scope_and_deduplicates() -> None:
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / "snapshot" / "tool_responses.yaml"
    )
    definitions = await snapshot.discover_tools()
    plan: list[dict[str, object]] = [
        {
            "id": "errors-30",
            "tool": "InspectPostgresErrors",
            "arguments": {"service": "order-service", "windowMinutes": 30},
            "purpose": "Inspect deadlock errors.",
            "testsHypotheses": ["opposite_order_transaction_deadlock"],
        },
        {
            "id": "errors-60-duplicate",
            "tool": "InspectPostgresErrors",
            "arguments": {"service": "wrong-service", "windowMinutes": 60},
            "purpose": "Repeat the same error inspection.",
            "testsHypotheses": ["opposite_order_transaction_deadlock"],
        },
        {
            "id": "wait-graph",
            "tool": "InspectPostgresWaitGraph",
            "arguments": {"database": "order-service", "windowMinutes": 60},
            "purpose": "Inspect the wait graph.",
            "testsHypotheses": ["opposite_order_transaction_deadlock"],
        },
    ]

    normalized, errors = normalize_tool_plan_steps(
        plan,
        trusted_tool_arguments={},
        tool_argument_contracts=snapshot.tool_argument_contracts,
        tool_definitions=definitions,
    )

    assert errors == []
    assert [step["id"] for step in normalized] == ["errors-30", "wait-graph"]
    assert normalized[0]["arguments"] == {
        "service": "order-service",
        "windowMinutes": 15,
    }
    assert normalized[1]["arguments"] == {
        "database": "agent_py",
        "windowMinutes": 15,
    }


@pytest.mark.asyncio
async def test_plan_normalizer_preserves_valid_variants_and_filters_invalid() -> None:
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-016" / "snapshot" / "tool_responses.yaml"
    )
    definitions = await snapshot.discover_tools()
    plan: list[dict[str, object]] = [
        {
            "id": view,
            "tool": "InspectClientRetryPolicy",
            "arguments": {"client": "wrong-client", "view": view},
            "purpose": "Inspect retry behavior.",
            "testsHypotheses": ["client_retry_amplification"],
        }
        for view in ("effective-policy", "sampled-timeline", "invented-view")
    ]

    normalized, errors = normalize_tool_plan_steps(
        plan,
        trusted_tool_arguments={},
        tool_argument_contracts=snapshot.tool_argument_contracts,
        tool_definitions=definitions,
    )

    assert [
        cast(dict[str, object], step["arguments"])["view"] for step in normalized
    ] == [
        "effective-policy",
        "sampled-timeline",
    ]
    assert [error.code for error in errors] == ["invalid_variant"]


@pytest.mark.asyncio
async def test_executor_rejects_ambiguous_legacy_arguments_before_mcp_audit(
    migrated_database_url: str,
) -> None:
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-016" / "snapshot" / "tool_responses.yaml"
    )
    definitions = constrain_tool_definitions(
        await snapshot.discover_tools(),
        snapshot.tool_argument_contracts,
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-invalid-legacy-arguments",
            status="accepted",
            query="Inspect retry amplification",
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, object()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        result = await service._executor(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "query": task.query,
                    "accessible_knowledge_base_ids": (),
                    "plan": [
                        {
                            "id": "ambiguous-policy-view",
                            "tool": "InspectClientRetryPolicy",
                            "arguments": {"client": "checkout-client"},
                            "purpose": "Inspect retry policy.",
                            "testsHypotheses": [],
                        }
                    ],
                    "plan_index": 0,
                    "executor_attempt_count": 0,
                    "max_total_steps": 6,
                    "executed_step_fingerprints": [],
                    "tool_definitions": tuple(definitions),
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        audits = await cast(Any, repositories.tool_call_audits).list_for_diagnostic_task(
            owner_user_id=task.owner_user_id,
            diagnostic_task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert snapshot.observations == ()
    assert audits == []
    assert result["executor_attempt_count"] == 1
    assert steps[-1].payload == {
        "planStepId": "ambiguous-policy-view",
        "tool": "InspectClientRetryPolicy",
        "errorCategory": "invalid_arguments",
        "contractCode": "ambiguous_variant",
    }


@pytest.mark.asyncio
async def test_executor_duplicate_legacy_step_does_not_consume_attempt_budget(
    migrated_database_url: str,
) -> None:
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-003" / "snapshot" / "tool_responses.yaml"
    )
    step: dict[str, object] = {
        "id": "duplicate-container",
        "tool": "InspectContainer",
        "arguments": {"service": "checkout-service"},
        "purpose": "Repeat a completed inspection.",
        "testsHypotheses": [],
    }
    fingerprint = tool_step_fingerprint(
        "InspectContainer",
        cast(dict[str, object], step["arguments"]),
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-duplicate-legacy-step",
            status="accepted",
            query="Inspect checkout",
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, object()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        result = await service._executor(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "plan": [step],
                    "plan_index": 0,
                    "executor_attempt_count": 2,
                    "max_total_steps": 6,
                    "executed_step_fingerprints": [fingerprint],
                    "tool_definitions": tuple(await snapshot.discover_tools()),
                },
            )
        )
        audits = await cast(Any, repositories.tool_call_audits).list_for_diagnostic_task(
            owner_user_id=task.owner_user_id,
            diagnostic_task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert result["executor_attempt_count"] == 2
    assert result["plan_index"] == 1
    assert snapshot.observations == ()
    assert audits == []


def test_plan_rejects_unknown_tools_and_hypotheses() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        parse_plan(
            '{"steps":[{"id":"x","tool":"Shell","arguments":{},'
            '"purpose":"inspect","testsHypotheses":["process-down"]}]}',
            available_tools={"InspectContainer"},
            known_hypotheses={"process-down"},
        )

    with pytest.raises(ValueError, match="unknown hypothesis"):
        parse_plan(
            '{"steps":[{"id":"x","tool":"InspectContainer","arguments":{},'
            '"purpose":"inspect","testsHypotheses":["invented"],'
            '"causalIntent":"context"}]}',
            available_tools={"InspectContainer"},
            known_hypotheses={"process-down"},
        )


def test_step_fingerprint_ignores_causal_intent_metadata() -> None:
    base: dict[str, object] = {
        "tool": "InspectPostgresWaitGraph",
        "arguments": {"database": "agent_py", "windowMinutes": 15},
    }

    assert _step_fingerprint({**base, "causalIntent": "trigger"}) == (
        _step_fingerprint({**base, "causalIntent": "mechanism"})
    )


@pytest.mark.asyncio
async def test_model_plan_causal_intents_are_minimally_repaired() -> None:
    class StaticChatModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "errors",
                            "tool": "InspectPostgresErrors",
                            "arguments": {
                                "service": "order-service",
                                "windowMinutes": 15,
                            },
                            "purpose": "Inspect database errors.",
                            "testsHypotheses": ["postgres_deadlock"],
                            "causalIntent": "mechanism",
                        },
                        {
                            "id": "graph",
                            "tool": "InspectPostgresWaitGraph",
                            "arguments": {
                                "database": "agent_py",
                                "windowMinutes": 15,
                            },
                            "purpose": "Inspect the wait graph.",
                            "testsHypotheses": ["postgres_deadlock"],
                            "causalIntent": "mechanism",
                        },
                        {
                            "id": "order",
                            "tool": "InspectTransactionResourceOrder",
                            "arguments": {
                                "service": "order-service",
                                "windowMinutes": 15,
                            },
                            "purpose": "Inspect transaction resource order.",
                            "testsHypotheses": ["postgres_deadlock"],
                            "causalIntent": "mechanism",
                        },
                    ]
                }
            )

    class StaticLlmProvider:
        def create_chat_model(self) -> StaticChatModel:
            return StaticChatModel()

    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / "snapshot" / "tool_responses.yaml"
    )
    service = AiopsDiagnosticService(
        repositories=cast(Any, object()),
        llm_provider=cast(Any, StaticLlmProvider()),
        retrieval_tool=cast(Any, object()),
        mcp_client=snapshot,
        cls_region="unused",
        cls_topic_id="unused",
        tool_argument_contracts=snapshot.tool_argument_contracts,
    )

    plan, origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Inspect a database incident.",
        alert={},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=await snapshot.discover_tools(),
        known_hypotheses=("postgres_deadlock",),
    )

    assert origin == "model"
    assert [(step["tool"], step["causalIntent"]) for step in plan] == [
        ("InspectPostgresErrors", "impact"),
        ("InspectPostgresWaitGraph", "mechanism"),
        ("InspectTransactionResourceOrder", "trigger"),
    ]
    assert [step["causalIntentOrigin"] for step in plan] == [
        "coverage_repair",
        "model",
        "coverage_repair",
    ]


def test_plan_requires_causal_intent() -> None:
    with pytest.raises(ValueError, match="causalIntent"):
        parse_plan(
            '{"steps":[{"id":"x","tool":"InspectPostgresWaitGraph",'
            '"arguments":{},"purpose":"inspect",'
            '"testsHypotheses":["deadlock"]}]}',
            available_tools={"InspectPostgresWaitGraph"},
            known_hypotheses={"deadlock"},
            causal_capabilities={"InspectPostgresWaitGraph": {"mechanism"}},
        )


def test_plan_rejects_intent_outside_tool_capability() -> None:
    with pytest.raises(ValueError, match="causalIntent"):
        parse_plan(
            '{"steps":[{"id":"x","tool":"InspectPostgresWaitGraph",'
            '"arguments":{},"purpose":"inspect",'
            '"testsHypotheses":["deadlock"],"causalIntent":"trigger"}]}',
            available_tools={"InspectPostgresWaitGraph"},
            known_hypotheses={"deadlock"},
            causal_capabilities={"InspectPostgresWaitGraph": {"mechanism"}},
        )


@pytest.mark.asyncio
async def test_evidence_evaluator_retains_known_support_and_allowed_model_role(
    migrated_database_url: str,
) -> None:
    prompts: list[str] = []

    class StaticChatModel:
        async def ainvoke(self, prompt: object) -> str:
            prompts.append(str(prompt))
            return json.dumps(
                {
                    "purpose": "Inspect transaction resource order.",
                    "supports": ["postgres_deadlock"],
                    "refutes": ["postgres_slow_query"],
                    "summary": "Transactions acquired rows in opposite order.",
                    "causalRole": "mechanism",
                }
            )

    class StaticLlmProvider:
        def create_chat_model(self) -> StaticChatModel:
            return StaticChatModel()

    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="causal-role-contract",
            status="running",
            query="Inspect a database incident.",
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, StaticLlmProvider()),
            retrieval_tool=cast(Any, object()),
            mcp_client=cast(Any, object()),
            cls_region="unused",
            cls_topic_id="unused",
        )

        update = await service._evidence_evaluator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "current_evidence_id": "ev-order",
                    "current_evidence_summary": "bounded observation",
                    "current_plan_step": {
                        "id": "resource-order",
                        "tool": "InspectTransactionResourceOrder",
                        "arguments": {},
                        "purpose": "Inspect transaction resource order.",
                        "testsHypotheses": ["postgres_deadlock"],
                        "causalIntent": "trigger",
                    },
                    "public_hypotheses": [
                        {"id": "postgres_deadlock", "description": "Deadlock."},
                        {
                            "id": "postgres_slow_query",
                            "description": "Slow query.",
                        },
                    ],
                    "hypothesis_states": [
                        {
                            "id": "postgres_deadlock",
                            "status": "open",
                            "confidence": 0.5,
                            "evidenceIds": [],
                        }
                    ],
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    observation = cast(list[dict[str, object]], update["observation_decisions"])[0]
    assert observation["summary"] == "Transactions acquired rows in opposite order."
    assert observation["supports"] == ["postgres_deadlock"]
    assert observation["refutes"] == ["postgres_slow_query"]
    assert observation["evidenceIds"] == ["ev-order"]
    assert observation["causalRole"] == "mechanism"
    assert observation["causalRoleOrigin"] == "model"
    assert observation["reportedCausalRole"] == "mechanism"
    assert observation["causalRoleCorrected"] is False
    assert steps[-1].payload["observationDecision"] == observation
    assert "compatible with a hypothesis is not sufficient to support it" in prompts[0]
    assert "decisively contradicts" in prompts[0]
    assert "Evaluate every hypothesis named by testsHypotheses" in prompts[0]


@pytest.mark.asyncio
async def test_evidence_evaluator_corrects_disallowed_model_role_to_plan_contract(
    migrated_database_url: str,
) -> None:
    class StaticChatModel:
        async def ainvoke(self, prompt: object) -> str:
            del prompt
            return json.dumps(
                {
                    "purpose": "Inspect the PostgreSQL wait graph.",
                    "supports": ["postgres_deadlock"],
                    "refutes": [],
                    "summary": "A two-session wait cycle exists.",
                    "causalRole": "trigger",
                }
            )

    class StaticLlmProvider:
        def create_chat_model(self) -> StaticChatModel:
            return StaticChatModel()

    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="disallowed-causal-role-contract",
            status="running",
            query="Inspect a database incident.",
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(Any, StaticLlmProvider()),
            retrieval_tool=cast(Any, object()),
            mcp_client=cast(Any, object()),
            cls_region="unused",
            cls_topic_id="unused",
        )

        update = await service._evidence_evaluator(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "current_evidence_id": "ev-cycle",
                    "current_evidence_summary": "bounded observation",
                    "current_plan_step": {
                        "id": "wait-graph",
                        "tool": "InspectPostgresWaitGraph",
                        "arguments": {},
                        "purpose": "Inspect the PostgreSQL wait graph.",
                        "testsHypotheses": ["postgres_deadlock"],
                        "causalIntent": "mechanism",
                    },
                    "public_hypotheses": [{"id": "postgres_deadlock"}],
                    "hypothesis_states": [
                        {
                            "id": "postgres_deadlock",
                            "status": "open",
                            "confidence": 0.5,
                            "evidenceIds": [],
                        }
                    ],
                },
            )
        )
    finally:
        await engine.dispose()

    observation = cast(list[dict[str, object]], update["observation_decisions"])[0]
    assert observation["causalRole"] == "mechanism"
    assert observation["causalRoleOrigin"] == "plan_contract"
    assert observation["reportedCausalRole"] == "trigger"
    assert observation["causalRoleCorrected"] is True


def test_supporting_decision_evidence_includes_incidental_known_support() -> None:
    assert _supporting_decision_evidence_ids(
        hypothesis_states=[{"id": "slow_database_work", "status": "supported"}],
        observation_decisions=[
            {
                "supports": ["slow_database_work"],
                "evidenceIds": ["ev-postgres"],
            },
            {
                "supports": ["slow_database_work"],
                "evidenceIds": ["ev-pool"],
            },
        ],
        persisted_evidence_ids=["ev-postgres", "ev-pool", "ev-deployment"],
    ) == ["ev-postgres", "ev-pool"]


def test_observation_decision_requires_known_hypotheses() -> None:
    with pytest.raises(ValueError, match="unknown hypothesis"):
        parse_observation_decision(
            '{"purpose":"check","supports":["invented"],"refutes":[],"summary":"x"}',
            known_hypotheses={"upstream_process_down"},
        )


def test_observation_decision_cannot_support_and_refute_same_hypothesis() -> None:
    with pytest.raises(ValueError, match="both support and refute"):
        parse_observation_decision(
            '{"purpose":"check","supports":["process-down"],'
            '"refutes":["process-down"],"summary":"x"}',
            known_hypotheses={"process-down"},
        )


def test_strong_accumulated_support_survives_one_conflicting_observation() -> None:
    decision = parse_observation_decision(
        '{"purpose":"check","supports":[],"refutes":["database_work"],'
        '"summary":"One observation conflicts.","causalRole":"context"}',
        known_hypotheses={"database_work"},
    )

    states = _update_hypothesis_states(
        [
            {
                "id": "database_work",
                "status": "supported",
                "confidence": 1.0,
                "evidenceIds": ["ev-1", "ev-2", "ev-3"],
            }
        ],
        decision=decision,
        evidence_id="ev-conflict",
    )

    assert states == [
        {
            "id": "database_work",
            "status": "supported",
            "confidence": 0.6,
            "evidenceIds": ["ev-1", "ev-2", "ev-3", "ev-conflict"],
        }
    ]


def test_weak_support_becomes_open_when_evidence_conflicts() -> None:
    decision = parse_observation_decision(
        '{"purpose":"check","supports":[],"refutes":["database_work"],'
        '"summary":"One observation conflicts.","causalRole":"context"}',
        known_hypotheses={"database_work"},
    )

    states = _update_hypothesis_states(
        [
            {
                "id": "database_work",
                "status": "supported",
                "confidence": 0.75,
                "evidenceIds": ["ev-support"],
            }
        ],
        decision=decision,
        evidence_id="ev-conflict",
    )

    assert states[0]["status"] == "open"
    assert states[0]["confidence"] == pytest.approx(0.35)


def test_open_hypothesis_becomes_refuted_after_one_refuting_observation() -> None:
    decision = parse_observation_decision(
        '{"purpose":"check","supports":[],"refutes":["traffic"],'
        '"summary":"Traffic stayed flat.","causalRole":"context"}',
        known_hypotheses={"traffic"},
    )

    states = _update_hypothesis_states(
        [
            {
                "id": "traffic",
                "status": "open",
                "confidence": 0.5,
                "evidenceIds": [],
            }
        ],
        decision=decision,
        evidence_id="ev-traffic",
    )

    assert states[0]["status"] == "refuted"
    assert states[0]["confidence"] == pytest.approx(0.1)


def test_context_support_cannot_outweigh_later_direct_refutation() -> None:
    context = parse_observation_decision(
        '{"purpose":"check","supports":["port_mismatch"],"refutes":[],'
        '"summary":"The symptom is compatible.","causalRole":"context"}',
        known_hypotheses={"port_mismatch"},
    )
    direct_refutation = parse_observation_decision(
        '{"purpose":"check","supports":[],"refutes":["port_mismatch"],'
        '"summary":"The process is not listening.","causalRole":"mechanism"}',
        known_hypotheses={"port_mismatch"},
    )

    after_context = _update_hypothesis_states(
        [
            {
                "id": "port_mismatch",
                "status": "open",
                "confidence": 0.5,
                "evidenceIds": [],
            }
        ],
        decision=context,
        evidence_id="ev-context",
    )
    after_direct = _update_hypothesis_states(
        after_context,
        decision=direct_refutation,
        evidence_id="ev-direct",
    )

    assert after_context[0]["status"] == "open"
    assert after_context[0]["confidence"] == pytest.approx(0.6)
    assert after_direct[0]["status"] == "refuted"
    assert after_direct[0]["confidence"] == pytest.approx(0.2)


def test_root_cause_decision_requires_available_evidence_ids() -> None:
    with pytest.raises(ValueError, match="evidence"):
        parse_root_cause_decision(
            '{"component":"checkout-service","mechanism":"process_unavailable",'
            '"trigger":"container_stopped","causalChain":[],"evidenceIds":[],'
            '"confidence":0.9}',
            available_evidence_ids={"ev-1"},
        )

    with pytest.raises(ValueError, match="unknown evidence"):
        parse_root_cause_decision(
            '{"component":"checkout-service","mechanism":"process_unavailable",'
            '"trigger":"container_stopped","causalChain":["stopped"],'
            '"evidenceIds":["fabricated"],"confidence":0.9}',
            available_evidence_ids={"ev-1"},
        )


def test_observation_decision_preserves_allowlisted_causal_role() -> None:
    decision = parse_observation_decision(
        '{"purpose":"check","supports":["process-down"],"refutes":[],'
        '"summary":"the process stopped","causalRole":"trigger"}',
        known_hypotheses={"process-down"},
    )

    assert decision.causal_role == "trigger"


def test_observation_decision_rejects_unknown_causal_role() -> None:
    with pytest.raises(ValueError, match="causalRole"):
        parse_observation_decision(
            '{"purpose":"check","supports":["process-down"],"refutes":[],'
            '"summary":"the process stopped","causalRole":"oracle"}',
            known_hypotheses={"process-down"},
        )


def test_root_cause_decision_accepts_string_causal_chain_as_one_item() -> None:
    decision = parse_root_cause_decision(
        json.dumps(
            {
                "component": "order-service",
                "mechanism": "opposite_order_transaction_deadlock",
                "trigger": "concurrent updates",
                "causalChain": (
                    "Concurrent transactions acquired resources in reverse order."
                ),
                "evidenceIds": ["ev-deadlock", "ev-cycle"],
                "confidence": 1.0,
            }
        ),
        available_evidence_ids={"ev-deadlock", "ev-cycle"},
    )

    assert decision.causal_chain == (
        "Concurrent transactions acquired resources in reverse order.",
    )


@pytest.mark.parametrize(
    "causal_chain",
    ("", "   ", 42, {"step": "invalid"}, ["valid", 42]),
)
def test_root_cause_decision_rejects_invalid_causal_chain_shapes(
    causal_chain: object,
) -> None:
    with pytest.raises(ValueError, match="causalChain"):
        parse_root_cause_decision(
            json.dumps(
                {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                    "trigger": "concurrent updates",
                    "causalChain": causal_chain,
                    "evidenceIds": ["ev-deadlock"],
                    "confidence": 0.9,
                }
            ),
            available_evidence_ids={"ev-deadlock"},
        )


def test_root_cause_decision_preserves_list_causal_chain() -> None:
    decision = parse_root_cause_decision(
        json.dumps(
            {
                "component": "order-service",
                "mechanism": "opposite_order_transaction_deadlock",
                "trigger": "concurrent updates",
                "causalChain": ["resources acquired", "wait cycle detected"],
                "evidenceIds": ["ev-deadlock"],
                "confidence": 0.9,
            }
        ),
        available_evidence_ids={"ev-deadlock"},
    )

    assert decision.causal_chain == ("resources acquired", "wait cycle detected")


def test_root_cause_decision_normalizes_only_declared_public_labels() -> None:
    decision = RootCauseDecision(
        component="postgres",
        mechanism="postgres_lock_blocking",
        trigger="concurrent transaction holds a row lock",
        causal_chain=("row lock held", "update waits"),
        evidence_ids=("ev-1",),
        confidence=0.9,
    )

    normalized = normalize_root_cause_decision(
        decision,
        component_aliases={"postgres": "postgresql"},
        mechanism_aliases={"postgres_lock_blocking": "row_lock_blocking"},
    )

    assert normalized.component == "postgresql"
    assert normalized.mechanism == "row_lock_blocking"
    assert normalized.trigger == decision.trigger
    assert normalized.causal_chain == decision.causal_chain
    assert normalized.evidence_ids == decision.evidence_ids


def test_sufficiency_rejects_unknown_evidence_hypotheses_and_tools() -> None:
    payload = json.dumps(
        {
            "status": "insufficient",
            "evidenceIds": ["fabricated"],
            "supportedHypotheses": ["unknown"],
            "refutedHypotheses": [],
            "unresolvedHypotheses": ["h-open"],
            "missingEvidence": ["Read the lock graph."],
            "recommendedTools": ["Shell"],
            "summary": "More evidence is required.",
        }
    )

    with pytest.raises(ValueError):
        parse_evidence_sufficiency(
            payload,
            available_evidence_ids={"ev-1"},
            known_hypotheses={"h-open"},
            available_tools={"InspectPostgresLockGraph"},
        )


def test_root_cause_validation_limits_unsupported_fields() -> None:
    with pytest.raises(ValueError, match="unsupported field"):
        parse_root_cause_validation(
            json.dumps(
                {
                    "status": "invalid",
                    "evidenceIds": ["ev-1"],
                    "unsupportedFields": ["privateReasoning"],
                    "missingEvidence": ["A direct trigger observation is missing."],
                    "summary": "The trigger is unsupported.",
                }
            ),
            available_evidence_ids={"ev-1"},
        )


def test_recovery_plan_requires_schema_valid_proposal_fields() -> None:
    plan = parse_recovery_plan(
        json.dumps(
            {
                "mode": "proposal_only",
                "action": "propose_nginx_timeout_mitigation",
                "target": "live_eval_upstream",
                "rationale": "The upstream response exceeded the read timeout.",
                "tool": "ProposeNginxTimeoutMitigation",
                "arguments": {
                    "target": "live_eval_upstream",
                    "risk": "A larger timeout can retain connections longer.",
                    "rollback": "Restore the previous timeout after approval.",
                    "verificationSteps": [
                        "Repeat the gateway probe.",
                        "Confirm the upstream latency is within the approved timeout.",
                    ],
                    "humanApprovalRequired": True,
                },
                "risk": "A larger timeout can retain connections longer.",
                "rollback": "Restore the previous timeout after approval.",
                "verificationSteps": [
                    "Repeat the gateway probe.",
                    "Confirm the upstream latency is within the approved timeout.",
                ],
                "evidenceIds": ["ev-1"],
                "decisionConfidence": 0.91,
                "humanApprovalRequired": True,
            }
        ),
        available_evidence_ids={"ev-1"},
        proposal_tools={"ProposeNginxTimeoutMitigation"},
    )

    assert plan.mode == "proposal_only"
    assert plan.tool == "ProposeNginxTimeoutMitigation"
    assert plan.human_approval_required is True


class EmptyRetrieval:
    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=input.top_k or 3,
            results=[],
            citations=[],
        )


class SufficientGateChatModel:
    async def ainvoke(self, input: object) -> str:
        del input
        return json.dumps(
            {
                "status": "sufficient",
                "evidenceIds": ["ev-cycle"],
                "supportedHypotheses": ["postgres_deadlock"],
                "refutedHypotheses": ["postgres_lock_wait", "postgres_slow_query"],
                "unresolvedHypotheses": [],
                "missingEvidence": [],
                "recommendedTools": [],
                "summary": "The observed cycle supports the deadlock candidate.",
            }
        )


class SufficientGateLlmProvider:
    def create_chat_model(self) -> SufficientGateChatModel:
        return SufficientGateChatModel()


class OneStringDecisionChatModel:
    async def ainvoke(self, input: object) -> str:
        del input
        return json.dumps(
            {
                "component": "order-service",
                "mechanism": "opposite_order_transaction_deadlock",
                "trigger": "Transactions acquired resources in opposite orders.",
                "causalChain": "One combined causal narrative.",
                "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
                "confidence": 0.97,
            }
        )


class OneStringDecisionLlmProvider:
    def create_chat_model(self) -> OneStringDecisionChatModel:
        return OneStringDecisionChatModel()


@pytest.mark.asyncio
async def test_decision_node_normalizes_grounded_expression(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="decision-grounded-causal-chain-repair",
            status="running",
            query=scenario.title,
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, OneStringDecisionLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
        )

        update = await service._decision(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "evidence_ids": ["ev-error", "ev-cycle", "ev-order"],
                    "public_hypotheses": [
                        {
                            "id": "postgres_deadlock",
                            "description": "Concurrent transactions formed a cycle.",
                        }
                    ],
                    "hypothesis_states": [
                        {
                            "id": "postgres_deadlock",
                            "status": "supported",
                            "confidence": 1.0,
                            "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
                        }
                    ],
                    "observation_decisions": [
                        {
                            "supports": ["postgres_deadlock"],
                            "evidenceIds": ["ev-error"],
                            "causalRole": "impact",
                            "summary": "PostgreSQL emitted SQLSTATE 40P01.",
                        },
                        {
                            "supports": ["postgres_deadlock"],
                            "evidenceIds": ["ev-cycle"],
                            "causalRole": "mechanism",
                            "summary": "The wait graph contained a two-session cycle.",
                        },
                        {
                            "supports": ["postgres_deadlock"],
                            "evidenceIds": ["ev-order"],
                            "causalRole": "trigger",
                            "summary": "Transactions acquired resources in opposite order.",
                        },
                    ],
                    "decision_vocabulary": {
                        "labelsByHypothesis": {
                            "postgres_deadlock": {
                                "component": "order-service",
                                "mechanism": "opposite_order_transaction_deadlock",
                            }
                        }
                    },
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    decision_payload = cast(dict[str, object], update["root_cause_decision"])
    assert decision_payload["trigger"] == (
        "Transactions acquired resources in opposite order."
    )
    assert decision_payload["confidence"] == 0.97
    assert decision_payload["evidenceIds"] == ["ev-error", "ev-cycle", "ev-order"]
    assert decision_payload["causalChain"] == [
        "Transactions acquired resources in opposite order.",
        "The wait graph contained a two-session cycle.",
        "PostgreSQL emitted SQLSTATE 40P01.",
    ]
    decision_step = steps[-1]
    assert decision_step.payload["decisionOrigin"] == "llm_grounded_normalization"
    assert decision_step.payload["decisionErrorCategory"] is None
    assert decision_step.payload["decisionAttempts"] == 1
    assert decision_step.payload["decisionErrorCodes"] == []
    assert decision_step.payload["decisionErrorCode"] is None
    assert decision_step.payload["decisionErrorPhase"] is None
    assert decision_step.payload["decisionRetryable"] is None
    assert decision_step.payload["decisionHttpStatusClass"] is None


@pytest.mark.asyncio
async def test_sufficiency_gate_routes_to_supported_refinement_and_audits_reason(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    metrics = _refinement_step("metrics", "GetDatabaseMetrics", ["postgres_slow_query"])
    resource_order = _refinement_step(
        "resource-order",
        "InspectTransactionResourceOrder",
        ["postgres_deadlock"],
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, SufficientGateLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
        )

        async def run_gate(
            task_id: str,
            *,
            attempts: int,
            candidate_plan: list[dict[str, object]] | None = None,
            linked_evidence_ids: list[str] | None = None,
            observations: list[dict[str, object]] | None = None,
            hypothesis_states: list[dict[str, object]] | None = None,
        ) -> tuple[dict[str, object], DiagnosticStepRecord]:
            await repositories.diagnostics.create_task(
                owner_user_id="benchmark-user",
                task_id=task_id,
                status="running",
                query=scenario.title,
                input_payload={},
            )
            update = await service._sufficiency_gate(  # pyright: ignore[reportPrivateUsage]
                cast(
                    Any,
                    {
                        "owner_user_id": "benchmark-user",
                        "task_id": task_id,
                        "plan": candidate_plan or [metrics, resource_order],
                        "plan_index": 0,
                        "evidence_ids": linked_evidence_ids or ["ev-cycle"],
                        "public_hypotheses": [
                            {"id": item.id, "description": item.description}
                            for item in scenario.hypotheses
                        ],
                        "hypothesis_states": hypothesis_states
                        or [
                            {
                                "id": "postgres_deadlock",
                                "status": "supported",
                                "confidence": 1.0,
                                "evidenceIds": linked_evidence_ids or ["ev-cycle"],
                            },
                            {
                                "id": "postgres_lock_wait",
                                "status": "refuted",
                                "confidence": 0.1,
                                "evidenceIds": ["ev-cycle"],
                            },
                            {
                                "id": "postgres_slow_query",
                                "status": "refuted",
                                "confidence": 0.1,
                                "evidenceIds": ["ev-cycle"],
                            },
                        ],
                        "observation_decisions": observations
                        or [
                            {
                                "supports": ["postgres_deadlock"],
                                "evidenceIds": ["ev-cycle"],
                                "causalRole": "mechanism",
                                "summary": "A two-session cycle exists.",
                            }
                        ],
                        "evidence": [],
                        "tool_definitions": (),
                        "executed_step_fingerprints": [],
                        "executor_attempt_count": attempts,
                        "max_total_steps": 6,
                        "max_replans": 2,
                        "replan_count": 0,
                    },
                )
            )
            steps = await repositories.diagnostics.list_steps(
                owner_user_id="benchmark-user",
                task_id=task_id,
            )
            return update, steps[-1]

        update, gate_step = await run_gate("gate-with-refinement", attempts=2)
        exhausted, exhausted_step = await run_gate("gate-without-budget", attempts=6)
        no_match, no_match_step = await run_gate(
            "gate-requires-replan",
            attempts=2,
            candidate_plan=[metrics],
        )
        complete, complete_step = await run_gate(
            "gate-complete-coverage",
            attempts=3,
            linked_evidence_ids=["ev-trigger", "ev-cycle", "ev-impact"],
            observations=[
                {
                    "supports": ["postgres_deadlock"],
                    "evidenceIds": ["ev-trigger"],
                    "causalRole": "trigger",
                    "summary": "Opposite resource order was observed.",
                },
                {
                    "supports": ["postgres_deadlock"],
                    "evidenceIds": ["ev-cycle"],
                    "causalRole": "mechanism",
                    "summary": "A two-session cycle exists.",
                },
                {
                    "supports": ["postgres_deadlock"],
                    "evidenceIds": ["ev-impact"],
                    "causalRole": "impact",
                    "summary": "PostgreSQL aborted a transaction.",
                },
            ],
        )
        open_competitor, open_competitor_step = await run_gate(
            "gate-open-competitor",
            attempts=2,
            candidate_plan=[resource_order, metrics],
            hypothesis_states=[
                {
                    "id": "postgres_deadlock",
                    "status": "supported",
                    "confidence": 1.0,
                    "evidenceIds": ["ev-cycle"],
                },
                {
                    "id": "postgres_lock_wait",
                    "status": "refuted",
                    "confidence": 0.1,
                    "evidenceIds": ["ev-cycle"],
                },
                {
                    "id": "postgres_slow_query",
                    "status": "open",
                    "confidence": 0.5,
                    "evidenceIds": [],
                },
            ],
        )
    finally:
        await engine.dispose()

    assert update["next_route"] == "executor"
    assert update["plan_index"] == 1
    assert update["termination_reason"] == ""
    assert gate_step.payload["nextRoute"] == "executor"
    assert (
        gate_step.payload["refinementReason"]
        == "missing_causal_role_plan_step_remaining"
    )
    assert gate_step.payload["missingCausalRoles"] == ["trigger", "impact"]
    assert exhausted["next_route"] == "decision"
    assert "plan_index" not in exhausted
    assert exhausted_step.payload["refinementReason"] == ""
    assert no_match["next_route"] == "replanner"
    assert no_match_step.payload["refinementReason"] == (
        "missing_causal_role_requires_replan"
    )
    assert complete["next_route"] == "decision"
    assert complete_step.payload["missingCausalRoles"] == []
    assert open_competitor["next_route"] == "executor"
    assert open_competitor["plan_index"] == 1
    assert open_competitor_step.payload["status"] == "insufficient"
    assert open_competitor_step.payload["unresolvedHypotheses"] == [
        "postgres_slow_query"
    ]
    assert open_competitor_step.payload["refinementReason"] == (
        "open_hypothesis_plan_step_remaining"
    )


class ReasoningChatModel:
    def __init__(self) -> None:
        self.observation_count = 0

    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "bounded diagnostic plan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-container",
                            "tool": "InspectContainer",
                            "arguments": {"service": "checkout-service"},
                            "purpose": "Check whether the upstream process is running.",
                            "testsHypotheses": [
                                "upstream_process_down",
                                "upstream_port_mismatch",
                            ],
                            "causalIntent": "mechanism",
                        },
                        {
                            "id": "inspect-nginx",
                            "tool": "InspectNginx",
                            "arguments": {"route": "checkout"},
                            "purpose": "Compare the gateway upstream with the service port.",
                            "testsHypotheses": [
                                "upstream_port_mismatch",
                                "dns_resolution_failure",
                            ],
                            "causalIntent": "context",
                        },
                    ]
                }
            )
        if "observation decision" in prompt:
            self.observation_count += 1
            if self.observation_count == 1:
                return json.dumps(
                    {
                        "purpose": "Check whether the upstream process is running.",
                        "supports": ["upstream_process_down"],
                        "refutes": ["upstream_port_mismatch"],
                        "summary": "The checkout container is exited and not listening.",
                    }
                )
            return json.dumps(
                {
                    "purpose": "Compare the gateway upstream with the service port.",
                    "supports": ["upstream_process_down"],
                    "refutes": ["dns_resolution_failure"],
                    "summary": (
                        "Nginx resolved checkout and its configured port refused connections."
                    ),
                }
            )
        if "root-cause decision" in prompt:
            supporting_ids = re.search(
                r"Supporting observation evidence IDs: (\[[^\]]*\])", prompt
            )
            evidence_ids = (
                cast(list[str], json.loads(supporting_ids.group(1)))
                if supporting_ids is not None
                else []
            )
            return json.dumps(
                {
                    "component": "checkout-service",
                    "mechanism": "process_unavailable",
                    "trigger": "benchmark_container_stopped",
                    "causalChain": [
                        "checkout container stopped",
                        "nginx upstream connection was refused",
                    ],
                    "evidenceIds": evidence_ids,
                    "confidence": 0.95,
                }
            )
        if "root-cause validation decision" in prompt:
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            return json.dumps(
                {
                    "status": "valid",
                    "evidenceIds": evidence_ids,
                    "unsupportedFields": [],
                    "missingEvidence": [],
                    "summary": "The structured observations support the candidate decision.",
                }
            )
        return """# 告警分析报告

## 📋 活跃告警清单

- CheckoutUpstream5xxHigh

## 📊 结论

结构化根因决策已关联持久化证据。"""


class ReasoningLlmProvider:
    def __init__(self) -> None:
        self.model = ReasoningChatModel()

    def create_chat_model(self) -> ReasoningChatModel:
        return self.model


class ReplanningChatModel:
    def __init__(self) -> None:
        self.observation_count = 0
        self.sufficiency_count = 0

    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "bounded diagnostic plan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-container",
                            "tool": "InspectContainer",
                            "arguments": {"service": "checkout-service"},
                            "purpose": "Check whether the checkout process is running.",
                            "testsHypotheses": [
                                "upstream_process_down",
                                "upstream_port_mismatch",
                            ],
                            "causalIntent": "mechanism",
                        }
                    ]
                }
            )
        if "gap-targeted diagnostic replan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-nginx-after-gap",
                            "tool": "InspectNginx",
                            "arguments": {"route": "checkout"},
                            "purpose": "Resolve the remaining route and DNS alternatives.",
                            "testsHypotheses": [
                                "upstream_port_mismatch",
                                "dns_resolution_failure",
                            ],
                            "causalIntent": "context",
                        }
                    ]
                }
            )
        if "observation decision" in prompt:
            self.observation_count += 1
            if self.observation_count == 1:
                return json.dumps(
                    {
                        "purpose": "Check whether the checkout process is running.",
                        "supports": ["upstream_process_down"],
                        "refutes": [],
                        "summary": "The checkout container is exited and has no listener.",
                        "causalRole": "mechanism",
                    }
                )
            return json.dumps(
                {
                    "purpose": "Resolve the remaining route and DNS alternatives.",
                    "supports": ["upstream_process_down"],
                    "refutes": ["upstream_port_mismatch", "dns_resolution_failure"],
                    "summary": (
                        "Nginx resolves checkout on port 8080 but the connection is refused."
                    ),
                    "causalRole": "context",
                }
            )
        if "evidence sufficiency decision" in prompt:
            self.sufficiency_count += 1
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            if self.sufficiency_count == 1:
                return json.dumps(
                    {
                        "status": "insufficient",
                        "evidenceIds": evidence_ids,
                        "supportedHypotheses": ["upstream_process_down"],
                        "refutedHypotheses": [],
                        "unresolvedHypotheses": [
                            "upstream_port_mismatch",
                            "dns_resolution_failure",
                        ],
                        "missingEvidence": ["Inspect the gateway route and DNS result."],
                        "recommendedTools": ["InspectNginx"],
                        "summary": "The process is down but gateway alternatives remain open.",
                    }
                )
            return json.dumps(
                {
                    "status": "sufficient",
                    "evidenceIds": evidence_ids,
                    "supportedHypotheses": ["upstream_process_down"],
                    "refutedHypotheses": [
                        "upstream_port_mismatch",
                        "dns_resolution_failure",
                    ],
                    "unresolvedHypotheses": [],
                    "missingEvidence": [],
                    "recommendedTools": [],
                    "summary": "The process failure is supported and alternatives are refuted.",
                }
            )
        if "root-cause decision" in prompt:
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            return json.dumps(
                {
                    "component": "checkout-service",
                    "mechanism": "process_unavailable",
                    "trigger": "benchmark_container_stopped",
                    "causalChain": [
                        "checkout container stopped",
                        "nginx upstream connection was refused",
                    ],
                    "evidenceIds": evidence_ids[-2:],
                    "confidence": 0.95,
                }
            )
        if "root-cause validation decision" in prompt:
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            return json.dumps(
                {
                    "status": "valid",
                    "evidenceIds": evidence_ids,
                    "unsupportedFields": [],
                    "missingEvidence": [],
                    "summary": "The trigger and causal chain are supported by the observations.",
                }
            )
        if "structured recovery plan" in prompt:
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            return json.dumps(
                {
                    "mode": "external_policy_required",
                    "action": "restore_checkout_process",
                    "target": "checkout-service",
                    "rationale": "The validated diagnosis shows the process is unavailable.",
                    "tool": None,
                    "arguments": {},
                    "risk": "A restart can interrupt in-flight work.",
                    "rollback": "Stop the replacement and restore the prior deployment state.",
                    "verificationSteps": [
                        "Verify the checkout listener is healthy.",
                        "Repeat the gateway request and confirm the 502 alert resolves.",
                    ],
                    "evidenceIds": evidence_ids,
                    "decisionConfidence": 0.95,
                    "humanApprovalRequired": True,
                }
            )
        return (
            "# 告警分析报告\n\n## 📋 活跃告警清单\n\n"
            "- CheckoutUpstream5xxHigh\n\n## 📊 结论\n\n证据充分。"
        )


class ReplanningLlmProvider:
    def __init__(self) -> None:
        self.model = ReplanningChatModel()

    def create_chat_model(self) -> ReplanningChatModel:
        return self.model


class ContractReplanningChatModel(ReplanningChatModel):
    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "bounded diagnostic plan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-container-wrong-scope",
                            "tool": "InspectContainer",
                            "arguments": {"service": "order-service"},
                            "purpose": "Check whether the checkout process is running.",
                            "testsHypotheses": [
                                "upstream_process_down",
                                "upstream_port_mismatch",
                            ],
                            "causalIntent": "mechanism",
                        }
                    ]
                }
            )
        if "gap-targeted diagnostic replan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-nginx-wrong-scope",
                            "tool": "InspectNginx",
                            "arguments": {"route": "order-service"},
                            "purpose": "Resolve the remaining route and DNS alternatives.",
                            "testsHypotheses": [
                                "upstream_port_mismatch",
                                "dns_resolution_failure",
                            ],
                            "causalIntent": "context",
                        }
                    ]
                }
            )
        return await super().ainvoke(input)


class ContractReplanningLlmProvider:
    def __init__(self) -> None:
        self.model = ContractReplanningChatModel()

    def create_chat_model(self) -> ContractReplanningChatModel:
        return self.model


class PostgresContractAcceptanceChatModel:
    def __init__(self) -> None:
        self.sufficiency_count = 0
        self.supporting_evidence_ids: list[str] = []

    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
        if "bounded diagnostic plan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "errors",
                            "tool": "InspectPostgresErrors",
                            "arguments": {
                                "service": "order-service",
                                "windowMinutes": 30,
                            },
                            "purpose": "Inspect structured PostgreSQL errors.",
                            "testsHypotheses": [
                                "postgres_deadlock",
                                "postgres_slow_query",
                            ],
                            "causalIntent": "impact",
                        },
                        {
                            "id": "wait-graph",
                            "tool": "InspectPostgresWaitGraph",
                            "arguments": {
                                "database": "order-service",
                                "windowMinutes": 60,
                            },
                            "purpose": "Inspect the PostgreSQL wait graph.",
                            "testsHypotheses": [
                                "postgres_deadlock",
                                "postgres_lock_wait",
                            ],
                            "causalIntent": "mechanism",
                        },
                        {
                            "id": "metrics",
                            "tool": "GetDatabaseMetrics",
                            "arguments": {
                                "database": "order-service",
                                "windowMinutes": 30,
                            },
                            "purpose": "Rule out database capacity and slow-query pressure.",
                            "testsHypotheses": ["postgres_slow_query"],
                            "causalIntent": "context",
                        },
                        {
                            "id": "resource-order",
                            "tool": "InspectTransactionResourceOrder",
                            "arguments": {
                                "service": "order-service",
                                "windowMinutes": 60,
                            },
                            "purpose": "Compare transaction resource order.",
                            "testsHypotheses": ["postgres_deadlock"],
                            "causalIntent": "trigger",
                        },
                    ]
                }
            )
        if "gap-targeted diagnostic replan" in prompt:
            return json.dumps({"steps": []})
        if "observation decision" in prompt:
            assert "causalRole" in prompt
            assert "trigger, mechanism, impact, or context" in prompt
            if "InspectPostgresWaitGraph" in prompt:
                self.supporting_evidence_ids.extend(
                    item
                    for item in evidence_ids
                    if item not in self.supporting_evidence_ids
                )
                return json.dumps(
                    {
                        "purpose": "Inspect the PostgreSQL wait graph.",
                        "supports": ["postgres_deadlock"],
                        "refutes": ["postgres_lock_wait"],
                        "causalRole": "mechanism",
                        "summary": "A two-session cycle exists without an ordinary blocker.",
                    }
                )
            if "GetDatabaseMetrics" in prompt:
                return json.dumps(
                    {
                        "purpose": "Rule out database capacity and slow-query pressure.",
                        "supports": [],
                        "refutes": ["postgres_slow_query"],
                        "causalRole": "context",
                        "summary": "Latency and capacity remain within the bounded baseline.",
                    }
                )
            if "InspectTransactionResourceOrder" in prompt:
                self.supporting_evidence_ids.extend(
                    item
                    for item in evidence_ids
                    if item not in self.supporting_evidence_ids
                )
                return json.dumps(
                    {
                        "purpose": "Compare transaction resource order.",
                        "supports": ["postgres_deadlock"],
                        "refutes": [],
                        "causalRole": "trigger",
                        "summary": (
                            "Concurrent transactions acquired order rows and inventory "
                            "rows in opposite order."
                        ),
                    }
                )
            self.supporting_evidence_ids.extend(
                item
                for item in evidence_ids
                if item not in self.supporting_evidence_ids
            )
            return json.dumps(
                {
                    "purpose": "Inspect deadlock evidence.",
                    "supports": ["postgres_deadlock"],
                    "refutes": [],
                    "causalRole": "impact",
                    "summary": "PostgreSQL aborted one transaction with SQLSTATE 40P01.",
                }
            )
        if "evidence sufficiency decision" in prompt:
            self.sufficiency_count += 1
            sufficient = self.sufficiency_count >= 2
            return json.dumps(
                {
                    "status": "sufficient" if sufficient else "insufficient",
                    "evidenceIds": self.supporting_evidence_ids,
                    "supportedHypotheses": ["postgres_deadlock"],
                    "refutedHypotheses": (
                        ["postgres_lock_wait", "postgres_slow_query"]
                        if sufficient
                        else []
                    ),
                    "unresolvedHypotheses": (
                        []
                        if sufficient
                        else ["postgres_lock_wait", "postgres_slow_query"]
                    ),
                    "missingEvidence": [] if sufficient else ["Inspect remaining alternatives."],
                    "recommendedTools": (
                        []
                        if sufficient
                        else ["InspectPostgresWaitGraph", "GetDatabaseMetrics"]
                    ),
                    "summary": (
                        "The deadlock and its alternatives are resolved."
                        if sufficient
                        else "More direct and rule-out evidence is required."
                    ),
                }
            )
        if "root-cause decision" in prompt:
            assert "direct triggering condition" in prompt
            assert "2 to 6 ordered atomic causal facts" in prompt
            assert "map to a supporting structured observation" in prompt
            assert "root_cause_semantics" not in prompt
            return json.dumps(
                {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                    "trigger": (
                        "concurrent_updates_acquired_order_and_inventory_rows_in_reverse_order"
                    ),
                    "causalChain": (
                        "Transactions acquire shared resources in opposite orders -> "
                        "a wait cycle forms -> PostgreSQL aborts one transaction."
                    ),
                    "evidenceIds": self.supporting_evidence_ids,
                    "confidence": 0.96,
                }
            )
        if "root-cause validation decision" in prompt:
            return json.dumps(
                {
                    "status": "valid",
                    "evidenceIds": evidence_ids,
                    "unsupportedFields": [],
                    "missingEvidence": [],
                    "summary": "The error, resource order, and wait cycle support the decision.",
                }
            )
        if "structured recovery plan" in prompt:
            return json.dumps(
                {
                    "mode": "external_policy_required",
                    "action": "standardize_transaction_resource_order",
                    "target": "order-service",
                    "rationale": "The validated cycle requires an application change.",
                    "tool": None,
                    "arguments": {},
                    "risk": "Changing transaction order requires application review.",
                    "rollback": "Restore the previous transaction implementation.",
                    "verificationSteps": [
                        "Run concurrent order updates.",
                        "Confirm SQLSTATE 40P01 no longer occurs.",
                    ],
                    "evidenceIds": evidence_ids,
                    "decisionConfidence": 0.96,
                    "humanApprovalRequired": True,
                }
            )
        return "# PostgreSQL transaction diagnosis\n\nThe bounded evidence supports a deadlock."


class PostgresContractAcceptanceLlmProvider:
    def __init__(self) -> None:
        self.model = PostgresContractAcceptanceChatModel()

    def create_chat_model(self) -> PostgresContractAcceptanceChatModel:
        return self.model


class RecordingPostgresMainModel(PostgresContractAcceptanceChatModel):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[str] = []

    async def ainvoke(self, input: object) -> str:
        self.inputs.append(str(input))
        return await super().ainvoke(input)


class RecordingDedicatedValidatorModel:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.wrapper_calls = 0

    def with_structured_output(
        self,
        _schema: type[object],
        **kwargs: object,
    ) -> "RecordingDedicatedValidatorModel":
        assert kwargs == {"method": "json_mode", "include_raw": True}
        self.wrapper_calls += 1
        return self

    async def ainvoke(self, input: object) -> object:
        prompt = str(input)
        self.inputs.append(prompt)
        evidence_ids = list(
            dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt))
        )
        return {
            "raw": object(),
            "parsed": {
                "status": "valid",
                "evidenceIds": evidence_ids,
                "unsupportedFields": [],
                "missingEvidence": [],
                "summary": "Public evidence supports the candidate.",
            },
            "parsing_error": None,
        }


class DedicatedValidatorLlmProvider:
    structured_output_method = "function_calling"
    validator_structured_output_method = "json_mode"
    validator_model_name = "qwen3.8-max"

    def __init__(self) -> None:
        self.main_model = RecordingPostgresMainModel()
        self.validator_model = RecordingDedicatedValidatorModel()

    def create_chat_model(self) -> RecordingPostgresMainModel:
        return self.main_model

    def create_validator_model(self) -> RecordingDedicatedValidatorModel:
        return self.validator_model


class RecordingDiagnosticsRepository:
    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []
        self.checkpoints: list[dict[str, object]] = []

    async def list_steps(self, **_kwargs: object) -> list[object]:
        return cast(list[object], self.steps)

    async def create_step(self, **kwargs: object) -> object:
        self.steps.append(kwargs)
        return object()

    async def save_checkpoint(self, **kwargs: object) -> object:
        self.checkpoints.append(kwargs)
        return object()


class RecordingMemoryRepositories:
    def __init__(self) -> None:
        self.diagnostics = RecordingDiagnosticsRepository()
        self.tool_call_audits = None


def test_legacy_provider_uses_main_model_for_validation_without_exposing_metadata() -> None:
    provider = PostgresContractAcceptanceLlmProvider()

    assert _validator_chat_model(cast(LlmProvider, provider)) is provider.model
    assert _validator_model_name(cast(LlmProvider, provider)) == "legacy-main-model"
    assert (
        _validator_structured_output_method(cast(LlmProvider, provider))
        == "function_calling"
    )


class UnavailablePostgresValidatorChatModel(PostgresContractAcceptanceChatModel):
    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "observation decision" in prompt and "InspectPostgresErrors" in prompt:
            assert "causalRole" in prompt
            return json.dumps(
                {
                    "purpose": "Inspect structured PostgreSQL errors.",
                    "supports": ["postgres_deadlock"],
                    "refutes": ["postgres_slow_query"],
                    "causalRole": "impact",
                    "summary": "PostgreSQL emitted SQLSTATE 40P01 without statement timeouts.",
                }
            )
        if "root-cause validation decision" in prompt:
            raise TimeoutError("validator provider timeout")
        if "root-cause decision" in prompt:
            evidence_ids = list(
                dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt))
            )[:3]
            return json.dumps(
                {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                    "trigger": "concurrent_updates_acquired_rows_in_reverse_order",
                    "causalChain": "One combined causal narrative.",
                    "evidenceIds": evidence_ids,
                    "confidence": 0.96,
                }
            )
        return await super().ainvoke(input)


class UnavailablePostgresValidatorLlmProvider:
    def __init__(self) -> None:
        self.model = UnavailablePostgresValidatorChatModel()

    def create_chat_model(self) -> UnavailablePostgresValidatorChatModel:
        return self.model


class MissingPostgresCandidateChatModel(PostgresContractAcceptanceChatModel):
    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "evidence sufficiency decision" in prompt:
            evidence_ids = list(
                dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt))
            )
            return json.dumps(
                {
                    "status": "sufficient",
                    "evidenceIds": evidence_ids,
                    "supportedHypotheses": [
                        "postgres_deadlock",
                        "postgres_slow_query",
                    ],
                    "refutedHypotheses": ["postgres_lock_wait"],
                    "unresolvedHypotheses": [],
                    "missingEvidence": [],
                    "recommendedTools": [],
                    "summary": "Two causes remain supported, so no unique candidate exists.",
                }
            )
        if "observation decision" in prompt:
            return json.dumps(
                {
                    "purpose": "Keep two public causes equally supported.",
                    "supports": ["postgres_deadlock", "postgres_slow_query"],
                    "refutes": [],
                    "summary": "The observation leaves two competing causes supported.",
                }
            )
        if "root-cause decision" in prompt:
            return "not-json"
        return await super().ainvoke(input)


class MissingPostgresCandidateLlmProvider:
    def __init__(self) -> None:
        self.model = MissingPostgresCandidateChatModel()

    def create_chat_model(self) -> MissingPostgresCandidateChatModel:
        return self.model


class UngroundedUnavailableValidatorChatModel(
    UnavailablePostgresValidatorChatModel
):
    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "root-cause validation decision" in prompt:
            raise TimeoutError("validator provider timeout")
        if "root-cause decision" in prompt:
            evidence_ids = list(
                dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt))
            )
            return json.dumps(
                {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                    "trigger": "concurrent_updates_acquired_rows_in_reverse_order",
                    "causalChain": [
                        "PostgreSQL emitted SQLSTATE 40P01 without statement timeouts.",
                        "A two-session cycle exists without an ordinary blocker.",
                        "The observation contains direct cyclic-dependency evidence.",
                    ],
                    "evidenceIds": evidence_ids,
                    "confidence": 0.96,
                }
            )
        return await super().ainvoke(input)


class UngroundedUnavailableValidatorLlmProvider:
    def __init__(self) -> None:
        self.model = UngroundedUnavailableValidatorChatModel()

    def create_chat_model(self) -> UngroundedUnavailableValidatorChatModel:
        return self.model


class DuplicateStepChatModel(ReplanningChatModel):
    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "bounded diagnostic plan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-container-first",
                            "tool": "InspectContainer",
                            "arguments": {"service": "checkout-service"},
                            "purpose": "Check the checkout process.",
                            "testsHypotheses": ["upstream_process_down"],
                            "causalIntent": "mechanism",
                        },
                        {
                            "id": "inspect-container-duplicate",
                            "tool": "InspectContainer",
                            "arguments": {"service": "checkout-service"},
                            "purpose": "Repeat the same process check.",
                            "testsHypotheses": ["upstream_process_down"],
                            "causalIntent": "mechanism",
                        },
                    ]
                }
            )
        return await super().ainvoke(input)


class DuplicateStepLlmProvider:
    def __init__(self) -> None:
        self.model = DuplicateStepChatModel()

    def create_chat_model(self) -> DuplicateStepChatModel:
        return self.model


class ValidationGapChatModel(ReplanningChatModel):
    def __init__(self) -> None:
        super().__init__()
        self.validation_count = 0

    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        if "evidence sufficiency decision" in prompt:
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            return json.dumps(
                {
                    "status": "sufficient",
                    "evidenceIds": evidence_ids,
                    "supportedHypotheses": ["upstream_process_down"],
                    "refutedHypotheses": [
                        "upstream_port_mismatch",
                        "dns_resolution_failure",
                    ],
                    "unresolvedHypotheses": [],
                    "missingEvidence": [],
                    "recommendedTools": [],
                    "summary": "A candidate decision can be attempted.",
                }
            )
        if "root-cause validation decision" in prompt:
            self.validation_count += 1
            evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
            if self.validation_count == 1:
                return json.dumps(
                    {
                        "status": "invalid",
                        "evidenceIds": evidence_ids,
                        "unsupportedFields": ["trigger", "causalChain"],
                        "missingEvidence": ["Inspect the gateway route and connection result."],
                        "summary": "Impact is visible but the causal path is incomplete.",
                    }
                )
            return json.dumps(
                {
                    "status": "valid",
                    "evidenceIds": evidence_ids,
                    "unsupportedFields": [],
                    "missingEvidence": [],
                    "summary": "The added gateway evidence supports the causal path.",
                }
            )
        return await super().ainvoke(input)


class ValidationGapLlmProvider:
    def __init__(self) -> None:
        self.model = ValidationGapChatModel()

    def create_chat_model(self) -> ValidationGapChatModel:
        return self.model


class ProposalMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def discover_tools(self) -> list[McpToolDefinition]:
        return [
            McpToolDefinition(
                "InspectSignal",
                "Read one safe diagnostic signal.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            McpToolDefinition(
                "ProposeMitigation",
                "Record a side-effect-free mitigation proposal.",
                {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "humanApprovalRequired": {"type": "boolean", "const": True},
                    },
                    "required": ["target", "humanApprovalRequired"],
                    "additionalProperties": False,
                },
            ),
        ]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, dict(arguments)))
        if name == "InspectSignal":
            return {"benchmarkEvidenceId": "signal-confirmed", "confirmed": True}
        if name == "ProposeMitigation":
            return {"accepted": True, "humanApprovalRequired": True}
        raise AssertionError(f"Unexpected tool: {name}")


class ProposalChatModel:
    async def ainvoke(self, input: object) -> str:
        prompt = str(input)
        evidence_ids = list(dict.fromkeys(re.findall(r"evidence_[0-9a-f]+", prompt)))
        if "bounded diagnostic plan" in prompt:
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "inspect-signal",
                            "tool": "InspectSignal",
                            "arguments": {},
                            "purpose": "Confirm the public incident signal.",
                            "testsHypotheses": ["signal_failure"],
                            "causalIntent": "context",
                        }
                    ]
                }
            )
        if "observation decision" in prompt:
            return json.dumps(
                {
                    "purpose": "Confirm the public incident signal.",
                    "supports": ["signal_failure"],
                    "refutes": [],
                    "summary": "The incident signal is confirmed.",
                }
            )
        if "evidence sufficiency decision" in prompt:
            return json.dumps(
                {
                    "status": "sufficient",
                    "evidenceIds": evidence_ids,
                    "supportedHypotheses": ["signal_failure"],
                    "refutedHypotheses": [],
                    "unresolvedHypotheses": [],
                    "missingEvidence": [],
                    "recommendedTools": [],
                    "summary": "The signal and impact are directly observed.",
                }
            )
        if "root-cause decision" in prompt:
            return json.dumps(
                {
                    "component": "test-service",
                    "mechanism": "signal_failure",
                    "trigger": "confirmed_signal",
                    "causalChain": ["signal failed", "service impact observed"],
                    "evidenceIds": evidence_ids,
                    "confidence": 0.92,
                }
            )
        if "root-cause validation decision" in prompt:
            return json.dumps(
                {
                    "status": "valid",
                    "evidenceIds": evidence_ids,
                    "unsupportedFields": [],
                    "missingEvidence": [],
                    "summary": "The decision is grounded.",
                }
            )
        if "structured recovery plan" in prompt:
            return json.dumps(
                {
                    "mode": "proposal_only",
                    "action": "propose_mitigation",
                    "target": "test-service",
                    "rationale": "The grounded signal supports a reviewed mitigation.",
                    "tool": "ProposeMitigation",
                    "arguments": {
                        "target": "test-service",
                        "humanApprovalRequired": True,
                    },
                    "risk": "The mitigation may change request behavior after approval.",
                    "rollback": "Restore the prior service policy.",
                    "verificationSteps": [
                        "Repeat the signal check.",
                        "Confirm service health after an approved action.",
                    ],
                    "evidenceIds": evidence_ids,
                    "decisionConfidence": 0.92,
                    "humanApprovalRequired": True,
                }
            )
        return (
            "# 告警分析报告\n\n## 📋 活跃告警清单\n\n"
            "- SignalFailure\n\n## 📊 结论\n\n提案已记录。"
        )


class ProposalLlmProvider:
    def __init__(self) -> None:
        self.model = ProposalChatModel()

    def create_chat_model(self) -> ProposalChatModel:
        return self.model


@pytest.mark.asyncio
async def test_workflow_replans_from_an_explicit_evidence_gap(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-003")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-003" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-gap-replan",
            status="accepted",
            query="Investigate checkout HTTP 502",
            input_payload={
                "alert": scenario.alert,
                "hypotheses": [
                    {"id": item.id, "description": item.description}
                    for item in scenario.hypotheses
                ],
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, ContractReplanningLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        events = [
            event
            async for event in service.stream(
                task=task,
                accessible_knowledge_base_ids=(),
            )
        ]
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert events[-1]["type"] == "complete"
    assert [item.tool_name for item in snapshot.observations] == [
        "InspectContainer",
    ]
    assert [step.phase for step in steps] == [
        "planner",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "replanner",
        "decision",
        "decision_validation",
        "recovery_planning",
        "policy_gate",
        "report",
    ]
    replanner = next(step for step in steps if step.phase == "replanner")
    assert replanner.payload["reason"] == "evidence_gap"
    assert replanner.payload["addedStepCount"] == 0
    assert replanner.payload["replanCount"] == 1
    validation = next(step for step in steps if step.phase == "decision_validation")
    assert validation.payload["status"] == "invalid"
    assert validation.payload["validationErrorCategory"] == "deterministic_gap"
    policy = next(step for step in steps if step.phase == "policy_gate")
    assert policy.payload["executionPermitted"] is False


@pytest.mark.asyncio
async def test_apy_013_routes_only_validation_to_dedicated_validator(
) -> None:
    provider = DedicatedValidatorLlmProvider()
    repositories = RecordingMemoryRepositories()
    service = AiopsDiagnosticService(
        repositories=cast(Any, repositories),
        llm_provider=cast(LlmProvider, provider),
        retrieval_tool=cast(Any, EmptyRetrieval()),
        mcp_client=cast(Any, object()),
        cls_region="unused",
        cls_topic_id="unused",
    )
    trigger = "Concurrent transactions acquired rows in opposite order."
    mechanism = "A two-session wait cycle formed."
    impact = "PostgreSQL emitted SQLSTATE 40P01."
    state: dict[str, object] = {
        "owner_user_id": "benchmark-user",
        "task_id": "diagnostic-dedicated-validator",
        "evidence_ids": ["evidence_a1", "evidence_b2", "evidence_c3"],
        "root_cause_decision": {
            "component": "order-service",
            "mechanism": "opposite_order_transaction_deadlock",
            "trigger": trigger,
            "causalChain": [trigger, mechanism, impact],
            "evidenceIds": ["evidence_a1", "evidence_b2", "evidence_c3"],
            "confidence": 0.96,
        },
        "public_hypotheses": [
            {"id": "postgres_deadlock"},
            {"id": "postgres_lock_wait"},
            {"id": "postgres_slow_query"},
        ],
        "hypothesis_states": [
            {"id": "postgres_deadlock", "status": "supported"},
            {"id": "postgres_lock_wait", "status": "refuted"},
            {"id": "postgres_slow_query", "status": "refuted"},
        ],
        "observation_decisions": [
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["evidence_a1"],
                "summary": trigger,
                "causalRole": "trigger",
            },
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["evidence_b2"],
                "summary": mechanism,
                "causalRole": "mechanism",
            },
            {
                "supports": ["postgres_deadlock"],
                "evidenceIds": ["evidence_c3"],
                "summary": impact,
                "causalRole": "impact",
            },
        ],
        "decision_vocabulary": {
            "labelsByHypothesis": {
                "postgres_deadlock": {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                }
            }
        },
        "evidence_sufficiency": {"recommendedTools": []},
        "tool_definitions": (),
        "evidence": [],
        "replan_count": 0,
        "max_replans": 0,
        "plan_index": 0,
        "plan": [],
    }

    result = await service._decision_validator(cast(Any, state))  # pyright: ignore[reportPrivateUsage]

    assert provider.validator_model.wrapper_calls == 1
    assert len(provider.validator_model.inputs) == 1
    assert not any(
        "root-cause validation decision" in prompt
        for prompt in provider.main_model.inputs
    )
    validator_prompt = provider.validator_model.inputs[0]
    assert "JSON" in validator_prompt
    assert '"status":"valid"' in validator_prompt
    assert "ground_truth" not in validator_prompt.casefold()
    assert "oracle" not in validator_prompt.casefold()
    step_payload = cast(dict[str, object], repositories.diagnostics.steps[0]["payload"])
    assert step_payload["validationModel"] == "qwen3.8-max"
    assert step_payload["validationErrorCodes"] == []
    checkpoint_payload = cast(
        dict[str, object],
        repositories.diagnostics.checkpoints[0]["checkpoint_payload"],
    )
    assert checkpoint_payload["validationModel"] == "qwen3.8-max"
    assert checkpoint_payload["validationErrorCodes"] == []
    returned = cast(dict[str, object], result["decision_validation"])
    assert returned["validationModel"] == "qwen3.8-max"
    assert returned["validationErrorCodes"] == []


def _semantic_expression_gap_state() -> dict[str, object]:
    return {
        "owner_user_id": "benchmark-user",
        "task_id": "diagnostic-semantic-expression-gap",
        "evidence_ids": ["evidence_a1", "evidence_b2", "evidence_c3"],
        "root_cause_decision": {
            "component": "postgresql",
            "mechanism": "slow_transaction_pool_exhaustion",
            "trigger": "A reporting transaction retained a lock for more than 428 seconds.",
            "causalChain": [
                "The long transaction held a database lock.",
                "Blocked requests retained every pooled connection.",
                "Pool acquisition attempts timed out.",
            ],
            "evidenceIds": ["evidence_a1", "evidence_b2", "evidence_c3"],
            "confidence": 0.96,
        },
        "public_hypotheses": [{"id": "slow_database_work"}],
        "hypothesis_states": [{"id": "slow_database_work", "status": "supported"}],
        "observation_decisions": [
            {
                "supports": ["slow_database_work"],
                "evidenceIds": ["evidence_a1"],
                "summary": "reporting-worker has held a transactionid lock for 428 seconds.",
                "causalRole": "trigger",
            },
            {
                "supports": ["slow_database_work"],
                "evidenceIds": ["evidence_b2"],
                "summary": "The pool is 20/20 checked out while borrowers wait on a DB lock.",
                "causalRole": "mechanism",
            },
            {
                "supports": ["slow_database_work"],
                "evidenceIds": ["evidence_c3"],
                "summary": "There were 37 pool acquire timeouts and a 21 percent error rate.",
                "causalRole": "impact",
            },
        ],
        "decision_vocabulary": {
            "labelsByHypothesis": {
                "slow_database_work": {
                    "component": "postgresql",
                    "mechanism": "slow_transaction_pool_exhaustion",
                }
            }
        },
        "evidence_sufficiency": {"recommendedTools": []},
        "tool_definitions": (),
        "evidence": [],
        "replan_count": 0,
        "max_replans": 0,
        "plan_index": 0,
        "plan": [],
    }


@pytest.mark.asyncio
async def test_semantic_expression_gap_reaches_dedicated_validator() -> None:
    provider = DedicatedValidatorLlmProvider()
    repositories = RecordingMemoryRepositories()
    service = AiopsDiagnosticService(
        repositories=cast(Any, repositories),
        llm_provider=cast(LlmProvider, provider),
        retrieval_tool=cast(Any, EmptyRetrieval()),
        mcp_client=cast(Any, object()),
        cls_region="unused",
        cls_topic_id="unused",
    )

    result = await service._decision_validator(  # pyright: ignore[reportPrivateUsage]
        cast(Any, _semantic_expression_gap_state())
    )

    assert provider.validator_model.wrapper_calls == 1
    assert len(provider.validator_model.inputs) == 1
    prompt = provider.validator_model.inputs[0]
    assert "every candidate field" in prompt
    assert "every causalChain fact" in prompt
    validation = cast(dict[str, object], result["decision_validation"])
    assert validation["status"] == "valid"
    assert validation["validationOrigin"] == "llm_confirmed"
    assert result["root_cause_decision"] is not None


@pytest.mark.asyncio
async def test_semantic_expression_gap_fails_closed_when_validator_is_unavailable() -> None:
    class TimeoutValidatorModel(RecordingDedicatedValidatorModel):
        async def ainvoke(self, input: object) -> object:
            self.inputs.append(str(input))
            raise TimeoutError("validator provider timeout")

    provider = DedicatedValidatorLlmProvider()
    provider.validator_model = TimeoutValidatorModel()
    repositories = RecordingMemoryRepositories()
    service = AiopsDiagnosticService(
        repositories=cast(Any, repositories),
        llm_provider=cast(LlmProvider, provider),
        retrieval_tool=cast(Any, EmptyRetrieval()),
        mcp_client=cast(Any, object()),
        cls_region="unused",
        cls_topic_id="unused",
    )

    result = await service._decision_validator(  # pyright: ignore[reportPrivateUsage]
        cast(Any, _semantic_expression_gap_state())
    )

    assert provider.validator_model.wrapper_calls == 1
    validation = cast(dict[str, object], result["decision_validation"])
    assert validation["status"] == "invalid"
    assert validation["validationErrorCategory"] == "model_call_failed"
    assert validation["validationWarning"] == "llm_validator_unavailable"
    assert validation["validationOrigin"] == "none"
    assert result["root_cause_decision"] is None


@pytest.mark.asyncio
async def test_apy_013_validator_unavailable_uses_grounded_fallback(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=f"diagnostic-apy-013-validator-unavailable-{uuid4().hex}",
            status="accepted",
            query=scenario.title,
            input_payload=build_application_diagnostic_input(scenario),
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, UnavailablePostgresValidatorLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        async for _ in service.stream(
            task=task,
            accessible_knowledge_base_ids=(),
        ):
            pass
        completed = await repositories.diagnostics.get_task(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert completed is not None
    assert [item.tool_name for item in snapshot.observations] == [
        "InspectPostgresErrors",
        "InspectPostgresWaitGraph",
        "InspectTransactionResourceOrder",
    ]
    assert not any(
        item.tool_name == "GetDatabaseMetrics" for item in snapshot.observations
    )
    assert len([step for step in steps if step.phase == "decision"]) == 1
    assert not any(
        step.phase == "replanner"
        and step.payload.get("reason") == "decision_validation_gap"
        for step in steps
    )
    validation = next(step for step in steps if step.phase == "decision_validation")
    assert validation.payload["status"] == "valid"
    assert (
        validation.payload["validationOrigin"]
        == "deterministic_grounded_fallback"
    )
    assert validation.payload["validationErrorCategory"] == "model_call_failed"
    assert validation.payload["validationErrorCode"] == "timeout"
    assert validation.payload["validationErrorPhase"] == "model_invoke"
    assert validation.payload["validationRetryable"] is True
    assert validation.payload["validationHttpStatusClass"] is None
    assert validation.payload["validationAttempts"] == 1
    assert validation.payload["validationModel"] == "legacy-main-model"
    assert validation.payload["validationErrorCodes"] == ["timeout"]
    assert validation.payload["validationWarning"] == "llm_validator_unavailable"
    assert completed.result_payload["rootCauseDecision"] is not None
    recovery_plan = cast(dict[str, object], completed.result_payload["recoveryPlan"])
    recovery_policy = cast(dict[str, object], completed.result_payload["recoveryPolicy"])
    assert recovery_plan["mode"] == "manual_review"
    assert recovery_policy["executionPermitted"] is False


@pytest.mark.asyncio
async def test_apy_013_missing_candidate_fails_closed_without_replanning(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=f"diagnostic-apy-013-candidate-missing-{uuid4().hex}",
            status="accepted",
            query=scenario.title,
            input_payload=build_application_diagnostic_input(scenario),
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, MissingPostgresCandidateLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        async for _ in service.stream(
            task=task,
            accessible_knowledge_base_ids=(),
        ):
            pass
        completed = await repositories.diagnostics.get_task(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert completed is not None
    validation = next(step for step in steps if step.phase == "decision_validation")
    assert validation.payload["status"] == "invalid"
    assert validation.payload["validationOrigin"] == "none"
    assert validation.payload["validationErrorCategory"] == "candidate_missing"
    assert validation.payload["nextRoute"] == "recovery_planner"
    assert completed.result_payload["rootCauseDecision"] is None
    replanners = [step for step in steps if step.phase == "replanner"]
    assert len(replanners) == 2
    assert all(step.payload["reason"] == "evidence_gap" for step in replanners)
    assert all(step.payload["addedStepCount"] == 0 for step in replanners)
    recovery_policy = cast(dict[str, object], completed.result_payload["recoveryPolicy"])
    assert recovery_policy["executionPermitted"] is False


@pytest.mark.asyncio
async def test_apy_013_unavailable_validator_keeps_normalized_grounded_candidate(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=f"diagnostic-apy-013-ungrounded-{uuid4().hex}",
            status="accepted",
            query=scenario.title,
            input_payload=build_application_diagnostic_input(scenario),
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(
                LlmProvider,
                UngroundedUnavailableValidatorLlmProvider(),
            ),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        async for _ in service.stream(
            task=task,
            accessible_knowledge_base_ids=(),
        ):
            pass
        completed = await repositories.diagnostics.get_task(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert completed is not None
    validation = next(step for step in steps if step.phase == "decision_validation")
    assert validation.payload["status"] == "valid"
    assert validation.payload["validationOrigin"] == "deterministic_grounded_fallback"
    assert validation.payload["validationErrorCategory"] == "model_call_failed"
    assert validation.payload["nextRoute"] == "recovery_planner"
    assert completed.result_payload["rootCauseDecision"] is not None
    assert not any(step.phase == "replanner" for step in steps)
    recovery_policy = cast(dict[str, object], completed.result_payload["recoveryPolicy"])
    assert recovery_policy["executionPermitted"] is False


@pytest.mark.asyncio
async def test_apy_013_sufficient_cycle_collects_four_relevant_exact_calls_and_a_decision(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-013")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-apy-013-contract-regression",
            status="accepted",
            query=scenario.title,
            input_payload=build_application_diagnostic_input(scenario),
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, PostgresContractAcceptanceLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        async for _ in service.stream(task=task, accessible_knowledge_base_ids=()):
            pass
        completed = await repositories.diagnostics.get_task(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert completed is not None
    assert [observation.tool_name for observation in snapshot.observations] == [
        "InspectPostgresErrors",
        "InspectPostgresWaitGraph",
        "GetDatabaseMetrics",
        "InspectTransactionResourceOrder",
    ]
    assert all(
        observation.arguments["windowMinutes"] == 15
        for observation in snapshot.observations
    )
    assert snapshot.observations[1].arguments["database"] == "agent_py"
    assert {item.evidence_id for item in snapshot.observations} == {
        "postgres-40p01-deadlock-record",
        "postgres-deadlock-cycle",
        "postgres-capacity-distractor",
        "postgres-opposite-resource-order",
    }
    assert not any(
        step.phase == "executor" and "errorCategory" in step.payload
        for step in steps
    )
    assert not any(
        step.payload.get("terminationReason") == "step_budget_exhausted"
        for step in steps
    )
    assert any(
        step.phase == "sufficiency_gate"
        and step.payload.get("refinementReason")
        == "missing_causal_role_plan_step_remaining"
        for step in steps
    )
    assert any(
        step.phase == "sufficiency_gate"
        and step.payload.get("refinementReason")
        == "open_hypothesis_plan_step_remaining"
        for step in steps
    )
    decision = next(step for step in steps if step.phase == "decision")
    validations = [step for step in steps if step.phase == "decision_validation"]
    decisions = [step for step in steps if step.phase == "decision"]
    assert len(decisions) == 1
    assert len(validations) == 1
    assert not any(
        step.phase == "replanner"
        and step.payload.get("reason") == "decision_validation_gap"
        for step in steps
    )
    root_cause = cast(dict[str, object], decision.payload["rootCauseDecision"])
    assert decision.payload["decisionOrigin"] == "llm_grounded_normalization"
    assert root_cause["causalChain"] == [
        "Concurrent transactions acquired order rows and inventory rows in opposite order.",
        "A two-session cycle exists without an ordinary blocker.",
        "PostgreSQL aborted one transaction with SQLSTATE 40P01.",
    ]
    assert len(cast(list[object], root_cause["causalChain"])) == 3
    assert validations[0].payload["status"] == "valid", validations[0].payload[
        "deterministicChecks"
    ]
    recovery_policy = cast(dict[str, object], completed.result_payload["recoveryPolicy"])
    assert recovery_policy["executionPermitted"] is False
    assert completed.status == "succeeded"


@pytest.mark.asyncio
async def test_duplicate_plan_step_is_filtered_before_executor(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-003")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-003" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-duplicate-step",
            status="accepted",
            query="Investigate checkout HTTP 502",
            input_payload={
                "alert": scenario.alert,
                "hypotheses": [
                    {"id": item.id, "description": item.description}
                    for item in scenario.hypotheses
                ],
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, DuplicateStepLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
            tool_argument_contracts=snapshot.tool_argument_contracts,
        )

        async for _ in service.stream(task=task, accessible_knowledge_base_ids=()):
            pass
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    executor_steps = [step for step in steps if step.phase == "executor"]
    assert [item.tool_name for item in snapshot.observations] == [
        "InspectContainer",
    ]
    assert len(executor_steps) == 1
    assert all("errorCategory" not in step.payload for step in executor_steps)


@pytest.mark.asyncio
async def test_invalid_causal_coverage_cannot_be_overridden_by_validator(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-003")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-003" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-validation-replan",
            status="accepted",
            query="Investigate checkout HTTP 502",
            input_payload={
                "alert": scenario.alert,
                "hypotheses": [
                    {"id": item.id, "description": item.description}
                    for item in scenario.hypotheses
                ],
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, ValidationGapLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
        )

        async for _ in service.stream(task=task, accessible_knowledge_base_ids=()):
            pass
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    validations = [step for step in steps if step.phase == "decision_validation"]
    replanner = next(step for step in steps if step.phase == "replanner")
    assert [item.payload["status"] for item in validations] == ["invalid"]
    assert validations[0].payload["validationErrorCategory"] == "deterministic_gap"
    assert (
        validations[0].payload["missingEvidence"]
        or validations[0].payload["unsupportedFields"]
    )
    assert len([step for step in steps if step.phase == "replanner"]) == 1
    assert replanner.payload["reason"] == "evidence_gap"
    assert [item.tool_name for item in snapshot.observations] == [
        "InspectContainer",
    ]


@pytest.mark.asyncio
async def test_policy_gate_does_not_record_proposal_without_grounded_cause(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    client = ProposalMcpClient()
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-proposal-policy",
            status="accepted",
            query="Investigate a test signal",
            input_payload={
                "alert": {"alertname": "SignalFailure"},
                "hypotheses": [
                    {"id": "signal_failure", "description": "The signal failed."}
                ],
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, ProposalLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=cast(Any, client),
            cls_region="unused",
            cls_topic_id="unused",
            tool_policies={"ProposeMitigation": "proposal_only"},
        )

        async for _ in service.stream(task=task, accessible_knowledge_base_ids=()):
            pass
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        audits = await cast(Any, repositories.tool_call_audits).list_for_diagnostic_task(
            owner_user_id=task.owner_user_id,
            diagnostic_task_id=task.id,
        )
    finally:
        await engine.dispose()

    policy = next(step for step in steps if step.phase == "policy_gate")
    assert [name for name, _ in client.calls] == ["InspectSignal"]
    assert policy.payload["status"] == "deferred"
    assert policy.payload["authorizationCode"] == "no_grounded_action"
    assert policy.payload["executionPermitted"] is False
    assert policy.payload["proposalRecorded"] is False
    assert [audit.tool_name for audit in audits] == [
        "knowledge_retrieval",
        "InspectSignal",
    ]


@pytest.mark.asyncio
async def test_policy_gate_denies_a_proposal_without_a_request_policy(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    client = ProposalMcpClient()
    try:
        repositories = create_sqlalchemy_memory_repositories(
            create_memory_session_factory(engine)
        )
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-proposal-denied",
            status="accepted",
            query="Investigate a test signal",
            input_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, ProposalLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=cast(Any, client),
            cls_region="unused",
            cls_topic_id="unused",
        )
        await service._policy_gate(  # pyright: ignore[reportPrivateUsage]
            cast(
                Any,
                {
                    "owner_user_id": task.owner_user_id,
                    "task_id": task.id,
                    "recovery_plan": {
                        "mode": "proposal_only",
                        "action": "propose_mitigation",
                        "target": "test-service",
                        "rationale": "Record a reviewed mitigation.",
                        "tool": "ProposeMitigation",
                        "arguments": {
                            "target": "test-service",
                            "humanApprovalRequired": True,
                        },
                        "risk": "The later action may change request behavior.",
                        "rollback": "Restore the prior service policy.",
                        "verificationSteps": ["Repeat the check.", "Confirm health."],
                        "evidenceIds": ["evidence-public"],
                        "decisionConfidence": 0.92,
                        "humanApprovalRequired": True,
                    },
                },
            )
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    denied = next(step for step in steps if step.phase == "policy_gate")
    assert denied.payload["status"] == "denied"
    assert denied.payload["authorizationCode"] == "proposal_tool_not_allowed"
    assert denied.payload["executionPermitted"] is False
    assert denied.payload["proposalRecorded"] is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_workflow_persists_updates_and_rejects_incomplete_causal_decision(
    migrated_database_url: str,
) -> None:
    scenario = load_public_scenario(SCENARIOS / "APY-003")
    snapshot = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-003" / scenario.snapshot_file
    )
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(create_memory_session_factory(engine))
        task = await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id="diagnostic-reasoning-trace",
            status="accepted",
            query="Investigate checkout HTTP 502",
            input_payload={
                "alert": scenario.alert,
                "hypotheses": [
                    {"id": item.id, "description": item.description}
                    for item in scenario.hypotheses
                ],
            },
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, ReasoningLlmProvider()),
            retrieval_tool=EmptyRetrieval(),
            mcp_client=snapshot,
            cls_region="unused",
            cls_topic_id="unused",
        )

        events = [
            event
            async for event in service.stream(
                task=task,
                accessible_knowledge_base_ids=(),
            )
        ]
        steps = await repositories.diagnostics.list_steps(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
        reports = await repositories.diagnostics.list_reports(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
        )
    finally:
        await engine.dispose()

    assert [item.tool_name for item in snapshot.observations] == [
        "InspectContainer",
        "InspectNginx",
    ]
    assert [step.phase for step in steps] == [
        "planner",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "replanner",
        "decision",
        "decision_validation",
        "recovery_planning",
        "policy_gate",
        "report",
    ]
    assert reports[0].payload["rootCauseDecision"] is None
    validation = next(step for step in steps if step.phase == "decision_validation")
    assert validation.payload["status"] == "invalid"
    assert validation.payload["validationErrorCategory"] == "deterministic_gap"
    assert all(
        cast(list[str], step.payload["evidenceIds"])
        for step in steps
        if step.phase == "evidence_evaluation"
    )
    assert events[-1]["type"] == "complete"
