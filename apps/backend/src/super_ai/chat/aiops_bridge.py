"""Least-privilege owner-bound tools over persisted AIOps results."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol, cast
from uuid import uuid4

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from super_ai.alert_ingestion.repositories import (
    AlertIncidentActiveQueryRepository,
    AlertIncidentRecord,
    DiagnosticScheduleResult,
    IncidentDiagnosticScheduler,
    IncidentUnavailable,
)
from super_ai.chat.run_events import tool_call_key
from super_ai.memory.models import utc_now
from super_ai.memory.repositories import (
    ChatToolExecutionClaim,
    ChatToolExecutionRepository,
    DiagnosticMemoryRepository,
    JsonDict,
)
from super_ai.recovery.intent_service import RecoveryIntentService


class BridgeResourceNotFound(LookupError):
    """The requested owner-scoped resource is absent or inaccessible."""


class RecoveryApprovalNotAllowed(RuntimeError):
    """The diagnostic has no proposal eligible for human approval."""


@dataclass(frozen=True, slots=True)
class RecoveryApprovalRequest:
    id: str
    owner_user_id: str
    diagnostic_task_id: str
    status: str
    execution_permitted: bool
    reused: bool
    legacy: bool = False

    def to_payload(self) -> JsonDict:
        payload: JsonDict = {
            "id": self.id,
            "diagnosticTaskId": self.diagnostic_task_id,
            "status": self.status,
            "executionPermitted": False,
            "reused": self.reused,
            "legacy": self.legacy,
        }
        if self.legacy:
            payload["safeInstruction"] = (
                "Create a current Recovery Intent; this legacy request grants no authority."
            )
        return payload


class RecoveryApprovalRequestRepository(Protocol):
    async def create_or_get(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
        proposal_fingerprint: str,
        request_reason: str,
        chat_run_id: str | None,
    ) -> RecoveryApprovalRequest: ...


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    id: str
    status: str
    alert_name: str
    service: str
    severity: str
    last_seen_at: datetime
    diagnostic_task_id: str | None

    def to_payload(self) -> JsonDict:
        return {
            "id": self.id,
            "status": self.status,
            "alertName": self.alert_name,
            "service": self.service,
            "severity": self.severity,
            "lastSeenAt": self.last_seen_at.isoformat(),
            "diagnosticTaskId": self.diagnostic_task_id,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticStatus:
    id: str
    status: str
    completed_at: datetime | None
    report_available: bool

    def to_payload(self) -> JsonDict:
        return {
            "id": self.id,
            "status": self.status,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "reportAvailable": self.report_available,
        }


@dataclass(frozen=True, slots=True)
class PublicDiagnosticReport:
    id: str
    task_id: str
    title: str
    content: str
    root_cause: JsonDict | None
    recovery_mode: str
    execution_permitted: bool
    human_approval_required: bool
    validator_status: str
    evidence_ids: tuple[str, ...]
    created_at: datetime

    def to_payload(self) -> JsonDict:
        return {
            "id": self.id,
            "taskId": self.task_id,
            "title": self.title,
            "content": self.content,
            "rootCause": self.root_cause,
            "recoveryMode": self.recovery_mode,
            "executionPermitted": self.execution_permitted,
            "humanApprovalRequired": self.human_approval_required,
            "validatorStatus": self.validator_status,
            "evidenceIds": list(self.evidence_ids),
            "createdAt": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PublicEvidence:
    id: str
    task_id: str
    kind: str
    source: str
    summary: str
    evaluation: str | None
    created_at: datetime

    def to_payload(self) -> JsonDict:
        return {
            "id": self.id,
            "taskId": self.task_id,
            "kind": self.kind,
            "source": self.source,
            "summary": self.summary,
            "evaluation": self.evaluation,
            "createdAt": self.created_at.isoformat(),
        }


class AiopsBridgeService:
    """Read owner-scoped AIOps state without exposing internal graph state."""

    def __init__(
        self,
        *,
        incidents: AlertIncidentActiveQueryRepository,
        diagnostics: DiagnosticMemoryRepository,
        scheduler: IncidentDiagnosticScheduler | None = None,
        approval_requests: RecoveryApprovalRequestRepository | None = None,
        recovery_intents: RecoveryIntentService | None = None,
        tool_executions: ChatToolExecutionRepository | None = None,
    ) -> None:
        self._incidents = incidents
        self._diagnostics = diagnostics
        self._scheduler = scheduler
        self._approval_requests = approval_requests
        self._recovery_intents = recovery_intents
        self.tool_executions = tool_executions

    async def list_active_incidents(
        self, *, owner_user_id: str, limit: int = 10
    ) -> tuple[IncidentSummary, ...]:
        records = await self._incidents.list_active(
            owner_user_id=owner_user_id, limit=min(max(limit, 1), 50)
        )
        return tuple(_incident_summary(record) for record in records)

    async def get_incident(self, *, owner_user_id: str, incident_id: str) -> IncidentSummary:
        record = await self._incidents.get_owned(
            owner_user_id=owner_user_id, incident_id=incident_id
        )
        if record is None:
            raise BridgeResourceNotFound("Incident is unavailable.")
        return _incident_summary(record)

    async def get_diagnostic_status(self, *, owner_user_id: str, task_id: str) -> DiagnosticStatus:
        task = await self._require_task(owner_user_id, task_id)
        reports = await self._diagnostics.list_reports(owner_user_id=owner_user_id, task_id=task_id)
        return DiagnosticStatus(task.id, task.status, task.completed_at, bool(reports))

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport:
        await self._require_task(owner_user_id, task_id)
        reports = await self._diagnostics.list_reports(owner_user_id=owner_user_id, task_id=task_id)
        if not reports:
            raise BridgeResourceNotFound("Diagnostic report is unavailable.")
        report = reports[-1]
        payload = report.payload
        recovery_plan = _mapping(payload.get("recoveryPlan"))
        recovery_policy = _mapping(payload.get("recoveryPolicy"))
        validation = _mapping(payload.get("decisionValidation"))
        root_cause = _public_root_cause(payload.get("rootCauseDecision"))
        return PublicDiagnosticReport(
            id=report.id,
            task_id=task_id,
            title=report.title,
            content=report.content,
            root_cause=root_cause,
            recovery_mode=_text(recovery_plan.get("mode"), "no_action"),
            execution_permitted=recovery_policy.get("executionPermitted") is True,
            human_approval_required=(
                recovery_policy.get("humanApprovalRequired") is True
                or recovery_plan.get("humanApprovalRequired") is True
            ),
            validator_status=_text(validation.get("validationOrigin"), "unavailable"),
            evidence_ids=tuple(_string_list(payload.get("evidenceIds"))),
            created_at=report.created_at,
        )

    async def get_diagnostic_evidence(
        self, *, owner_user_id: str, task_id: str, limit: int = 20
    ) -> tuple[PublicEvidence, ...]:
        await self._require_task(owner_user_id, task_id)
        records = await self._diagnostics.list_evidence(
            owner_user_id=owner_user_id, task_id=task_id
        )
        bounded = records[: min(max(limit, 1), 50)]
        return tuple(
            PublicEvidence(
                id=record.id,
                task_id=record.task_id,
                kind=record.kind,
                source=record.source,
                summary=record.summary,
                evaluation=(
                    str(record.payload["evaluation"])
                    if isinstance(record.payload.get("evaluation"), (str, int, float, bool))
                    else None
                ),
                created_at=record.created_at,
            )
            for record in bounded
        )

    async def start_incident_diagnostic(
        self,
        *,
        owner_user_id: str,
        incident_id: str,
        note: str | None,
    ) -> DiagnosticScheduleResult:
        if self._scheduler is None:
            raise RuntimeError("Incident diagnostic scheduling is unavailable.")
        try:
            return await self._scheduler.schedule_for_incident(
                owner_user_id=owner_user_id,
                incident_id=incident_id,
                note=(note or "").strip()[:1000] or None,
            )
        except IncidentUnavailable as exc:
            raise BridgeResourceNotFound("Incident is unavailable.") from exc

    async def create_recovery_approval_request(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        reason: str,
        chat_run_id: str | None,
    ) -> RecoveryApprovalRequest:
        bounded_reason = reason.strip()[:1000]
        if not bounded_reason:
            raise ValueError("Recovery approval reason is required.")
        if self._recovery_intents is not None:
            result = await self._recovery_intents.create_result(
                owner_user_id=owner_user_id,
                diagnostic_task_id=task_id,
                note=bounded_reason,
            )
            intent = result.intent
            return RecoveryApprovalRequest(
                id=intent.id,
                owner_user_id=owner_user_id,
                diagnostic_task_id=task_id,
                status=intent.status,
                execution_permitted=False,
                reused=result.reused,
            )
        if self._approval_requests is None:
            raise RuntimeError("Recovery approval persistence is unavailable.")
        report = await self.get_diagnostic_report(owner_user_id=owner_user_id, task_id=task_id)
        if (
            not report.human_approval_required
            or report.recovery_mode == "no_action"
            or report.execution_permitted
        ):
            raise RecoveryApprovalNotAllowed(
                "Diagnostic recovery proposal is not eligible for approval."
            )
        canonical = json.dumps(
            {
                "taskId": report.task_id,
                "recoveryMode": report.recovery_mode,
                "rootCause": report.root_cause,
                "evidenceIds": report.evidence_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy = await self._approval_requests.create_or_get(
            owner_user_id=owner_user_id,
            diagnostic_task_id=task_id,
            proposal_fingerprint=sha256(canonical.encode("utf-8")).hexdigest(),
            request_reason=bounded_reason,
            chat_run_id=chat_run_id,
        )
        return RecoveryApprovalRequest(
            id=legacy.id,
            owner_user_id=legacy.owner_user_id,
            diagnostic_task_id=legacy.diagnostic_task_id,
            status=legacy.status,
            execution_permitted=False,
            reused=legacy.reused,
            legacy=True,
        )

    async def _require_task(self, owner_user_id: str, task_id: str) -> Any:
        task = await self._diagnostics.get_task(owner_user_id=owner_user_id, task_id=task_id)
        if task is None:
            raise BridgeResourceNotFound("Diagnostic task is unavailable.")
        return task


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListActiveIncidentsInput(_StrictInput):
    limit: int = Field(default=10, ge=1, le=50)


class GetIncidentInput(_StrictInput):
    incident_id: str = Field(min_length=1, max_length=80)


class DiagnosticTaskInput(_StrictInput):
    task_id: str = Field(min_length=1, max_length=80)


class DiagnosticEvidenceInput(DiagnosticTaskInput):
    limit: int = Field(default=20, ge=1, le=50)


class StartIncidentDiagnosticInput(_StrictInput):
    incident_id: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=1000)


class CreateRecoveryApprovalInput(DiagnosticTaskInput):
    reason: str = Field(min_length=1, max_length=1000)


def build_aiops_bridge_tools(
    *,
    owner_user_id: str,
    service: AiopsBridgeService,
    chat_run_id: str | None = None,
) -> dict[str, StructuredTool]:
    """Bind authenticated owner identity outside every model-visible schema."""

    async def list_active_incidents(limit: int = 10) -> JsonDict:
        items = await service.list_active_incidents(owner_user_id=owner_user_id, limit=limit)
        return {"items": [item.to_payload() for item in items]}

    async def get_incident(incident_id: str) -> JsonDict:
        return (
            await service.get_incident(owner_user_id=owner_user_id, incident_id=incident_id)
        ).to_payload()

    async def get_diagnostic_status(task_id: str) -> JsonDict:
        return (
            await service.get_diagnostic_status(owner_user_id=owner_user_id, task_id=task_id)
        ).to_payload()

    async def get_diagnostic_report(task_id: str) -> JsonDict:
        return (
            await service.get_diagnostic_report(owner_user_id=owner_user_id, task_id=task_id)
        ).to_payload()

    async def get_diagnostic_evidence(task_id: str, limit: int = 20) -> JsonDict:
        items = await service.get_diagnostic_evidence(
            owner_user_id=owner_user_id, task_id=task_id, limit=limit
        )
        return {"items": [item.to_payload() for item in items]}

    async def start_incident_diagnostic(incident_id: str, note: str | None = None) -> JsonDict:
        result = await service.start_incident_diagnostic(
            owner_user_id=owner_user_id, incident_id=incident_id, note=note
        )
        return {
            "diagnosticTaskId": result.diagnostic_task_id,
            "backgroundJobId": result.background_job_id,
            "reused": result.reused,
        }

    async def create_recovery_approval_request(task_id: str, reason: str) -> JsonDict:
        return (
            await service.create_recovery_approval_request(
                owner_user_id=owner_user_id,
                task_id=task_id,
                reason=reason,
                chat_run_id=chat_run_id,
            )
        ).to_payload()

    definitions = (
        (
            "list_active_incidents",
            "List active incidents owned by the current user.",
            list_active_incidents,
            ListActiveIncidentsInput,
        ),
        ("get_incident", "Get one owned incident summary.", get_incident, GetIncidentInput),
        (
            "get_diagnostic_status",
            "Get one owned diagnostic status.",
            get_diagnostic_status,
            DiagnosticTaskInput,
        ),
        (
            "get_diagnostic_report",
            "Get one owned public diagnostic report.",
            get_diagnostic_report,
            DiagnosticTaskInput,
        ),
        (
            "get_diagnostic_evidence",
            "Get public evidence for one owned diagnostic.",
            get_diagnostic_evidence,
            DiagnosticEvidenceInput,
        ),
        (
            "start_incident_diagnostic",
            "Create or reuse a diagnostic for one owned active incident.",
            start_incident_diagnostic,
            StartIncidentDiagnosticInput,
        ),
        (
            "create_recovery_approval_request",
            "Create a pending human approval request; never execute recovery.",
            create_recovery_approval_request,
            CreateRecoveryApprovalInput,
        ),
    )
    invocation_counts: dict[str, int] = {}

    def wrap(
        name: str,
        operation: Callable[..., Awaitable[JsonDict]],
    ) -> Callable[..., Awaitable[JsonDict]]:
        async def invoke(**arguments: object) -> JsonDict:
            executions = service.tool_executions
            if chat_run_id is None or executions is None:
                return await operation(**arguments)
            invocation_counts[name] = invocation_counts.get(name, 0) + 1
            logical_step = f"{name}:{invocation_counts[name]}"
            canonical = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            arguments_fingerprint = sha256(canonical.encode("utf-8")).hexdigest()
            key = tool_call_key(chat_run_id, logical_step, name, arguments)
            lease_owner = f"chat_tool_{uuid4().hex}"
            side_effecting = name in {
                "start_incident_diagnostic",
                "create_recovery_approval_request",
            }
            claim = await executions.claim(
                ChatToolExecutionClaim(
                    tool_call_key=key,
                    owner_user_id=owner_user_id,
                    chat_run_id=chat_run_id,
                    logical_step=logical_step,
                    tool_name=name,
                    arguments_fingerprint=arguments_fingerprint,
                    lease_owner=lease_owner,
                    lease_expires_at=utc_now() + timedelta(seconds=30),
                    side_effecting=side_effecting,
                )
            )
            if claim.action == "reuse":
                return claim.execution.public_result
            if claim.action == "manual_review":
                return {
                    "status": "manual_review",
                    "executionPermitted": False,
                    "toolCallKey": key,
                }
            if claim.action == "wait":
                return {"status": "in_progress", "toolCallKey": key}
            try:
                result = await operation(**arguments)
            except Exception:
                if side_effecting:
                    await executions.mark_uncertain(
                        tool_call_key=key,
                        lease_owner=lease_owner,
                        safe_error_code="BRIDGE_TOOL_OUTCOME_UNKNOWN",
                    )
                else:
                    await executions.fail(
                        tool_call_key=key,
                        lease_owner=lease_owner,
                        safe_error_code="BRIDGE_TOOL_FAILED",
                        retryable=True,
                    )
                raise
            await executions.complete(
                tool_call_key=key,
                lease_owner=lease_owner,
                public_result=result,
            )
            return result

        return invoke

    return {
        name: StructuredTool.from_function(
            coroutine=wrap(name, cast(Callable[..., Awaitable[JsonDict]], coroutine)),
            name=name,
            description=description,
            args_schema=args_schema,
        )
        for name, description, coroutine, args_schema in definitions
    }


def _incident_summary(record: AlertIncidentRecord) -> IncidentSummary:
    return IncidentSummary(
        id=record.id,
        status=record.status,
        alert_name=record.alert_name,
        service=record.service,
        severity=record.severity,
        last_seen_at=record.last_seen_at,
        diagnostic_task_id=record.diagnostic_task_id,
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    mapping = cast(Mapping[object, object], value)
    return {key: item for key, item in mapping.items() if isinstance(key, str)}


def _public_root_cause(value: object) -> JsonDict | None:
    if not isinstance(value, dict):
        return None
    allowed = ("component", "mechanism", "primaryCause", "trigger", "causalChain", "confidence")
    return {key: value[key] for key in allowed if key in value}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [item for item in items if isinstance(item, str)][:100]


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
