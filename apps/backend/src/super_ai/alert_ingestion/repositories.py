"""Repository contracts and records for alert ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .domain import AlertDeliveryStatus

AlertDisposition = Literal[
    "incident_created",
    "duplicate_updated",
    "incident_resolved",
    "filtered",
    "orphan_resolved",
]
RedisMode = Literal["primary", "contended", "degraded", "postgresql"]


class AlertPersistenceError(RuntimeError):
    """Safe persistence failure exposed at the HTTP boundary."""


class IncidentUnavailable(LookupError):
    """An owner-scoped Incident is absent or inaccessible."""


class IncidentNotActive(RuntimeError):
    """A diagnostic cannot be scheduled for a resolved Incident."""


@dataclass(frozen=True, slots=True)
class AlertIncidentRecord:
    """Minimal owner-scoped Incident projection safe for application queries."""

    id: str
    owner_user_id: str
    status: str
    alert_name: str
    service: str
    severity: str
    last_seen_at: datetime
    diagnostic_task_id: str | None


class AlertIncidentQueryRepository(Protocol):
    async def list_active(
        self, *, owner_user_id: str, limit: int
    ) -> list[AlertIncidentRecord]: ...

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None: ...


@dataclass(frozen=True, slots=True)
class DiagnosticScheduleResult:
    diagnostic_task_id: str
    background_job_id: str
    reused: bool


class IncidentDiagnosticScheduler(Protocol):
    async def schedule_for_incident(
        self,
        *,
        owner_user_id: str,
        incident_id: str,
        note: str | None,
    ) -> DiagnosticScheduleResult: ...


@dataclass(frozen=True, slots=True)
class IngestionWrite:
    owner_user_id: str
    source_id: str
    status: AlertDeliveryStatus
    group_key_hash: str
    payload_sha256: str
    normalized_payload: dict[str, object]
    query: str
    safe_alert: dict[str, object]
    filtered: bool
    received_at: datetime
    alert_name: str
    service: str
    severity: str
    starts_at: datetime | None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    disposition: AlertDisposition
    incident_id: str | None
    diagnostic_task_id: str | None
    background_job_id: str | None
    redis_mode: RedisMode = "postgresql"

    @property
    def duplicate(self) -> bool:
        return self.disposition == "duplicate_updated"

    @property
    def filtered(self) -> bool:
        return self.disposition == "filtered"


class AlertIngestionRepository(Protocol):
    async def apply(self, write: IngestionWrite) -> IngestionResult:
        """Apply exactly one authenticated delivery transactionally."""
        ...
