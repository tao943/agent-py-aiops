from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from super_ai.chat.execution_policy import policy_for
from super_ai.chat.intent import ChatRoute
from super_ai.chat.tool_catalog import RequiredToolUnavailable, ToolCatalog


def _tool(name: str) -> StructuredTool:
    def invoke() -> str:
        return name

    return StructuredTool.from_function(
        func=invoke,
        name=name,
        description=f"Test tool {name}",
    )


def test_explicit_report_uses_direct_read_with_postcondition() -> None:
    policy = policy_for(
        ChatRoute(
            "diagnostic_status",
            1.0,
            "rule",
            diagnostic_task_id="diagnostic_1",
        )
    )

    assert policy.mode == "direct_read"
    assert policy.required_capability == "diagnostic_report"
    assert policy.postcondition == "diagnostic_result"
    assert policy.required_tools == frozenset()


def test_new_diagnostic_requires_confirmation_without_model_visible_write_tool() -> None:
    policy = policy_for(
        ChatRoute("start_diagnostic", 1.0, "rule", incident_id="incident_1")
    )

    assert policy.mode == "confirmation_required"
    assert policy.required_capability == "start_diagnostic"
    assert "start_incident_diagnostic" not in policy.allowed_tools
    assert policy.postcondition == "pending_action_or_existing_diagnostic"


def test_knowledge_react_has_normal_budget_and_required_retrieval() -> None:
    policy = policy_for(ChatRoute("knowledge_question", 0.9, "model"))

    assert policy.mode == "bounded_react"
    assert policy.budget.max_model_calls == 3
    assert policy.budget.max_query_rewrite_calls == 1
    assert policy.budget.max_tool_calls == 2
    assert policy.budget.deadline_seconds == 120.0
    assert policy.required_tools == frozenset({"knowledge_retrieval"})


def test_non_knowledge_routes_do_not_reserve_rewrite_calls() -> None:
    policy = policy_for(ChatRoute("general_chat", 0.9, "model"))

    assert policy.budget.max_query_rewrite_calls == 0


def test_blocked_route_has_zero_model_and_tool_budget() -> None:
    policy = policy_for(
        ChatRoute(
            "general_chat",
            1.0,
            "rule",
            blocked_reason="prompt_injection_sensitive_action",
        )
    )

    assert policy.required_capability == "input_safety_refusal"
    assert policy.allowed_tools == frozenset()
    assert policy.budget.max_model_calls == 0
    assert policy.budget.max_tool_calls == 0


def test_missing_required_tool_fails_compilation() -> None:
    with pytest.raises(RequiredToolUnavailable, match="knowledge_retrieval"):
        ToolCatalog().compile(
            policy=policy_for(ChatRoute("knowledge_question", 0.9, "model")),
            registry={},
        )


def test_catalog_exposes_only_policy_allowlist_with_stable_version() -> None:
    registry = {
        "execute_recovery": _tool("execute_recovery"),
        "load_skill": _tool("load_skill"),
        "knowledge_retrieval": _tool("knowledge_retrieval"),
    }
    policy = policy_for(ChatRoute("knowledge_question", 0.9, "model"))

    first = ToolCatalog().compile(policy=policy, registry=registry)
    second = ToolCatalog().compile(
        policy=policy,
        registry=dict(reversed(tuple(registry.items()))),
    )

    assert first.names == ("knowledge_retrieval", "load_skill")
    assert tuple(tool.name for tool in first.tools) == first.names
    assert first.catalog_version == second.catalog_version
    assert "execute_recovery" not in first.names


def test_direct_read_does_not_expose_bridge_tools_to_model() -> None:
    policy = policy_for(
        ChatRoute(
            "diagnostic_status",
            1.0,
            "rule",
            diagnostic_task_id="diagnostic_1",
        )
    )
    compiled = ToolCatalog().compile(
        policy=policy,
        registry={"get_diagnostic_report": _tool("get_diagnostic_report")},
    )

    assert compiled.names == ()
    assert compiled.tools == ()
