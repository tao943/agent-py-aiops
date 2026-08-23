"""Persistence boundary for governed production recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from super_ai.recovery.contracts import (
    RecoveryAction,
    RecoveryAuditEventRecord,
    RecoveryCheck,
    RecoveryIntentRecord,
    RecoveryRiskTier,
    RecoveryStatus,
)


class RecoveryStateConflict(RuntimeError):
    """The persisted state no longer permits the requested transition."""


@dataclass(frozen=True, slots=True)
class RecoveryIntentCreate:
    id: str
    owner_user_id: str
    incident_id: str
    diagnostic_task_id: str
    report_id: str
    action: RecoveryAction
    target_key: str
    canonical_arguments: dict[str, object]
    proposal_fingerprint: str
    evidence_ids: tuple[str, ...]
    validator_origin: str
    policy_authorization_code: str
    risk_tier: RecoveryRiskTier
    automatic_eligible: bool
    approval_required: bool
    status: RecoveryStatus
    trusted_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class RecoveryIntentCreateResult:
    intent: RecoveryIntentRecord
    reused: bool


@dataclass(frozen=True, slots=True)
class RecoveryApprovalRecord:
    id: str
    intent_id: str
    owner_user_id: str
    approver_user_id: str
    incident_id: str
    proposal_fingerprint: str
    decision: str
    created_at: datetime
    expires_at: datetime


class RecoveryIntentRepository(Protocol):
    async def create_intent_with_job_and_event(
        self,
        request: RecoveryIntentCreate,
        *,
        background_job_id: str | None,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentCreateResult: ...

    async def get_owned(
        self, *, owner_user_id: str, intent_id: str
    ) -> RecoveryIntentRecord | None: ...

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
    ) -> RecoveryIntentRecord | None: ...

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
        execution_key: str | None = None,
        execution_summary: str | None = None,
        verification_checks: tuple[RecoveryCheck, ...] | None = None,
    ) -> RecoveryIntentRecord | None: ...

    async def reject(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentRecord | None: ...

    async def cancel_before_claim(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentRecord | None: ...

    async def list_events(
        self,
        *,
        owner_user_id: str,
        intent_id: str,
        after_sequence: int = 0,
    ) -> list[RecoveryAuditEventRecord]: ...

    async def get_current_approval(
        self, *, owner_user_id: str, intent_id: str
    ) -> RecoveryApprovalRecord | None: ...
