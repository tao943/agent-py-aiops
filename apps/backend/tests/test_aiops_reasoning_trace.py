import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from super_ai.aiops import AiopsDiagnosticService
from super_ai.aiops.diagnostics import (
    _supported_refinement_index,
    normalize_tool_plan_steps,
)
from super_ai.aiops.reasoning import (
    RootCauseDecision,
    normalize_root_cause_decision,
    parse_evidence_sufficiency,
    parse_observation_decision,
    parse_plan,
    parse_recovery_plan,
    parse_root_cause_decision,
    parse_root_cause_validation,
)
from super_ai.evaluation import SnapshotMcpClient, load_public_scenario
from super_ai.evaluation.runner import build_application_diagnostic_input
from super_ai.llm import LlmProvider
from super_ai.mcp.tool_arguments import constrain_tool_definitions, tool_step_fingerprint
from super_ai.mcp_client import McpToolDefinition
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalToolInput, KnowledgeRetrievalToolResult

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"


def _refinement_step(
    step_id: str,
    tool: str,
    hypotheses: list[str],
) -> dict[str, object]:
    return {
        "id": step_id,
        "tool": tool,
        "arguments": {"scope": step_id},
        "purpose": f"Inspect {step_id}.",
        "testsHypotheses": hypotheses,
    }


def test_supported_refinement_selects_first_remaining_matching_step() -> None:
    metrics = _refinement_step("metrics", "GetDatabaseMetrics", ["postgres_slow_query"])
    resource_order = _refinement_step(
        "resource-order",
        "InspectTransactionResourceOrder",
        ["postgres_deadlock"],
    )
    second_match = _refinement_step(
        "second-match",
        "InspectPostgresWaitGraph",
        ["postgres_deadlock"],
    )

    assert _supported_refinement_index(
        plan=[metrics, resource_order, second_match],
        plan_index=0,
        supported_hypotheses={"postgres_deadlock"},
        executed_fingerprints=set(),
    ) == 1


def test_supported_refinement_skips_executed_match() -> None:
    executed = _refinement_step(
        "resource-order",
        "InspectTransactionResourceOrder",
        ["postgres_deadlock"],
    )
    remaining = _refinement_step(
        "wait-graph",
        "InspectPostgresWaitGraph",
        ["postgres_deadlock"],
    )

    assert _supported_refinement_index(
        plan=[executed, remaining],
        plan_index=0,
        supported_hypotheses={"postgres_deadlock"},
        executed_fingerprints={
            tool_step_fingerprint("InspectTransactionResourceOrder", {"scope": "resource-order"})
        },
    ) == 1


def test_supported_refinement_does_not_revisit_plan_prefix() -> None:
    previous = _refinement_step(
        "previous",
        "InspectPostgresErrors",
        ["postgres_deadlock"],
    )
    unrelated = _refinement_step(
        "metrics",
        "GetDatabaseMetrics",
        ["postgres_slow_query"],
    )

    assert _supported_refinement_index(
        plan=[previous, unrelated],
        plan_index=1,
        supported_hypotheses={"postgres_deadlock"},
        executed_fingerprints=set(),
    ) is None


def test_supported_refinement_ignores_non_supported_candidates() -> None:
    lock_wait = _refinement_step(
        "lock-wait",
        "InspectPostgresWaitGraph",
        ["postgres_lock_wait"],
    )
    slow_query = _refinement_step(
        "metrics",
        "GetDatabaseMetrics",
        ["postgres_slow_query"],
    )

    assert _supported_refinement_index(
        plan=[lock_wait, slow_query],
        plan_index=0,
        supported_hypotheses={"postgres_deadlock"},
        executed_fingerprints=set(),
    ) is None


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
            '"purpose":"inspect","testsHypotheses":["invented"]}]}',
            available_tools={"InspectContainer"},
            known_hypotheses={"process-down"},
        )


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

        async def run_gate(task_id: str, *, attempts: int) -> tuple[dict[str, object], object]:
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
                        "plan": [metrics, resource_order],
                        "plan_index": 0,
                        "evidence_ids": ["ev-cycle"],
                        "public_hypotheses": [
                            {"id": item.id, "description": item.description}
                            for item in scenario.hypotheses
                        ],
                        "hypothesis_states": [],
                        "observation_decisions": [],
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
            return cast(dict[str, object], update), steps[-1]

        update, gate_step = await run_gate("gate-with-refinement", attempts=2)
        exhausted, exhausted_step = await run_gate("gate-without-budget", attempts=6)
    finally:
        await engine.dispose()

    assert update["next_route"] == "executor"
    assert update["plan_index"] == 1
    assert update["termination_reason"] == ""
    assert gate_step.payload["nextRoute"] == "executor"
    assert (
        gate_step.payload["refinementReason"]
        == "supported_hypothesis_plan_step_remaining"
    )
    assert exhausted["next_route"] == "decision"
    assert "plan_index" not in exhausted
    assert exhausted_step.payload["refinementReason"] == ""


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
                            "testsHypotheses": ["postgres_deadlock"],
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
                        },
                    ]
                }
            )
        if "gap-targeted diagnostic replan" in prompt:
            return json.dumps({"steps": []})
        if "observation decision" in prompt:
            if "InspectPostgresWaitGraph" in prompt:
                return json.dumps(
                    {
                        "purpose": "Inspect the PostgreSQL wait graph.",
                        "supports": ["postgres_deadlock"],
                        "refutes": ["postgres_lock_wait"],
                        "summary": "A two-session cycle exists without an ordinary blocker.",
                    }
                )
            if "GetDatabaseMetrics" in prompt:
                return json.dumps(
                    {
                        "purpose": "Rule out database capacity and slow-query pressure.",
                        "supports": [],
                        "refutes": ["postgres_slow_query"],
                        "summary": "Latency and capacity remain within the bounded baseline.",
                    }
                )
            return json.dumps(
                {
                    "purpose": "Inspect deadlock evidence.",
                    "supports": ["postgres_deadlock"],
                    "refutes": [],
                    "summary": "The observation contains direct cyclic-dependency evidence.",
                }
            )
        if "evidence sufficiency decision" in prompt:
            self.sufficiency_count += 1
            sufficient = self.sufficiency_count >= 2
            return json.dumps(
                {
                    "status": "sufficient" if sufficient else "insufficient",
                    "evidenceIds": evidence_ids,
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
            return json.dumps(
                {
                    "component": "order-service",
                    "mechanism": "opposite_order_transaction_deadlock",
                    "trigger": (
                        "concurrent_updates_acquired_order_and_inventory_rows_in_reverse_order"
                    ),
                    "causalChain": [
                        "transactions acquired order and inventory rows in opposite order",
                        "each transaction waited for a row held by the other",
                        "PostgreSQL detected the cycle and aborted one transaction",
                    ],
                    "evidenceIds": evidence_ids,
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
                        },
                        {
                            "id": "inspect-container-duplicate",
                            "tool": "InspectContainer",
                            "arguments": {"service": "checkout-service"},
                            "purpose": "Repeat the same process check.",
                            "testsHypotheses": ["upstream_process_down"],
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
        "InspectNginx",
    ]
    assert [step.phase for step in steps] == [
        "planner",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "replanner",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "decision",
        "decision_validation",
        "recovery_planning",
        "policy_gate",
        "report",
    ]
    replanner = next(step for step in steps if step.phase == "replanner")
    assert replanner.payload["reason"] == "evidence_gap"
    assert replanner.payload["addedStepCount"] == 1
    assert replanner.payload["replanCount"] == 1
    validation = next(step for step in steps if step.phase == "decision_validation")
    assert validation.payload["status"] == "valid"
    recovery = next(step for step in steps if step.phase == "recovery_planning")
    policy = next(step for step in steps if step.phase == "policy_gate")
    assert recovery.payload["mode"] == "external_policy_required"
    assert policy.payload["status"] == "deferred"
    assert policy.payload["executionPermitted"] is False


@pytest.mark.asyncio
async def test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision(
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
        "postgres-opposite-resource-order",
    }
    assert all(item.tool_name != "GetDatabaseMetrics" for item in snapshot.observations)
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
        == "supported_hypothesis_plan_step_remaining"
        for step in steps
    )
    decision = next(step for step in steps if step.phase == "decision")
    assert decision.payload["rootCauseDecision"] is not None
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
        "InspectNginx",
    ]
    assert len(executor_steps) == 2
    assert all("errorCategory" not in step.payload for step in executor_steps)


@pytest.mark.asyncio
async def test_invalid_decision_validation_replans_before_reporting(
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
    assert [item.payload["status"] for item in validations] == ["invalid", "valid"]
    assert replanner.payload["reason"] == "decision_validation_gap"
    assert [item.tool_name for item in snapshot.observations] == [
        "InspectContainer",
        "InspectNginx",
    ]


@pytest.mark.asyncio
async def test_policy_gate_records_only_a_whitelisted_proposal_tool(
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
    assert [name for name, _ in client.calls] == ["InspectSignal", "ProposeMitigation"]
    assert policy.payload["status"] == "allowed"
    assert policy.payload["authorizationCode"] == "proposal_recorded"
    assert policy.payload["executionPermitted"] is False
    assert policy.payload["proposalRecorded"] is True
    assert [audit.tool_name for audit in audits] == [
        "knowledge_retrieval",
        "InspectSignal",
        "ProposeMitigation",
    ]
    forbidden_terms = ("write", "reload", "restart", "switch", "signal", "apply")
    assert not any(
        term in audit.tool_name.casefold()
        for audit in audits
        if audit.tool_name == "ProposeMitigation"
        for term in forbidden_terms
    )


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
async def test_workflow_persists_hypothesis_updates_and_grounded_decision(
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
        evidence = await repositories.diagnostics.list_evidence(
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
    decision = cast(dict[str, object], reports[0].payload["rootCauseDecision"])
    assert decision["mechanism"] == "process_unavailable"
    assert set(cast(list[str], decision["evidenceIds"])) <= {item.id for item in evidence}
    assert all(
        cast(list[str], step.payload["evidenceIds"])
        for step in steps
        if step.phase == "evidence_evaluation"
    )
    assert events[-1]["type"] == "complete"
