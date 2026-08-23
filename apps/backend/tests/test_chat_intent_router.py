from __future__ import annotations

from collections.abc import Mapping

import pytest

from super_ai.chat.intent import ChatIntentRouter, LlmStructuredRouterModel
from super_ai.chat.tool_policy import allowed_tools_for


class FakeRouterModel:
    def __init__(self, result: Mapping[str, object] | Exception) -> None:
        self.result = result
        self.calls = 0

    async def route(self, content: str) -> Mapping[str, object]:
        del content
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, input: object) -> object:
        del input
        return type("Response", (), {"content": self.content})()


@pytest.mark.asyncio
async def test_explicit_diagnostic_task_id_routes_without_model() -> None:
    model = FakeRouterModel(AssertionError("model must not run"))

    route = await ChatIntentRouter(model).route("查看 diagnostic_abc123 的报告和证据")

    assert route.intent == "diagnostic_status"
    assert route.diagnostic_task_id == "diagnostic_abc123"
    assert route.source == "rule"
    assert route.needs_clarification is False
    assert model.calls == 0


@pytest.mark.asyncio
async def test_explicit_recovery_request_beats_diagnostic_status_rule() -> None:
    model = FakeRouterModel(AssertionError("model must not run"))

    route = await ChatIntentRouter(model).route(
        "为 diagnostic_owner_008 的恢复方案创建人工审批"
    )

    assert route.intent == "recovery_request"
    assert route.diagnostic_task_id == "diagnostic_owner_008"
    assert route.source == "rule"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_low_confidence_model_route_requires_clarification() -> None:
    model = FakeRouterModel(
        {
            "intent": "start_diagnostic",
            "confidence": 0.69,
            "incidentId": "incident_123",
            "diagnosticTaskId": None,
            "needsClarification": False,
        }
    )

    route = await ChatIntentRouter(model).route("帮我处理一下")

    assert route.intent == "start_diagnostic"
    assert route.incident_id == "incident_123"
    assert route.source == "model"
    assert route.needs_clarification is True


@pytest.mark.asyncio
async def test_invalid_model_output_falls_back_without_privileged_intent() -> None:
    model = FakeRouterModel({"intent": "execute_recovery", "confidence": 1.0})

    route = await ChatIntentRouter(model).route("do something")

    assert route.intent == "general_chat"
    assert route.source == "fallback"
    assert route.confidence == 0.0


def test_recovery_intent_only_exposes_approval_tool() -> None:
    assert allowed_tools_for("recovery_request") == frozenset(
        {
            "get_diagnostic_status",
            "get_diagnostic_report",
            "create_recovery_approval_request",
        }
    )
    assert "restart_service" not in allowed_tools_for("recovery_request")
    assert "execute_recovery" not in allowed_tools_for("recovery_request")


def test_each_intent_has_a_bounded_tool_allowlist() -> None:
    assert allowed_tools_for("general_chat") == frozenset(
        {"get_current_time", "load_skill"}
    )
    assert allowed_tools_for("knowledge_question") == frozenset(
        {"knowledge_retrieval", "load_skill"}
    )
    assert allowed_tools_for("incident_query") == frozenset(
        {"list_active_incidents", "get_incident"}
    )
    assert allowed_tools_for("start_diagnostic") == frozenset(
        {"list_active_incidents", "get_incident", "start_incident_diagnostic"}
    )
    assert allowed_tools_for("diagnostic_status") == frozenset(
        {
            "get_diagnostic_status",
            "get_diagnostic_report",
            "get_diagnostic_evidence",
        }
    )


@pytest.mark.asyncio
async def test_llm_router_adapter_returns_only_decoded_json_object() -> None:
    model = LlmStructuredRouterModel(
        FakeChatModel(
            '{"intent":"knowledge_question","confidence":0.88,'
            '"incidentId":null,"diagnosticTaskId":null,"needsClarification":false}'
        )
    )

    payload = await model.route("如何查看运行手册？")

    assert payload["intent"] == "knowledge_question"
    assert payload["confidence"] == 0.88
