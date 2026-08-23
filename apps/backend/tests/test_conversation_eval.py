from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from super_ai.chat.evaluation import (
    ConversationEvalObservation,
    ConversationEvalScenario,
    FixtureConversationEvalBridge,
    IntegratedConversationEvalRunner,
    load_conversation_eval_fixtures,
    run_conversation_eval,
)
from super_ai.chat.execution_policy import policy_for
from super_ai.chat.intent import ChatIntentRouter, ChatRoute, KeywordRouterModel

FIXTURES = Path(__file__).parent / "fixtures" / "conversation_eval.json"


class SafeFakeConversationRunner:
    async def evaluate(
        self, scenario: ConversationEvalScenario
    ) -> ConversationEvalObservation:
        expected_safety = scenario.available_resources.get("expectedSafety")
        safety = (
            dict(cast(Mapping[str, object], expected_safety))
            if isinstance(expected_safety, dict)
            else None
        )
        expected_policy = policy_for(
            ChatRoute(
                scenario.expected_intent,
                1.0,
                "rule",
                incident_id=scenario.expected_incident_id,
                diagnostic_task_id=scenario.expected_diagnostic_task_id,
            )
        )
        return ConversationEvalObservation(
            intent=scenario.expected_intent,
            incident_id=scenario.expected_incident_id,
            diagnostic_task_id=scenario.expected_diagnostic_task_id,
            exposed_tools=scenario.expected_tools,
            invoked_tools=scenario.expected_tools[:1],
            completed=True,
            grounding_ids=scenario.expected_grounding_ids,
            idempotency_correct=True,
            cross_tenant_access_count=0,
            automatic_recovery_execution_count=0,
            public_events=(
                {"id": "2", "type": "content.delta", "delta": "public"},
                {"id": "3", "type": "complete"},
            ),
            replayed_event_sequences=(2, 3),
            expected_replay_sequences=(2, 3),
            structured_safety=safety,
            mode=expected_policy.mode,
            required_tools=tuple(sorted(expected_policy.required_tools)),
            direct_read_bypassed_react=expected_policy.mode == "direct_read",
            confirmation_required=expected_policy.mode == "confirmation_required",
        )


class MutatingRunner(SafeFakeConversationRunner):
    def __init__(self, gate: str) -> None:
        self.gate = gate

    async def evaluate(
        self, scenario: ConversationEvalScenario
    ) -> ConversationEvalObservation:
        observation = await super().evaluate(scenario)
        target_scenario = {
            "missing_required_tool_success": "CHAT-GEN-002",
            "unconfirmed_write": "CHAT-START-001",
            "post_budget_call": "CHAT-GEN-001",
            "mode": "CHAT-INC-001",
            "postcondition": "CHAT-GEN-001",
            "direct_bypass": "CHAT-INC-001",
            "confirmation": "CHAT-START-001",
            "context_fidelity": "CHAT-GEN-001",
        }.get(self.gate, "CHAT-SEC-002")
        if scenario.id != target_scenario:
            return observation
        if self.gate == "cross_tenant":
            return replace(observation, cross_tenant_access_count=1)
        if self.gate == "forbidden_tool":
            return replace(
                observation,
                invoked_tools=(*observation.invoked_tools, "execute_recovery"),
            )
        if self.gate == "reasoning":
            return replace(
                observation,
                public_events=(
                    *observation.public_events,
                    {"type": "debug", "reasoning": "private"},
                ),
            )
        if self.gate == "recovery_execution":
            return replace(observation, automatic_recovery_execution_count=1)
        if self.gate == "missing_required_tool_success":
            return replace(observation, invoked_tools=(), completed=True)
        if self.gate == "unconfirmed_write":
            return replace(observation, unconfirmed_write_count=1)
        if self.gate == "post_budget_call":
            return replace(observation, post_budget_call_count=1)
        if self.gate == "mode":
            return replace(observation, mode="bounded_react")
        if self.gate == "postcondition":
            return replace(observation, postcondition_satisfied=False)
        if self.gate == "direct_bypass":
            return replace(observation, direct_read_bypassed_react=False)
        if self.gate == "confirmation":
            return replace(observation, confirmation_required=False)
        if self.gate == "context_fidelity":
            return replace(observation, context_fidelity=False)
        raise AssertionError(f"unknown gate {self.gate}")


class SafetyMismatchRunner(SafeFakeConversationRunner):
    async def evaluate(
        self, scenario: ConversationEvalScenario
    ) -> ConversationEvalObservation:
        observation = await super().evaluate(scenario)
        if scenario.id == "CHAT-REC-001":
            return replace(
                observation,
                structured_safety={
                    "executionPermitted": True,
                    "humanApprovalRequired": False,
                    "recoveryMode": "automatic",
                },
            )
        return observation


def load_scenarios() -> tuple[ConversationEvalScenario, ...]:
    return load_conversation_eval_fixtures(FIXTURES)


@pytest.mark.asyncio
async def test_eval_passes_twelve_bounded_scenarios() -> None:
    result = await run_conversation_eval(
        load_scenarios(),
        runner=IntegratedConversationEvalRunner(
            router=ChatIntentRouter(KeywordRouterModel()),
            bridge=FixtureConversationEvalBridge(),
        ),
    )

    assert result.scenario_count == 12
    assert result.category_counts == {
        "general": 1,
        "knowledge": 1,
        "incident": 2,
        "start": 2,
        "status": 1,
        "evidence": 1,
        "recovery": 2,
        "security": 2,
    }
    assert result.intent_accuracy == 1.0
    assert result.mode_accuracy == 1.0
    assert result.target_extraction == 1.0
    assert result.allowed_tool_precision == 1.0
    assert result.required_tool_recall == 1.0
    assert result.task_completion == 1.0
    assert result.postcondition == 1.0
    assert result.direct_bypass == 1.0
    assert result.budget_compliance == 1.0
    assert result.confirmation == 1.0
    assert result.grounding == 1.0
    assert result.context_fidelity == 1.0
    assert result.idempotency == 1.0
    assert result.cross_tenant_isolation == 1.0
    assert result.recovery_safety == 1.0
    assert result.structured_safety_fidelity == 1.0
    assert result.reasoning_leakage_count == 0
    assert result.sse_replay_correctness == 1.0
    assert result.hard_gates.passed is True
    assert result.passed is True


@pytest.mark.parametrize(
    "gate",
    ["cross_tenant", "forbidden_tool", "reasoning", "recovery_execution"],
)
@pytest.mark.asyncio
async def test_any_security_gate_failure_fails_suite(gate: str) -> None:
    result = await run_conversation_eval(load_scenarios(), runner=MutatingRunner(gate))

    assert result.passed is False
    assert gate in result.failed_hard_gates


@pytest.mark.parametrize(
    "gate",
    ["missing_required_tool_success", "unconfirmed_write", "post_budget_call"],
)
@pytest.mark.asyncio
async def test_any_execution_gate_failure_fails_suite(gate: str) -> None:
    result = await run_conversation_eval(load_scenarios(), runner=MutatingRunner(gate))

    assert result.passed is False
    assert gate in result.failed_hard_gates


@pytest.mark.parametrize(
    ("mutation", "metric"),
    [
        ("mode", "mode_accuracy"),
        ("postcondition", "postcondition"),
        ("direct_bypass", "direct_bypass"),
        ("confirmation", "confirmation"),
        ("context_fidelity", "context_fidelity"),
    ],
)
@pytest.mark.asyncio
async def test_incorrect_execution_contract_fails_suite(
    mutation: str, metric: str
) -> None:
    result = await run_conversation_eval(
        load_scenarios(), runner=MutatingRunner(mutation)
    )

    assert getattr(result, metric) < 1.0
    assert result.passed is False


@pytest.mark.asyncio
async def test_structured_safety_mismatch_is_a_hard_gate() -> None:
    result = await run_conversation_eval(load_scenarios(), runner=SafetyMismatchRunner())

    assert result.passed is False
    assert result.structured_safety_mismatch_count == 1
    assert "safety_mismatch" in result.failed_hard_gates


class AlwaysGeneralRouter:
    async def route(self, content: str) -> ChatRoute:
        del content
        return ChatRoute("general_chat", 1.0, "model")


@pytest.mark.asyncio
async def test_integrated_eval_observes_real_router_failure() -> None:
    result = await run_conversation_eval(
        load_scenarios(),
        runner=IntegratedConversationEvalRunner(
            router=AlwaysGeneralRouter(),
            bridge=FixtureConversationEvalBridge(),
        ),
    )

    assert result.intent_accuracy < 1.0
    assert result.passed is False


def test_fixture_loader_rejects_wrong_distribution(tmp_path: Path) -> None:
    malformed = tmp_path / "conversation_eval.json"
    malformed.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly 12"):
        load_conversation_eval_fixtures(malformed)
