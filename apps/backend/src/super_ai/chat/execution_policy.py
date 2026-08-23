"""Auditable execution policy derived from one validated chat route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from super_ai.chat.intent import ChatRoute
from super_ai.chat.tool_policy import allowed_tools_for

ExecutionMode = Literal["direct_read", "confirmation_required", "bounded_react"]


@dataclass(frozen=True, slots=True)
class ChatExecutionBudget:
    max_model_calls: int
    max_tool_calls: int
    deadline_seconds: float
    max_query_rewrite_calls: int = 0


@dataclass(frozen=True, slots=True)
class ChatExecutionPolicy:
    mode: ExecutionMode
    required_capability: str
    allowed_tools: frozenset[str]
    required_tools: frozenset[str]
    postcondition: str
    budget: ChatExecutionBudget


_NO_MODEL_BUDGET = ChatExecutionBudget(0, 0, 30.0)
_NORMAL_REACT_BUDGET = ChatExecutionBudget(2, 2, 120.0)
_KNOWLEDGE_REACT_BUDGET = ChatExecutionBudget(3, 2, 120.0, 1)
_EXPLORATORY_REACT_BUDGET = ChatExecutionBudget(4, 6, 180.0)


def policy_for(route: ChatRoute) -> ChatExecutionPolicy:
    """Compile one immutable policy without consulting model-generated tool names."""

    if route.needs_clarification:
        safe_tools = allowed_tools_for(route.intent) - frozenset(
            {"start_incident_diagnostic", "create_recovery_approval_request"}
        )
        return ChatExecutionPolicy(
            mode="bounded_react",
            required_capability="clarify_target",
            allowed_tools=safe_tools,
            required_tools=frozenset(),
            postcondition="clarification_requested",
            budget=_EXPLORATORY_REACT_BUDGET,
        )

    if route.intent == "incident_query":
        return _direct_read("incident_query", "incident_result")
    if route.intent == "diagnostic_status":
        return _direct_read("diagnostic_report", "diagnostic_result")
    if route.intent == "start_diagnostic":
        return _confirmation(
            capability="start_diagnostic",
            safe_precheck_tools=frozenset({"list_active_incidents", "get_incident"}),
            postcondition="pending_action_or_existing_diagnostic",
        )
    if route.intent == "recovery_request":
        return _confirmation(
            capability="recovery_approval",
            safe_precheck_tools=frozenset(
                {"get_diagnostic_status", "get_diagnostic_report"}
            ),
            postcondition="pending_action_or_existing_approval",
        )
    if route.intent == "knowledge_question":
        return ChatExecutionPolicy(
            mode="bounded_react",
            required_capability="knowledge_answer",
            allowed_tools=allowed_tools_for(route.intent),
            required_tools=frozenset({"knowledge_retrieval"}),
            postcondition="grounded_answer",
            budget=_KNOWLEDGE_REACT_BUDGET,
        )
    return ChatExecutionPolicy(
        mode="bounded_react",
        required_capability="general_answer",
        allowed_tools=allowed_tools_for(route.intent),
        required_tools=frozenset(),
        postcondition="response_generated",
        budget=_NORMAL_REACT_BUDGET,
    )


def _direct_read(capability: str, postcondition: str) -> ChatExecutionPolicy:
    return ChatExecutionPolicy(
        mode="direct_read",
        required_capability=capability,
        allowed_tools=frozenset(),
        required_tools=frozenset(),
        postcondition=postcondition,
        budget=_NO_MODEL_BUDGET,
    )


def _confirmation(
    *, capability: str, safe_precheck_tools: frozenset[str], postcondition: str
) -> ChatExecutionPolicy:
    return ChatExecutionPolicy(
        mode="confirmation_required",
        required_capability=capability,
        allowed_tools=safe_precheck_tools,
        required_tools=frozenset(),
        postcondition=postcondition,
        budget=_NO_MODEL_BUDGET,
    )
