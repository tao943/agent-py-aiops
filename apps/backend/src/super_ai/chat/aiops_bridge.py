"""Least-privilege owner-bound tools over persisted AIOps results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from super_ai.alert_ingestion.repositories import (
    AlertIncidentQueryRepository,
    AlertIncidentRecord,
)
from super_ai.memory.repositories import DiagnosticMemoryRepository, JsonDict


class BridgeResourceNotFound(LookupError):
    """The requested owner-scoped resource is absent or inaccessible."""


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
        incidents: AlertIncidentQueryRepository,
        diagnostics: DiagnosticMemoryRepository,
    ) -> None:
        self._incidents = incidents
        self._diagnostics = diagnostics

    async def list_active_incidents(
        self, *, owner_user_id: str, limit: int = 10
    ) -> tuple[IncidentSummary, ...]:
        records = await self._incidents.list_active(
            owner_user_id=owner_user_id, limit=min(max(limit, 1), 50)
        )
        return tuple(_incident_summary(record) for record in records)

    async def get_incident(
        self, *, owner_user_id: str, incident_id: str
    ) -> IncidentSummary:
        record = await self._incidents.get_owned(
            owner_user_id=owner_user_id, incident_id=incident_id
        )
        if record is None:
            raise BridgeResourceNotFound("Incident is unavailable.")
        return _incident_summary(record)

    async def get_diagnostic_status(
        self, *, owner_user_id: str, task_id: str
    ) -> DiagnosticStatus:
        task = await self._require_task(owner_user_id, task_id)
        reports = await self._diagnostics.list_reports(
            owner_user_id=owner_user_id, task_id=task_id
        )
        return DiagnosticStatus(task.id, task.status, task.completed_at, bool(reports))

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport:
        await self._require_task(owner_user_id, task_id)
        reports = await self._diagnostics.list_reports(
            owner_user_id=owner_user_id, task_id=task_id
        )
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

    async def _require_task(self, owner_user_id: str, task_id: str) -> Any:
        task = await self._diagnostics.get_task(
            owner_user_id=owner_user_id, task_id=task_id
        )
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


def build_aiops_bridge_tools(
    *, owner_user_id: str, service: AiopsBridgeService
) -> dict[str, StructuredTool]:
    """Bind authenticated owner identity outside every model-visible schema."""

    async def list_active_incidents(limit: int = 10) -> JsonDict:
        items = await service.list_active_incidents(
            owner_user_id=owner_user_id, limit=limit
        )
        return {"items": [item.to_payload() for item in items]}

    async def get_incident(incident_id: str) -> JsonDict:
        return (
            await service.get_incident(
                owner_user_id=owner_user_id, incident_id=incident_id
            )
        ).to_payload()

    async def get_diagnostic_status(task_id: str) -> JsonDict:
        return (
            await service.get_diagnostic_status(
                owner_user_id=owner_user_id, task_id=task_id
            )
        ).to_payload()

    async def get_diagnostic_report(task_id: str) -> JsonDict:
        return (
            await service.get_diagnostic_report(
                owner_user_id=owner_user_id, task_id=task_id
            )
        ).to_payload()

    async def get_diagnostic_evidence(task_id: str, limit: int = 20) -> JsonDict:
        items = await service.get_diagnostic_evidence(
            owner_user_id=owner_user_id, task_id=task_id, limit=limit
        )
        return {"items": [item.to_payload() for item in items]}

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
    )
    return {
        name: StructuredTool.from_function(
            coroutine=coroutine,
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
