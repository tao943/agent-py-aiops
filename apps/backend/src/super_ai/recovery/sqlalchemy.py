"""Conflict-safe PostgreSQL recovery state machine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.models import (
    BackgroundJobModel,
    ProductionRecoveryApprovalModel,
    ProductionRecoveryAuditEventModel,
    ProductionRecoveryIntentModel,
)
from super_ai.recovery.contracts import (
    RecoveryAction,
    RecoveryAuditEventRecord,
    RecoveryCheck,
    RecoveryCheckStatus,
    RecoveryIntentRecord,
    RecoveryRiskTier,
    RecoveryStatus,
)
from super_ai.recovery.repository import (
    RecoveryApprovalRecord,
    RecoveryIntentCreate,
    RecoveryIntentCreateResult,
    RecoveryStateConflict,
)

_ACTIVE_STATUSES = (
    "proposed",
    "awaiting_approval",
    "queued",
    "revalidating",
    "executing",
    "verifying",
)
FailureInjector = Callable[[str], None]


class SQLAlchemyRecoveryIntentRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._failure_injector = failure_injector

    async def create_intent_with_job_and_event(
        self,
        request: RecoveryIntentCreate,
        *,
        background_job_id: str | None,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentCreateResult:
        values = {
            "id": request.id,
            "owner_user_id": request.owner_user_id,
            "incident_id": request.incident_id,
            "diagnostic_task_id": request.diagnostic_task_id,
            "report_id": request.report_id,
            "action": request.action,
            "target_key": request.target_key,
            "canonical_arguments": request.canonical_arguments,
            "proposal_fingerprint": request.proposal_fingerprint,
            "evidence_ids": list(request.evidence_ids),
            "validator_origin": request.validator_origin,
            "policy_authorization_code": request.policy_authorization_code,
            "risk_tier": request.risk_tier,
            "automatic_eligible": request.automatic_eligible,
            "approval_required": request.approval_required,
            "status": request.status,
            "execution_key": None,
            "background_job_id": None,
            "approval_expires_at": None,
            "trusted_snapshot": request.trusted_snapshot,
            "execution_summary": None,
            "verification_checks": [],
            "safe_reason_code": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        async with self._session_factory() as session, session.begin():
            inserted = await session.scalar(
                postgresql_insert(ProductionRecoveryIntentModel)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(ProductionRecoveryIntentModel.id)
            )
            if inserted is None:
                existing = await session.scalar(
                    select(ProductionRecoveryIntentModel).where(
                        ProductionRecoveryIntentModel.owner_user_id
                        == request.owner_user_id,
                        ProductionRecoveryIntentModel.proposal_fingerprint
                        == request.proposal_fingerprint,
                        ProductionRecoveryIntentModel.status.in_(_ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise RecoveryStateConflict("recovery_intent_conflict")
                return RecoveryIntentCreateResult(_intent_record(existing), True)
            self._fail("after_intent")
            if background_job_id is not None:
                session.add(_recovery_job(background_job_id, request, now))
                await session.flush()
                await session.execute(
                    update(ProductionRecoveryIntentModel)
                    .where(ProductionRecoveryIntentModel.id == request.id)
                    .values(background_job_id=background_job_id)
                )
                self._fail("after_job")
            session.add(
                _event(
                    event_id=event_id,
                    intent_id=request.id,
                    owner_user_id=request.owner_user_id,
                    sequence=1,
                    event_type="intent.created",
                    from_status=None,
                    to_status=request.status,
                    safe_reason_code=None,
                    safe_summary="Recovery intent created from grounded diagnostic facts.",
                    duration_ms=None,
                    now=now,
                )
            )
            self._fail("after_event")
            row = await session.get(ProductionRecoveryIntentModel, request.id)
            if row is None:
                raise RuntimeError("recovery_intent_insert_failed")
            return RecoveryIntentCreateResult(_intent_record(row), False)

    async def get_owned(
        self, *, owner_user_id: str, intent_id: str
    ) -> RecoveryIntentRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ProductionRecoveryIntentModel).where(
                    ProductionRecoveryIntentModel.id == intent_id,
                    ProductionRecoveryIntentModel.owner_user_id == owner_user_id,
                )
            )
            return _intent_record(row) if row is not None else None

    async def approve_with_job_and_event(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        approval_id: str,
        confirmation_fingerprint: str,
        background_job_id: str,
        event_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> RecoveryIntentRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(ProductionRecoveryIntentModel)
                .where(
                    ProductionRecoveryIntentModel.id == intent_id,
                    ProductionRecoveryIntentModel.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
            if row is None:
                return None
            if row.status == "queued":
                return _intent_record(row)
            if row.status != "awaiting_approval":
                raise RecoveryStateConflict("recovery_invalid_transition")
            session.add(
                ProductionRecoveryApprovalModel(
                    id=approval_id,
                    intent_id=intent_id,
                    owner_user_id=owner_user_id,
                    approver_user_id=owner_user_id,
                    incident_id=row.incident_id,
                    proposal_fingerprint=row.proposal_fingerprint,
                    confirmation_fingerprint=confirmation_fingerprint,
                    decision="approved",
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            self._fail("after_approval")
            row.status = "queued"
            row.approval_expires_at = expires_at
            row.updated_at = now
            session.add(_recovery_job_for_row(background_job_id, row, now))
            await session.flush()
            row.background_job_id = background_job_id
            self._fail("after_job")
            sequence = await _next_sequence(session, intent_id)
            session.add(
                _event(
                    event_id=event_id,
                    intent_id=intent_id,
                    owner_user_id=owner_user_id,
                    sequence=sequence,
                    event_type="intent.approved",
                    from_status="awaiting_approval",
                    to_status="queued",
                    safe_reason_code=None,
                    safe_summary="Recovery intent approved and queued.",
                    duration_ms=None,
                    now=now,
                )
            )
            self._fail("after_event")
            await session.flush()
            return _intent_record(row)

    async def transition(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        expected_statuses: tuple[RecoveryStatus, ...],
        to_status: RecoveryStatus,
        event_id: str,
        event_type: str,
        safe_reason_code: str | None,
        safe_summary: str,
        now: datetime,
        duration_ms: int | None = None,
    ) -> RecoveryIntentRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(ProductionRecoveryIntentModel)
                .where(
                    ProductionRecoveryIntentModel.id == intent_id,
                    ProductionRecoveryIntentModel.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
            if row is None:
                return None
            if row.status not in expected_statuses:
                raise RecoveryStateConflict("recovery_invalid_transition")
            from_status = row.status
            row.status = to_status
            row.safe_reason_code = safe_reason_code
            row.updated_at = now
            if to_status == "executing" and row.started_at is None:
                row.started_at = now
            if to_status in {
                "recovered",
                "denied",
                "rejected",
                "expired",
                "cancelled",
                "verification_failed",
                "manual_intervention",
            }:
                row.completed_at = now
            sequence = await _next_sequence(session, intent_id)
            session.add(
                _event(
                    event_id=event_id,
                    intent_id=intent_id,
                    owner_user_id=owner_user_id,
                    sequence=sequence,
                    event_type=event_type,
                    from_status=from_status,
                    to_status=to_status,
                    safe_reason_code=safe_reason_code,
                    safe_summary=safe_summary,
                    duration_ms=duration_ms,
                    now=now,
                )
            )
            await session.flush()
            return _intent_record(row)

    async def list_events(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        after_sequence: int = 0,
    ) -> list[RecoveryAuditEventRecord]:
        async with self._session_factory() as session:
            owned = await session.scalar(
                select(ProductionRecoveryIntentModel.id).where(
                    ProductionRecoveryIntentModel.id == intent_id,
                    ProductionRecoveryIntentModel.owner_user_id == owner_user_id,
                )
            )
            if owned is None:
                return []
            rows = (
                await session.scalars(
                    select(ProductionRecoveryAuditEventModel)
                    .where(
                        ProductionRecoveryAuditEventModel.intent_id == intent_id,
                        ProductionRecoveryAuditEventModel.owner_user_id == owner_user_id,
                        ProductionRecoveryAuditEventModel.sequence > after_sequence,
                    )
                    .order_by(ProductionRecoveryAuditEventModel.sequence)
                )
            ).all()
            return [_audit_record(row) for row in rows]

    async def reject(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentRecord | None:
        return await self.transition(
            owner_user_id=owner_user_id,
            intent_id=intent_id,
            expected_statuses=("awaiting_approval",),
            to_status="rejected",
            event_id=event_id,
            event_type="intent.rejected",
            safe_reason_code="owner_rejected",
            safe_summary="Recovery intent rejected by its incident owner.",
            now=now,
        )

    async def cancel_before_claim(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentRecord | None:
        return await self.transition(
            owner_user_id=owner_user_id,
            intent_id=intent_id,
            expected_statuses=("queued",),
            to_status="cancelled",
            event_id=event_id,
            event_type="intent.cancelled",
            safe_reason_code="owner_cancelled",
            safe_summary="Recovery intent cancelled before execution claim.",
            now=now,
        )

    async def get_current_approval(
        self, *, owner_user_id: str, intent_id: str
    ) -> RecoveryApprovalRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ProductionRecoveryApprovalModel).where(
                    ProductionRecoveryApprovalModel.intent_id == intent_id,
                    ProductionRecoveryApprovalModel.owner_user_id == owner_user_id,
                    ProductionRecoveryApprovalModel.decision == "approved",
                )
            )
            if row is None:
                return None
            return RecoveryApprovalRecord(
                id=row.id,
                intent_id=row.intent_id,
                owner_user_id=row.owner_user_id,
                approver_user_id=row.approver_user_id,
                incident_id=row.incident_id,
                proposal_fingerprint=row.proposal_fingerprint,
                decision=row.decision,
                created_at=row.created_at,
                expires_at=row.expires_at,
            )

    def _fail(self, stage: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(stage)


async def _next_sequence(session: AsyncSession, intent_id: str) -> int:
    current = await session.scalar(
        select(func.max(ProductionRecoveryAuditEventModel.sequence)).where(
            ProductionRecoveryAuditEventModel.intent_id == intent_id
        )
    )
    return int(current or 0) + 1


def _recovery_job(
    job_id: str, request: RecoveryIntentCreate, now: datetime
) -> BackgroundJobModel:
    return BackgroundJobModel(
        id=job_id,
        owner_user_id=request.owner_user_id,
        kind="production_recovery",
        resource_type="production_recovery_intent",
        resource_id=request.id,
        status="queued",
        payload={"intentId": request.id},
        attempt=0,
        max_attempts=1,
        timeout_seconds=300,
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


def _recovery_job_for_row(
    job_id: str, row: ProductionRecoveryIntentModel, now: datetime
) -> BackgroundJobModel:
    request = RecoveryIntentCreate(
        id=row.id,
        owner_user_id=row.owner_user_id,
        incident_id=row.incident_id,
        diagnostic_task_id=row.diagnostic_task_id,
        report_id=row.report_id,
        action=cast(RecoveryAction, row.action),
        target_key=row.target_key,
        canonical_arguments=dict(row.canonical_arguments),
        proposal_fingerprint=row.proposal_fingerprint,
        evidence_ids=tuple(row.evidence_ids),
        validator_origin=row.validator_origin,
        policy_authorization_code=row.policy_authorization_code,
        risk_tier=cast(RecoveryRiskTier, row.risk_tier),
        automatic_eligible=row.automatic_eligible,
        approval_required=row.approval_required,
        status="queued",
        trusted_snapshot=dict(row.trusted_snapshot),
    )
    return _recovery_job(job_id, request, now)


def _event(
    *,
    event_id: str,
    intent_id: str,
    owner_user_id: str,
    sequence: int,
    event_type: str,
    from_status: RecoveryStatus | None,
    to_status: RecoveryStatus,
    safe_reason_code: str | None,
    safe_summary: str,
    duration_ms: int | None,
    now: datetime,
) -> ProductionRecoveryAuditEventModel:
    return ProductionRecoveryAuditEventModel(
        event_id=event_id,
        intent_id=intent_id,
        owner_user_id=owner_user_id,
        sequence=sequence,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        safe_reason_code=safe_reason_code,
        safe_summary=safe_summary,
        duration_ms=duration_ms,
        created_at=now,
    )


def _intent_record(row: ProductionRecoveryIntentModel) -> RecoveryIntentRecord:
    checks: list[RecoveryCheck] = []
    for raw in row.verification_checks:
        checked_at_raw = raw.get("checkedAt")
        checked_at = (
            datetime.fromisoformat(str(checked_at_raw)) if checked_at_raw else None
        )
        checks.append(
            RecoveryCheck(
                key=str(raw.get("key", "unknown")),
                status=cast(RecoveryCheckStatus, raw.get("status", "pending")),
                safe_summary=str(raw.get("safeSummary", "")),
                checked_at=checked_at,
            )
        )
    return RecoveryIntentRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        incident_id=row.incident_id,
        diagnostic_task_id=row.diagnostic_task_id,
        report_id=row.report_id,
        action=cast(RecoveryAction, row.action),
        target_key=row.target_key,
        risk_tier=cast(RecoveryRiskTier, row.risk_tier),
        automatic_eligible=row.automatic_eligible,
        approval_required=row.approval_required,
        status=cast(RecoveryStatus, row.status),
        proposal_fingerprint=row.proposal_fingerprint,
        evidence_ids=tuple(row.evidence_ids),
        canonical_arguments=dict(row.canonical_arguments),
        trusted_snapshot=dict(row.trusted_snapshot),
        created_at=row.created_at,
        approval_expires_at=row.approval_expires_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        safe_reason_code=row.safe_reason_code,
        execution_summary=row.execution_summary,
        verification=tuple(checks),
    )


def _audit_record(row: ProductionRecoveryAuditEventModel) -> RecoveryAuditEventRecord:
    return RecoveryAuditEventRecord(
        sequence=row.sequence,
        event_type=row.event_type,
        from_status=cast(RecoveryStatus | None, row.from_status),
        to_status=cast(RecoveryStatus, row.to_status),
        safe_reason_code=row.safe_reason_code,
        safe_summary=row.safe_summary,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
    )
