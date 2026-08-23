"""Bounded intent routing for Conversation Agent tool selection."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from super_ai.chat.input_safety import evaluate_chat_input_safety
from super_ai.llm import ChatModel

ChatIntent = Literal[
    "general_chat",
    "knowledge_question",
    "incident_query",
    "start_diagnostic",
    "diagnostic_status",
    "recovery_request",
]
ChatRouteSource = Literal["rule", "model", "fallback"]

_INTENTS: frozenset[str] = frozenset(
    {
        "general_chat",
        "knowledge_question",
        "incident_query",
        "start_diagnostic",
        "diagnostic_status",
        "recovery_request",
    }
)
_DIAGNOSTIC_ID = re.compile(r"(?<![A-Za-z0-9_-])(diagnostic_[A-Za-z0-9_-]+)")
_INCIDENT_ID = re.compile(r"(?<![A-Za-z0-9_-])(incident_[A-Za-z0-9_-]+)")
_START_WORDS = ("启动", "开始", "诊断", "排查", "处理", "investigate", "diagnose")
_RECOVERY_WORDS = ("恢复", "审批", "approval", "recover", "remediation")


@dataclass(frozen=True, slots=True)
class ChatRoute:
    """Public, auditable routing decision without hidden model reasoning."""

    intent: ChatIntent
    confidence: float
    source: ChatRouteSource
    incident_id: str | None = None
    diagnostic_task_id: str | None = None
    needs_clarification: bool = False
    blocked_reason: str | None = None


class StructuredRouterModel(Protocol):
    """Narrow structured-classification boundary used after deterministic rules."""

    async def route(self, content: str) -> Mapping[str, object]: ...


class LlmStructuredRouterModel:
    """Ask the configured Chat model for one bounded JSON routing object."""

    def __init__(self, model: ChatModel) -> None:
        self._model = model

    async def route(self, content: str) -> Mapping[str, object]:
        response = await self._model.ainvoke(
            "Classify the user request for tool routing. Return one JSON object only with "
            "intent, confidence, incidentId, diagnosticTaskId, needsClarification. "
            "Allowed intents: general_chat, knowledge_question, incident_query, "
            "start_diagnostic, diagnostic_status, recovery_request. Do not include reasoning. "
            f"User request: {content[:4000]}"
        )
        raw_content = getattr(response, "content", response)
        if not isinstance(raw_content, str):
            raise ValueError("Router model response must be text JSON.")
        decoded = json.loads(raw_content)
        if not isinstance(decoded, dict):
            raise ValueError("Router model response must be a JSON object.")
        return cast(Mapping[str, object], decoded)


class KeywordRouterModel:
    """Offline deterministic classifier for injected test runners and degraded mode."""

    async def route(self, content: str) -> Mapping[str, object]:
        lowered = content.casefold()
        if any(word in lowered for word in ("告警", "事故", "incident", "alert")):
            intent: ChatIntent = "incident_query"
        elif any(word in lowered for word in ("如何", "怎么", "什么", "how", "what")):
            intent = "knowledge_question"
        else:
            intent = "general_chat"
        return {
            "intent": intent,
            "confidence": 0.80,
            "incidentId": None,
            "diagnosticTaskId": None,
            "needsClarification": False,
        }


class ChatIntentRouter:
    """Prefer explicit identifiers and safely fall back when classification fails."""

    def __init__(self, model: StructuredRouterModel) -> None:
        self._model = model

    async def route(self, content: str) -> ChatRoute:
        normalized = content.strip()
        safety = evaluate_chat_input_safety(normalized)
        if safety.blocked:
            return ChatRoute(
                "general_chat",
                1.0,
                "rule",
                blocked_reason=safety.reason_code,
            )
        explicit = _route_explicit_identifiers(normalized)
        if explicit is not None:
            return explicit
        try:
            return _validate_model_route(await self._model.route(normalized))
        except Exception:
            return ChatRoute("general_chat", 0.0, "fallback")


def _route_explicit_identifiers(content: str) -> ChatRoute | None:
    diagnostic_match = _DIAGNOSTIC_ID.search(content)
    if diagnostic_match is not None:
        lowered = content.casefold()
        intent: ChatIntent = (
            "recovery_request"
            if any(word in lowered for word in _RECOVERY_WORDS)
            else "diagnostic_status"
        )
        return ChatRoute(
            intent,
            1.0,
            "rule",
            diagnostic_task_id=diagnostic_match.group(1),
        )
    incident_match = _INCIDENT_ID.search(content)
    if incident_match is None:
        return None
    lowered = content.casefold()
    intent: ChatIntent = (
        "start_diagnostic" if any(word in lowered for word in _START_WORDS) else "incident_query"
    )
    return ChatRoute(intent, 1.0, "rule", incident_id=incident_match.group(1))


def _validate_model_route(payload: Mapping[str, object]) -> ChatRoute:
    raw_intent = payload.get("intent")
    if not isinstance(raw_intent, str) or raw_intent not in _INTENTS:
        raise ValueError("Router returned an unsupported intent.")
    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise ValueError("Router confidence must be numeric.")
    confidence = float(raw_confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Router confidence is outside the supported range.")
    incident_id = _optional_identifier(payload.get("incidentId"), _INCIDENT_ID)
    diagnostic_task_id = _optional_identifier(
        payload.get("diagnosticTaskId"), _DIAGNOSTIC_ID
    )
    intent = cast(ChatIntent, raw_intent)
    target_missing = (intent == "start_diagnostic" and incident_id is None) or (
        intent in {"diagnostic_status", "recovery_request"} and diagnostic_task_id is None
    )
    explicit_clarification = payload.get("needsClarification", False)
    if not isinstance(explicit_clarification, bool):
        raise ValueError("Router clarification flag must be boolean.")
    return ChatRoute(
        intent=intent,
        confidence=confidence,
        source="model",
        incident_id=incident_id,
        diagnostic_task_id=diagnostic_task_id,
        needs_clarification=explicit_clarification or confidence < 0.70 or target_missing,
    )


def _optional_identifier(value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("Router returned an invalid resource identifier.")
    return value
