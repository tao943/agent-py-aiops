"""Unified, auditable execution for one Conversation Agent turn."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from super_ai.chat.aiops_bridge import IncidentSummary, PublicDiagnosticReport
from super_ai.chat.execution_policy import ExecutionMode, policy_for
from super_ai.chat.intent import ChatRoute

ChatTurnStatus = Literal[
    "succeeded",
    "succeeded_with_degraded_explanation",
    "awaiting_confirmation",
    "failed",
    "manual_review",
]
ExplanationStatus = Literal["not_requested", "running", "succeeded", "degraded"]


class ChatPostconditionFailed(RuntimeError):
    """A structured execution result did not prove the required postcondition."""


class DirectReadBridge(Protocol):
    async def list_active_incidents(
        self, *, owner_user_id: str, limit: int = 10
    ) -> tuple[IncidentSummary, ...]: ...

    async def get_incident(
        self, *, owner_user_id: str, incident_id: str
    ) -> IncidentSummary: ...

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport: ...


class ExplanationModel(Protocol):
    async def ainvoke(self, input: object) -> object: ...


@dataclass(frozen=True, slots=True)
class ChatTurnExecutionRequest:
    owner_user_id: str
    content: str
    route: ChatRoute
    chat_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    route: ChatRoute
    mode: ExecutionMode
    status: ChatTurnStatus
    structured_result: Mapping[str, object] | None
    pending_action_id: str | None
    postcondition: str
    explanation_status: ExplanationStatus
    safe_error_code: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "status": self.status,
            "structuredResult": dict(self.structured_result or {}),
            "pendingActionId": self.pending_action_id,
            "postcondition": self.postcondition,
            "explanationStatus": self.explanation_status,
            "safeErrorCode": self.safe_error_code,
        }


@dataclass(frozen=True, slots=True)
class ChatTurnEvent:
    type: str
    payload: Mapping[str, object]
    result: ChatTurnResult | None = None


class ChatTurnExecutionService:
    """Execute deterministic reads before an optional, non-authoritative explanation."""

    def __init__(
        self,
        *,
        bridge: DirectReadBridge,
        explanation_model: ExplanationModel | None,
    ) -> None:
        self._bridge = bridge
        self._explanation_model = explanation_model

    async def execute(
        self, request: ChatTurnExecutionRequest
    ) -> AsyncIterator[ChatTurnEvent]:
        policy = policy_for(request.route)
        yield ChatTurnEvent(
            "execution.mode_selected",
            {
                "mode": policy.mode,
                "requiredCapability": policy.required_capability,
                "postcondition": policy.postcondition,
            },
        )
        if policy.mode != "direct_read":
            raise ChatPostconditionFailed(
                "ChatTurnExecutionService direct path received a non-direct policy."
            )

        structured_result = await self._execute_direct_read(request)
        _validate_postcondition(policy.postcondition, structured_result)
        yield ChatTurnEvent("structured.result", structured_result)

        explanation_status: ExplanationStatus = "not_requested"
        status: ChatTurnStatus = "succeeded"
        if self._explanation_model is not None:
            explanation_status = "running"
            try:
                response = await self._explanation_model.ainvoke(
                    _explanation_prompt(structured_result)
                )
                explanation = _response_text(response)
                if not explanation:
                    raise ValueError("Explanation model returned no public text.")
                yield ChatTurnEvent("explanation.delta", {"delta": explanation})
                explanation_status = "succeeded"
            except Exception:
                explanation_status = "degraded"
                status = "succeeded_with_degraded_explanation"
                yield ChatTurnEvent(
                    "explanation.degraded",
                    {"code": "CHAT_EXPLANATION_DEGRADED", "retryable": True},
                )

        result = ChatTurnResult(
            route=request.route,
            mode=policy.mode,
            status=status,
            structured_result=structured_result,
            pending_action_id=None,
            postcondition=policy.postcondition,
            explanation_status=explanation_status,
            safe_error_code=None,
        )
        yield ChatTurnEvent("complete", {"result": result.to_payload()}, result=result)

    async def _execute_direct_read(
        self, request: ChatTurnExecutionRequest
    ) -> dict[str, object]:
        if request.route.intent == "incident_query":
            incident_id = request.route.incident_id
            if incident_id is not None:
                incident = await self._bridge.get_incident(
                    owner_user_id=request.owner_user_id,
                    incident_id=incident_id,
                )
                return {"incident": incident.to_payload()}
            incidents = await self._bridge.list_active_incidents(
                owner_user_id=request.owner_user_id,
                limit=10,
            )
            return {"incidents": [item.to_payload() for item in incidents]}

        task_id = request.route.diagnostic_task_id
        if request.route.intent != "diagnostic_status" or task_id is None:
            raise ChatPostconditionFailed("Direct read target is missing or unsupported.")
        report = await self._bridge.get_diagnostic_report(
            owner_user_id=request.owner_user_id,
            task_id=task_id,
        )
        return {"diagnostic": report.to_payload()}


def _validate_postcondition(
    postcondition: str, structured_result: Mapping[str, object]
) -> None:
    if postcondition == "incident_result":
        incident = structured_result.get("incident")
        incidents = structured_result.get("incidents")
        if isinstance(incident, Mapping) or isinstance(incidents, list):
            return
        raise ChatPostconditionFailed("Incident result is absent.")
    if postcondition != "diagnostic_result":
        raise ChatPostconditionFailed("Unsupported direct-read postcondition.")
    diagnostic = structured_result.get("diagnostic")
    if not isinstance(diagnostic, Mapping):
        raise ChatPostconditionFailed("Diagnostic result is absent.")
    payload = cast(Mapping[str, object], diagnostic)
    required_types: tuple[tuple[str, type[object]], ...] = (
        ("id", str),
        ("taskId", str),
        ("recoveryMode", str),
        ("executionPermitted", bool),
        ("humanApprovalRequired", bool),
        ("validatorStatus", str),
        ("evidenceIds", list),
    )
    if any(not isinstance(payload.get(key), expected) for key, expected in required_types):
        raise ChatPostconditionFailed("Diagnostic result is incomplete.")
    if (
        payload.get("executionPermitted") is True
        and payload.get("humanApprovalRequired") is True
    ):
        raise ChatPostconditionFailed("Diagnostic safety fields are contradictory.")


def _explanation_prompt(structured_result: Mapping[str, object]) -> str:
    if "diagnostic" not in structured_result:
        return (
            "请用简体中文简要说明以下 owner-scoped Incident 查询结果。不得添加未提供的事实："
            + json.dumps(
                structured_result,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )[:12000]
        )
    diagnostic = structured_result.get("diagnostic")
    source: Mapping[str, object] = (
        cast(Mapping[str, object], diagnostic) if isinstance(diagnostic, Mapping) else {}
    )
    allowed: dict[str, object] = {
        key: source[key]
        for key in (
            "id",
            "taskId",
            "title",
            "rootCause",
            "recoveryMode",
            "executionPermitted",
            "humanApprovalRequired",
            "validatorStatus",
            "evidenceIds",
        )
        if key in source
    }
    return (
        "请用简体中文解释以下已验证的诊断 DTO。不得更改安全字段，不得添加未提供的事实，"
        "只返回面向用户的简短说明："
        + json.dumps(allowed, ensure_ascii=False, sort_keys=True, default=str)
    )


def _response_text(response: object) -> str:
    content = getattr(response, "content", response)
    return content.strip()[:4000] if isinstance(content, str) else ""
