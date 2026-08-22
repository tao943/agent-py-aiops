"""Confirmation-gated Chat entry into the existing AIOps Live diagnostic path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from super_ai.chat.aiops_bridge import IncidentSummary, PublicDiagnosticReport
from super_ai.chat.tool_policy import allowed_tools_for
from super_ai.memory.repositories import PendingChatActionRecord


class ChatLiveEntryError(RuntimeError):
    """A safe public failure at the Chat-to-Live boundary."""


class PendingStartActions(Protocol):
    async def preview_start(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        incident_id: str,
        chat_run_id: str | None = None,
        note: str | None = None,
        expires_at: datetime | None = None,
    ) -> PendingChatActionRecord: ...

    async def confirm(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord: ...


class ConfirmedActionExecutionAwaiter(Protocol):
    async def await_executed(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord: ...


class ChatLiveBridge(Protocol):
    async def get_incident(
        self, *, owner_user_id: str, incident_id: str
    ) -> IncidentSummary: ...

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport: ...


@dataclass(frozen=True, slots=True)
class ConversationLiveMetrics:
    route_accuracy: float
    mode_accuracy: float
    target_accuracy: float
    postcondition: float
    confirmation_accuracy: float
    model_call_count: int
    tool_call_count: int
    confirmation_latency_ms: int

    def to_payload(self) -> dict[str, object]:
        return {
            "routeAccuracy": self.route_accuracy,
            "modeAccuracy": self.mode_accuracy,
            "targetAccuracy": self.target_accuracy,
            "postcondition": self.postcondition,
            "confirmationAccuracy": self.confirmation_accuracy,
            "modelCallCount": self.model_call_count,
            "toolCallCount": self.tool_call_count,
            "confirmationLatencyMs": self.confirmation_latency_ms,
        }


@dataclass(frozen=True, slots=True)
class ChatLiveStartResult:
    action_id: str
    diagnostic_task_id: str
    background_job_id: str | None
    confirmation_required_at: datetime
    confirmed_at: datetime
    confirmation_latency_ms: int
    metrics: ConversationLiveMetrics


@dataclass(frozen=True, slots=True)
class ChatLiveReportResult:
    report_id: str
    diagnostic_task_id: str
    evidence_ids: tuple[str, ...]
    report: PublicDiagnosticReport


class ChatLiveEntryAdapter:
    """Use durable Chat actions while leaving diagnosis and scoring with AIOps."""

    def __init__(
        self,
        *,
        pending_actions: PendingStartActions,
        bridge: ChatLiveBridge,
        execution_awaiter: ConfirmedActionExecutionAwaiter,
    ) -> None:
        self._pending_actions = pending_actions
        self._bridge = bridge
        self._execution_awaiter = execution_awaiter

    @property
    def exposed_tools(self) -> tuple[str, ...]:
        """Return only Conversation start tools; CLS belongs to the AIOps Agent."""

        return tuple(sorted(allowed_tools_for("start_diagnostic")))

    async def request_start_from_incident(
        self,
        *,
        owner_user_id: str,
        incident_id: str,
        client_request_id: str,
    ) -> PendingChatActionRecord:
        """Validate owner scope and create/reuse an unexecuted Pending Action."""

        _validate_client_request_id(client_request_id)
        incident = await self._bridge.get_incident(
            owner_user_id=owner_user_id,
            incident_id=incident_id,
        )
        if incident.id != incident_id or incident.status != "active":
            raise ChatLiveEntryError("Incident is not eligible for Live diagnosis.")
        action = await self._pending_actions.preview_start(
            owner_user_id=owner_user_id,
            session_id=_evaluation_session_id(owner_user_id, client_request_id),
            incident_id=incident_id,
            chat_run_id=client_request_id,
            note="Live evaluation entered through confirmation-gated Chat.",
        )
        if action.action_type != "start_diagnostic":
            raise ChatLiveEntryError("Pending action type is invalid.")
        return action

    async def confirm_start(
        self, *, owner_user_id: str, action_id: str
    ) -> ChatLiveStartResult:
        """Confirm once, then wait for the existing worker's idempotent result."""

        confirmed = await self._pending_actions.confirm(
            owner_user_id=owner_user_id,
            action_id=action_id,
        )
        if confirmed.action_type != "start_diagnostic" or confirmed.confirmed_at is None:
            raise ChatLiveEntryError("Diagnostic start was not confirmed.")
        executed = await self._execution_awaiter.await_executed(
            owner_user_id=owner_user_id,
            action_id=action_id,
        )
        task_id = executed.execution_result_id
        if executed.status != "executed" or not task_id:
            raise ChatLiveEntryError("Confirmed diagnostic did not produce a durable task.")
        latency_ms = max(
            0,
            round((confirmed.confirmed_at - confirmed.created_at).total_seconds() * 1000),
        )
        metrics = ConversationLiveMetrics(
            route_accuracy=1.0,
            mode_accuracy=1.0,
            target_accuracy=1.0,
            postcondition=1.0,
            confirmation_accuracy=1.0,
            model_call_count=0,
            tool_call_count=2,
            confirmation_latency_ms=latency_ms,
        )
        return ChatLiveStartResult(
            action_id=executed.id,
            diagnostic_task_id=task_id,
            background_job_id=executed.background_job_id,
            confirmation_required_at=confirmed.created_at,
            confirmed_at=confirmed.confirmed_at,
            confirmation_latency_ms=latency_ms,
            metrics=metrics,
        )

    async def read_final_report(
        self, *, owner_user_id: str, diagnostic_task_id: str
    ) -> ChatLiveReportResult:
        """Read the same owner-scoped report and evidence IDs produced by AIOps."""

        report = await self._bridge.get_diagnostic_report(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        if report.task_id != diagnostic_task_id:
            raise ChatLiveEntryError("Diagnostic report identity is inconsistent.")
        return ChatLiveReportResult(
            report_id=report.id,
            diagnostic_task_id=report.task_id,
            evidence_ids=report.evidence_ids,
            report=report,
        )


def _evaluation_session_id(owner_user_id: str, client_request_id: str) -> str:
    digest = sha256(f"{owner_user_id}:{client_request_id}".encode()).hexdigest()[:24]
    return f"chat_live_{digest}"


def _validate_client_request_id(value: str) -> str:
    if not value or len(value) > 80 or any(char in value for char in "/\\\x00\r\n"):
        raise ValueError("Chat Live client request ID is invalid.")
    return value
