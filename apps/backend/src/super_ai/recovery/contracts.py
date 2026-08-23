"""Immutable domain contracts for governed production recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

RecoveryAction = Literal["restart_compose_service", "terminate_postgres_blocker"]
RecoveryRiskTier = Literal["low", "high"]
RecoveryStatus = Literal[
    "proposed",
    "awaiting_approval",
    "queued",
    "revalidating",
    "executing",
    "verifying",
    "recovered",
    "denied",
    "rejected",
    "expired",
    "cancelled",
    "verification_failed",
    "manual_intervention",
]
RecoveryCheckStatus = Literal["passed", "failed", "pending"]


def canonical_json(value: object) -> str:
    """Serialize JSON-compatible values with a stable byte representation."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def proposal_fingerprint(
    *,
    owner_user_id: str,
    incident_id: str,
    diagnostic_task_id: str,
    report_id: str,
    action: RecoveryAction,
    target_key: str,
    canonical_arguments: Mapping[str, object],
    evidence_ids: Sequence[str],
) -> str:
    """Build the immutable identity of one grounded recovery proposal."""

    material = {
        "action": action,
        "arguments": dict(canonical_arguments),
        "diagnosticTaskId": diagnostic_task_id,
        "evidenceIds": sorted(set(evidence_ids)),
        "incidentId": incident_id,
        "ownerUserId": owner_user_id,
        "reportId": report_id,
        "targetKey": target_key,
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryCheck:
    key: str
    status: RecoveryCheckStatus
    safe_summary: str
    checked_at: datetime | None

    def public_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "status": self.status,
            "safeSummary": self.safe_summary,
            "checkedAt": self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass(frozen=True, slots=True)
class RecoveryIntentRecord:
    id: str
    owner_user_id: str
    incident_id: str
    diagnostic_task_id: str
    report_id: str
    action: RecoveryAction
    target_key: str
    risk_tier: RecoveryRiskTier
    automatic_eligible: bool
    approval_required: bool
    status: RecoveryStatus
    proposal_fingerprint: str
    evidence_ids: tuple[str, ...]
    canonical_arguments: Mapping[str, object]
    trusted_snapshot: Mapping[str, object]
    created_at: datetime
    approval_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    safe_reason_code: str | None
    execution_summary: str | None
    verification: tuple[RecoveryCheck, ...]

    def public_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "incidentId": self.incident_id,
            "diagnosticTaskId": self.diagnostic_task_id,
            "reportId": self.report_id,
            "action": self.action,
            "targetKey": self.target_key,
            "riskTier": self.risk_tier,
            "automaticEligible": self.automatic_eligible,
            "approvalRequired": self.approval_required,
            "status": self.status,
            "proposalFingerprint": self.proposal_fingerprint,
            "createdAt": self.created_at.isoformat(),
            "approvalExpiresAt": (
                self.approval_expires_at.isoformat()
                if self.approval_expires_at
                else None
            ),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "safeReasonCode": self.safe_reason_code,
            "executionSummary": self.execution_summary,
            "verification": [check.public_payload() for check in self.verification],
        }


@dataclass(frozen=True, slots=True)
class RecoveryPolicyDecision:
    allowed: bool
    next_status: RecoveryStatus
    safe_reason_code: str | None
    automatic_eligible: bool
    approval_required: bool


@dataclass(frozen=True, slots=True)
class RecoveryExecutionResult:
    succeeded: bool
    outcome_known: bool
    safe_summary: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RecoveryVerificationResult:
    passed: bool
    checks: tuple[RecoveryCheck, ...]
    safe_summary: str


@dataclass(frozen=True, slots=True)
class RecoveryAuditEventRecord:
    sequence: int
    event_type: str
    from_status: RecoveryStatus | None
    to_status: RecoveryStatus
    safe_reason_code: str | None
    safe_summary: str
    duration_ms: int | None
    created_at: datetime

    def public_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "type": self.event_type,
            "fromStatus": self.from_status,
            "toStatus": self.to_status,
            "safeReasonCode": self.safe_reason_code,
            "safeSummary": self.safe_summary,
            "durationMs": self.duration_ms,
            "createdAt": self.created_at.isoformat(),
        }
