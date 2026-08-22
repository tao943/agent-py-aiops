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
IncidentStatus = Literal["active", "resolved"]
VerificationStatus = Literal["pending", "passed", "failed", "not_applicable"]


class AlertPersistenceError(RuntimeError):
    """Safe persistence failure exposed at the HTTP boundary."""


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
    scenario_id: str | None = None
    run_id: str | None = None


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


@dataclass(frozen=True, slots=True)
class LiveAlertLifecycle:
    incident_id: str
    diagnostic_task_id: str
    background_job_id: str
    report_id: str | None
    status: IncidentStatus
    verification_status: VerificationStatus
    verified_at: datetime | None
    verification_summary: str | None

    @property
    def closed_verified(self) -> bool:
        return self.status == "resolved" and self.verification_status == "passed"


class AlertIngestionRepository(Protocol):
    async def apply(self, write: IngestionWrite) -> IngestionResult:
        """Apply exactly one authenticated delivery transactionally."""
        ...

    async def get_live_lifecycle(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
    ) -> LiveAlertLifecycle | None: ...

    async def record_verification(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
        status: Literal["passed", "failed"],
        summary: str,
        verified_at: datetime,
    ) -> LiveAlertLifecycle: ...
