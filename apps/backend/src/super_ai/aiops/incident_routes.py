"""Owner-scoped HTTP projection for the event-first AIOps workspace."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from super_ai.alert_ingestion.repositories import (
    AlertIncidentQueryRepository,
    AlertIncidentRecord,
    IncidentDiagnosticScheduler,
    IncidentNotActive,
    InvalidIncidentCursor,
)
from super_ai.api.responses import ApiErrorException, success_response
from super_ai.auth.repositories import UserRecord

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_DIAGNOSTIC_STATUSES = frozenset({"accepted", "running", "succeeded", "failed", "cancelled"})
_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
_RECOVERY_TERMINAL = frozenset(
    {
        "recovered",
        "denied",
        "rejected",
        "expired",
        "cancelled",
        "verification_failed",
        "manual_intervention",
    }
)


class RuntimeStarter(Protocol):
    async def start(self) -> None: ...


class DiagnoseIncidentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)


def create_incident_router(
    *,
    current_user_dependency: Callable[..., Awaitable[UserRecord]],
    repository: AlertIncidentQueryRepository,
    scheduler: IncidentDiagnosticScheduler,
    runtime: RuntimeStarter,
) -> APIRouter:
    """Create the authenticated Incident list/detail/diagnose router."""

    router = APIRouter(prefix="/aiops/incidents", tags=["aiops-incidents"])
    user_dependency = Depends(current_user_dependency)

    @router.get("")
    async def list_incidents(
        request: Request,
        user: UserRecord = user_dependency,
        status: Literal["active", "resolved", "all"] = "active",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> object:
        try:
            page = await repository.list_owned(
                owner_user_id=user.id,
                status=status,
                limit=limit,
                cursor=cursor,
            )
        except InvalidIncidentCursor as exc:
            raise ApiErrorException("VALIDATION_INVALID_ARGUMENT") from exc
        return success_response(
            request,
            {
                "items": [_incident_payload(item) for item in page.items],
                "nextCursor": page.next_cursor,
            },
        )

    @router.get("/{incident_id}")
    async def get_incident(
        request: Request,
        incident_id: str,
        user: UserRecord = user_dependency,
    ) -> object:
        incident = await _owned_incident(repository, user.id, incident_id)
        return success_response(
            request,
            {
                "incident": {
                    **_incident_payload(incident),
                    "summary": incident.summary,
                    "alertLabels": {},
                    "alertAnnotations": {},
                    "evidenceChain": None,
                    "recoveryIntent": None,
                    "recoveryEvents": [],
                }
            },
        )

    @router.post("/{incident_id}:diagnose")
    async def diagnose_incident(
        request: Request,
        incident_id: str,
        body: DiagnoseIncidentBody | None = None,
        user: UserRecord = user_dependency,
    ) -> object:
        await _owned_incident(repository, user.id, incident_id)
        try:
            result = await scheduler.schedule_for_incident(
                owner_user_id=user.id,
                incident_id=incident_id,
                note=body.note if body is not None else None,
            )
        except IncidentNotActive as exc:
            raise ApiErrorException("BUSINESS_CONFLICT") from exc
        if not result.reused:
            await runtime.start()
        return success_response(
            request,
            {
                "incidentId": incident_id,
                "diagnosticTaskId": result.diagnostic_task_id,
                "backgroundJobId": result.background_job_id,
                "reused": result.reused,
            },
            status_code=200 if result.reused else 202,
        )

    return router


async def _owned_incident(
    repository: AlertIncidentQueryRepository,
    owner_user_id: str,
    incident_id: str,
) -> AlertIncidentRecord:
    if not _ID.fullmatch(incident_id):
        raise ApiErrorException("BUSINESS_NOT_FOUND")
    incident = await repository.get_owned(
        owner_user_id=owner_user_id,
        incident_id=incident_id,
    )
    if incident is None:
        raise ApiErrorException("BUSINESS_NOT_FOUND")
    return incident


def _incident_payload(record: AlertIncidentRecord) -> dict[str, object]:
    first_seen_at = record.first_seen_at or record.last_seen_at
    updated_at = record.updated_at or record.last_seen_at
    diagnostic_status = (
        record.diagnostic_status if record.diagnostic_status in _DIAGNOSTIC_STATUSES else None
    )
    severity = record.severity.lower()
    recovery_status = record.recovery_execution_status or "not_available"
    return {
        "id": record.id,
        "status": record.status,
        "alertName": record.alert_name,
        "service": record.service or None,
        "severity": severity if severity in _SEVERITIES else "unknown",
        "firstSeenAt": first_seen_at.isoformat(),
        "lastSeenAt": record.last_seen_at.isoformat(),
        "updatedAt": updated_at.isoformat(),
        "deliveryCount": record.delivery_count,
        "diagnosticTaskId": record.diagnostic_task_id,
        "diagnosticStatus": diagnostic_status,
        "verificationStatus": _verification_status(record.verification_status),
        "currentStage": _current_stage(record, recovery_status),
        "source": record.source_id or None,
        "environment": record.environment,
        "assignee": None,
        "agentMode": record.agent_mode,
        "approvalStatus": record.approval_status,
        "recoveryMode": record.recovery_mode,
        "recoveryExecutionStatus": recovery_status,
        "recoveryIntentId": record.recovery_intent_id,
        "productionRecoveryExecution": record.recovery_intent_id is not None,
    }


def _verification_status(value: str) -> Literal["pending", "passed", "failed", "not_available"]:
    if value in {"pending", "passed", "failed"}:
        return value  # type: ignore[return-value]
    return "not_available"


def _current_stage(record: AlertIncidentRecord, recovery_status: str) -> str:
    if record.status == "resolved":
        return "closed"
    if recovery_status in {"verifying", "recovered", "verification_failed"}:
        return "verification"
    if recovery_status != "not_available":
        return "recovery" if recovery_status not in _RECOVERY_TERMINAL else "decision"
    if record.diagnostic_status in {"accepted", "running"}:
        return "investigation"
    if record.diagnostic_status in {"succeeded", "failed", "cancelled"}:
        return "decision"
    return "alert"
