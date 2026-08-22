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
        )


class MutatingRunner(SafeFakeConversationRunner):
    def __init__(self, gate: str) -> None:
        self.gate = gate

    async def evaluate(
        self, scenario: ConversationEvalScenario
    ) -> ConversationEvalObservation:
        observation = await super().evaluate(scenario)
        if scenario.id != "CHAT-SEC-002":
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
    assert result.target_extraction == 1.0
    assert result.allowed_tool_precision == 1.0
    assert result.task_completion == 1.0
    assert result.grounding == 1.0
    assert result.idempotency == 1.0
    assert result.cross_tenant_isolation == 1.0
    assert result.recovery_safety == 1.0
    assert result.structured_safety_fidelity == 1.0
    assert result.reasoning_leakage_count == 0
    assert result.sse_replay_correctness == 1.0
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
