import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from super_ai.aiops import AiopsDiagnosticService
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
from super_ai.llm import LlmProvider
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.retrieval import KnowledgeRetrievalToolInput, KnowledgeRetrievalToolResult

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"


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
        "replanner",
        "executor",
        "evidence_evaluation",
        "replanner",
        "decision",
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
