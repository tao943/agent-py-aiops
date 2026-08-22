"""PostgreSQL-backed atomic alert ingestion state machine."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.models import (
    AlertEventModel,
    AlertIncidentModel,
    BackgroundJobModel,
    DiagnosticTaskModel,
)

from .repositories import (
    AlertDisposition,
    AlertIncidentRecord,
    AlertPersistenceError,
    DiagnosticScheduleResult,
    IncidentNotActive,
    IncidentUnavailable,
    IngestionResult,
    IngestionWrite,
)


class SQLAlchemyAlertIngestionRepository:
    """Persist incidents, events, diagnostic tasks, and jobs in one transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active(
        self, *, owner_user_id: str, limit: int
    ) -> list[AlertIncidentRecord]:
        bounded_limit = min(max(limit, 1), 50)
        statement = (
            select(AlertIncidentModel)
            .where(
                AlertIncidentModel.owner_user_id == owner_user_id,
                AlertIncidentModel.status == "active",
            )
            .order_by(AlertIncidentModel.updated_at.desc(), AlertIncidentModel.id.asc())
            .limit(bounded_limit)
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return [_incident_record(row) for row in rows]

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        statement = select(AlertIncidentModel).where(
            AlertIncidentModel.id == incident_id,
            AlertIncidentModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(statement)).one_or_none()
        return _incident_record(row) if row is not None else None

    async def schedule_for_incident(
        self,
        *,
        owner_user_id: str,
        incident_id: str,
        note: str | None,
    ) -> DiagnosticScheduleResult:
        now = _utc_now()
        try:
            async with self._session_factory() as session, session.begin():
                incident = (
                    await session.scalars(
                        select(AlertIncidentModel)
                        .where(
                            AlertIncidentModel.id == incident_id,
                            AlertIncidentModel.owner_user_id == owner_user_id,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if incident is None:
                    raise IncidentUnavailable("Incident is unavailable.")
                if incident.status != "active":
                    raise IncidentNotActive("Incident is not active.")
                if incident.diagnostic_task_id is not None:
                    job = (
                        await session.scalars(
                            select(BackgroundJobModel).where(
                                BackgroundJobModel.owner_user_id == owner_user_id,
                                BackgroundJobModel.resource_type == "aiops_diagnostic",
                                BackgroundJobModel.resource_id
                                == incident.diagnostic_task_id,
                            )
                        )
                    ).one_or_none()
                    if job is None:
                        raise AlertPersistenceError(
                            "Incident diagnostic job is unavailable."
                        )
                    return DiagnosticScheduleResult(
                        incident.diagnostic_task_id, job.id, True
                    )
                suffix = uuid4().hex
                task_id = f"diagnostic_{suffix}"
                job_id = f"job_{suffix}"
                query = _incident_query(incident, note)
                session.add(
                    DiagnosticTaskModel(
                        id=task_id,
                        owner_user_id=owner_user_id,
                        status="accepted",
                        query=query,
                        input_payload={
                            "query": query,
                            "alert": {
                                "id": incident.id,
                                "alertName": incident.alert_name,
                                "service": incident.service,
                                "severity": incident.severity,
                            },
                        },
                        result_payload={},
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                )
                await session.flush()
                session.add(
                    BackgroundJobModel(
                        id=job_id,
                        owner_user_id=owner_user_id,
                        kind="aiops_diagnosis",
                        resource_type="aiops_diagnostic",
                        resource_id=task_id,
                        status="queued",
                        payload={"diagnosticId": task_id},
                        attempt=0,
                        max_attempts=3,
                        timeout_seconds=1800,
                        available_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        cancel_requested_at=None,
                        retry_of_job_id=None,
                        error_message=None,
                        created_at=now,
                        updated_at=now,
                        started_at=None,
                        completed_at=None,
                    )
                )
                incident.diagnostic_task_id = task_id
                incident.updated_at = now
                return DiagnosticScheduleResult(task_id, job_id, False)
        except SQLAlchemyError as exc:
            raise AlertPersistenceError("Alert persistence is unavailable.") from exc

    async def apply(self, write: IngestionWrite) -> IngestionResult:
        try:
            async with self._session_factory() as session, session.begin():
                if write.filtered:
                    await self._insert_event(session, write, None, "filtered")
                    return IngestionResult("filtered", None, None, None)
                active = await self._active_for_update(session, write)
                if write.status == "resolved":
                    return await self._apply_resolved(session, active, write)
                if active is not None:
                    return await self._apply_duplicate(session, active, write)
                return await self._create_or_update_duplicate(session, write)
        except SQLAlchemyError as exc:
            raise AlertPersistenceError("Alert persistence is unavailable.") from exc

    async def _create_or_update_duplicate(
        self,
        session: AsyncSession,
        write: IngestionWrite,
    ) -> IngestionResult:
        incident_id = f"incident_{uuid4().hex}"
        statement = (
            insert(AlertIncidentModel)
            .values(
                id=incident_id,
                owner_user_id=write.owner_user_id,
                source_id=write.source_id,
                group_key_hash=write.group_key_hash,
                status="active",
                alert_name=write.alert_name,
                service=write.service,
                severity=write.severity,
                starts_at=write.starts_at,
                last_seen_at=write.received_at,
                resolved_at=None,
                delivery_count=1,
                diagnostic_task_id=None,
                created_at=write.received_at,
                updated_at=write.received_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    AlertIncidentModel.owner_user_id,
                    AlertIncidentModel.source_id,
                    AlertIncidentModel.group_key_hash,
                ],
                index_where=AlertIncidentModel.status == "active",
            )
            .returning(AlertIncidentModel.id)
        )
        created_id = (await session.execute(statement)).scalar_one_or_none()
        if created_id is None:
            active = await self._active_for_update(session, write)
            if active is None:
                raise AlertPersistenceError("Active incident conflict could not be resolved.")
            return await self._apply_duplicate(session, active, write)
        suffix = created_id.removeprefix("incident_")
        task_id = f"diagnostic_{suffix}"
        job_id = f"job_{suffix}"
        session.add(
            DiagnosticTaskModel(
                id=task_id,
                owner_user_id=write.owner_user_id,
                status="accepted",
                query=write.query,
                input_payload={"query": write.query, "alert": write.safe_alert},
                result_payload={},
                created_at=write.received_at,
                updated_at=write.received_at,
                completed_at=None,
            )
        )
        session.add(
            BackgroundJobModel(
                id=job_id,
                owner_user_id=write.owner_user_id,
                kind="aiops_diagnosis",
                resource_type="aiops_diagnostic",
                resource_id=task_id,
                status="queued",
                payload={"diagnosticId": task_id},
                attempt=0,
                max_attempts=3,
                timeout_seconds=1800,
                available_at=write.received_at,
                lease_owner=None,
                lease_expires_at=None,
                cancel_requested_at=None,
                retry_of_job_id=None,
                error_message=None,
                created_at=write.received_at,
                updated_at=write.received_at,
                started_at=None,
                completed_at=None,
            )
        )
        incident = (
            await session.scalars(
                select(AlertIncidentModel)
                .where(AlertIncidentModel.id == created_id)
                .with_for_update()
            )
        ).one()
        incident.diagnostic_task_id = task_id
        await self._insert_event(session, write, created_id, "incident_created")
        return IngestionResult("incident_created", created_id, task_id, job_id)

    async def _apply_duplicate(
        self,
        session: AsyncSession,
        incident: AlertIncidentModel,
        write: IngestionWrite,
    ) -> IngestionResult:
        incident.delivery_count += 1
        incident.last_seen_at = write.received_at
        incident.updated_at = write.received_at
        incident.alert_name = write.alert_name
        incident.service = write.service
        incident.severity = write.severity
        await self._insert_event(session, write, incident.id, "duplicate_updated")
        task_id = incident.diagnostic_task_id
        return IngestionResult(
            "duplicate_updated",
            incident.id,
            task_id,
            _job_id(task_id),
        )

    async def _apply_resolved(
        self,
        session: AsyncSession,
        incident: AlertIncidentModel | None,
        write: IngestionWrite,
    ) -> IngestionResult:
        if incident is None:
            await self._insert_event(session, write, None, "orphan_resolved")
            return IngestionResult("orphan_resolved", None, None, None)
        incident.delivery_count += 1
        incident.status = "resolved"
        incident.last_seen_at = write.received_at
        incident.resolved_at = write.received_at
        incident.updated_at = write.received_at
        await self._insert_event(session, write, incident.id, "incident_resolved")
        task_id = incident.diagnostic_task_id
        return IngestionResult(
            "incident_resolved",
            incident.id,
            task_id,
            _job_id(task_id),
        )

    async def _active_for_update(
        self,
        session: AsyncSession,
        write: IngestionWrite,
    ) -> AlertIncidentModel | None:
        statement = (
            select(AlertIncidentModel)
            .where(
                AlertIncidentModel.owner_user_id == write.owner_user_id,
                AlertIncidentModel.source_id == write.source_id,
                AlertIncidentModel.group_key_hash == write.group_key_hash,
                AlertIncidentModel.status == "active",
            )
            .with_for_update()
        )
        return (await session.scalars(statement)).one_or_none()

    async def _insert_event(
        self,
        session: AsyncSession,
        write: IngestionWrite,
        incident_id: str | None,
        disposition: AlertDisposition,
    ) -> None:
        identity = ":".join(
            (write.owner_user_id, write.source_id, write.status, write.payload_sha256)
        )
        event_id = f"alert_event_{sha256(identity.encode('utf-8')).hexdigest()}"
        statement = (
            insert(AlertEventModel)
            .values(
                id=event_id,
                incident_id=incident_id,
                owner_user_id=write.owner_user_id,
                source_id=write.source_id,
                status=write.status,
                disposition=disposition,
                payload_sha256=write.payload_sha256,
                normalized_payload=write.normalized_payload,
                received_at=write.received_at,
            )
            .on_conflict_do_nothing(index_elements=[AlertEventModel.id])
        )
        await session.execute(statement)


def _job_id(task_id: str | None) -> str | None:
    if task_id is None:
        return None
    return f"job_{task_id.removeprefix('diagnostic_')}"


def _incident_record(row: AlertIncidentModel) -> AlertIncidentRecord:
    return AlertIncidentRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        status=row.status,
        alert_name=row.alert_name,
        service=row.service,
        severity=row.severity,
        last_seen_at=row.last_seen_at,
        diagnostic_task_id=row.diagnostic_task_id,
    )


def _incident_query(incident: AlertIncidentModel, note: str | None) -> str:
    query = (
        f"Investigate {incident.alert_name} affecting {incident.service}. "
        f"Severity: {incident.severity}."
    )
    bounded_note = (note or "").strip()[:1000]
    return f"{query} Operator note: {bounded_note}" if bounded_note else query


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
