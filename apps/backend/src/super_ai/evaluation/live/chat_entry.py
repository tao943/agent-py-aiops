"""Confirmation-gated Chat entry into the existing AIOps Live diagnostic path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from super_ai.chat.aiops_bridge import IncidentSummary, PublicDiagnosticReport
from super_ai.chat.pending_actions import PendingChatActionService
from super_ai.chat.tool_policy import allowed_tools_for
from super_ai.evaluation.artifacts import RunArtifact
from super_ai.evaluation.live.domain import (
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveScenario,
)
from super_ai.memory.models import utc_now
from super_ai.memory.repositories import (
    PendingChatActionRecord,
    PendingChatActionRepository,
)


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


class ApplicationLiveDiagnostic(Protocol):
    async def diagnose(
        self,
        *,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        evidence_context: LiveEvidenceContext,
    ) -> RunArtifact: ...


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


class ChatEntryLiveDiagnosticAdapter:
    """Make Chat confirmation the entry while delegating all diagnosis to AIOps Live."""

    def __init__(
        self,
        *,
        owner_user_id: str,
        pending_repository: PendingChatActionRepository,
        report_bridge: ChatLiveBridge,
        diagnostic_delegate: ApplicationLiveDiagnostic,
    ) -> None:
        self._owner_user_id = owner_user_id
        self._pending_repository = pending_repository
        self._report_bridge = report_bridge
        self._diagnostic_delegate = diagnostic_delegate
        self._metrics_by_task: dict[str, ConversationLiveMetrics] = {}

    async def diagnose(
        self,
        *,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        evidence_context: LiveEvidenceContext,
    ) -> RunArtifact:
        incident_bridge = _PreparedIncidentBridge(
            incident=IncidentSummary(
                id=evidence_context.incident_id,
                status="active",
                alert_name=str(scenario.alert.get("alertname") or scenario.id),
                service=str(scenario.alert.get("service") or "live-evaluation"),
                severity=str(scenario.alert.get("severity") or "warning"),
                last_seen_at=utc_now(),
                diagnostic_task_id=None,
            ),
            report_bridge=self._report_bridge,
        )
        awaiter = _InlineLiveExecutionAwaiter(
            repository=self._pending_repository,
            diagnostic_delegate=self._diagnostic_delegate,
            run_id=run_id,
            scenario=scenario,
            observation=observation,
            evidence_context=evidence_context,
        )
        entry = ChatLiveEntryAdapter(
            pending_actions=PendingChatActionService(self._pending_repository),
            bridge=incident_bridge,
            execution_awaiter=awaiter,
        )
        action = await entry.request_start_from_incident(
            owner_user_id=self._owner_user_id,
            incident_id=evidence_context.incident_id,
            client_request_id=run_id,
        )
        if action.status != "pending" or action.execution_result_id is not None:
            raise ChatLiveEntryError("Chat Live request bypassed confirmation.")
        started = await entry.confirm_start(
            owner_user_id=self._owner_user_id,
            action_id=action.id,
        )
        artifact = awaiter.artifact
        if (
            artifact is None
            or artifact.diagnostic_task_id != started.diagnostic_task_id
            or artifact.scenario_id != scenario.id
        ):
            raise ChatLiveEntryError("AIOps diagnostic artifact identity is inconsistent.")
        report = await entry.read_final_report(
            owner_user_id=self._owner_user_id,
            diagnostic_task_id=started.diagnostic_task_id,
        )
        artifact_evidence = {item.record_id for item in artifact.evidence}
        if not set(report.evidence_ids).issubset(artifact_evidence):
            raise ChatLiveEntryError("Chat report evidence does not match AIOps output.")
        self._metrics_by_task[started.diagnostic_task_id] = started.metrics
        return artifact

    def conversation_metrics(self) -> dict[str, object]:
        if not self._metrics_by_task:
            return {}
        latest_task = next(reversed(self._metrics_by_task))
        return self._metrics_by_task[latest_task].to_payload()


class _PreparedIncidentBridge:
    def __init__(
        self, *, incident: IncidentSummary, report_bridge: ChatLiveBridge
    ) -> None:
        self._incident = incident
        self._report_bridge = report_bridge

    async def get_incident(
        self, *, owner_user_id: str, incident_id: str
    ) -> IncidentSummary:
        del owner_user_id
        if incident_id != self._incident.id:
            raise ChatLiveEntryError("Prepared Live incident is unavailable.")
        return self._incident

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport:
        return await self._report_bridge.get_diagnostic_report(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )


class _InlineLiveExecutionAwaiter:
    """Evaluation-only worker that preserves Pending Action idempotency."""

    def __init__(
        self,
        *,
        repository: PendingChatActionRepository,
        diagnostic_delegate: ApplicationLiveDiagnostic,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        evidence_context: LiveEvidenceContext,
    ) -> None:
        self._repository = repository
        self._diagnostic_delegate = diagnostic_delegate
        self._run_id = run_id
        self._scenario = scenario
        self._observation = observation
        self._evidence_context = evidence_context
        self.artifact: RunArtifact | None = None

    async def await_executed(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord:
        action = await self._repository.get_owned(
            owner_user_id=owner_user_id,
            action_id=action_id,
        )
        if action is None:
            raise ChatLiveEntryError("Confirmed Chat action disappeared.")
        if action.status == "executed" and action.execution_result_id:
            if self.artifact is None:
                raise ChatLiveEntryError("Executed Chat action lacks this run's artifact.")
            return action
        if action.status != "confirmed":
            raise ChatLiveEntryError("Chat action is not ready for execution.")
        artifact = await self._diagnostic_delegate.diagnose(
            run_id=self._run_id,
            scenario=self._scenario,
            observation=self._observation,
            evidence_context=self._evidence_context,
        )
        if not artifact.diagnostic_task_id:
            raise ChatLiveEntryError("AIOps Live did not return a durable task ID.")
        self.artifact = artifact
        return await self._repository.mark_executed(
            owner_user_id=owner_user_id,
            action_id=action_id,
            execution_result_id=artifact.diagnostic_task_id,
            now=utc_now(),
        )
