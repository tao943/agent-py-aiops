"""Least-privilege tool allowlists for each public chat intent."""

from __future__ import annotations

from typing import Final

from super_ai.chat.intent import ChatIntent

_TOOLS_BY_INTENT: Final[dict[ChatIntent, frozenset[str]]] = {
    "general_chat": frozenset({"get_current_time", "load_skill"}),
    "knowledge_question": frozenset({"knowledge_retrieval", "load_skill"}),
    "incident_query": frozenset({"list_active_incidents", "get_incident"}),
    "start_diagnostic": frozenset(
        {"list_active_incidents", "get_incident", "start_incident_diagnostic"}
    ),
    "diagnostic_status": frozenset(
        {"get_diagnostic_status", "get_diagnostic_report", "get_diagnostic_evidence"}
    ),
    "recovery_request": frozenset(
        {
            "get_diagnostic_status",
            "get_diagnostic_report",
            "create_recovery_approval_request",
        }
    ),
}


def allowed_tools_for(intent: ChatIntent) -> frozenset[str]:
    """Return the immutable tool allowlist for one validated intent."""

    return _TOOLS_BY_INTENT[intent]
