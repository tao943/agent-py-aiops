"""Deterministic creation and pre-execution recovery policy gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from super_ai.recovery.config import ProductionRecoverySettings
from super_ai.recovery.contracts import RecoveryIntentRecord, RecoveryPolicyDecision
from super_ai.recovery.proposal_adapter import RecoveryProposal
from super_ai.recovery.repository import RecoveryApprovalRecord


@dataclass(frozen=True, slots=True)
class RecoveryCreationFacts:
    diagnostic_succeeded: bool
    report_available: bool
    incident_active: bool
    evidence_sufficient: bool
    deterministic_validation_passed: bool
    proposal: RecoveryProposal | None


@dataclass(frozen=True, slots=True)
class RecoveryExecutionFacts:
    incident_active: bool
    proposal_fingerprint: str


class RecoveryPolicy:
    def evaluate_creation(
        self,
        facts: RecoveryCreationFacts,
        settings: ProductionRecoverySettings,
    ) -> RecoveryPolicyDecision:
        hard_gates = (
            (facts.diagnostic_succeeded, "diagnostic_incomplete"),
            (facts.report_available, "report_unavailable"),
            (facts.incident_active, "incident_not_active"),
            (facts.evidence_sufficient, "evidence_insufficient"),
            (facts.deterministic_validation_passed, "validation_failed"),
            (facts.proposal is not None, "proposal_not_grounded"),
            (settings.enabled, "recovery_disabled"),
        )
        for passed, reason in hard_gates:
            if not passed:
                return _denied_creation(reason)
        proposal = facts.proposal
        assert proposal is not None
        compose = settings.compose_targets.get(proposal.target_key)
        postgres = settings.postgres_targets.get(proposal.target_key)
        if compose is None and postgres is None:
            return _denied_creation("target_not_allowlisted")
        if compose is not None:
            if proposal.action != "restart_compose_service":
                return _denied_creation("action_target_mismatch")
            if not compose.automatic_recovery_enabled:
                return _denied_creation("automatic_recovery_disabled")
            return RecoveryPolicyDecision(True, "queued", None, True, False)
        if proposal.action != "terminate_postgres_blocker":
            return _denied_creation("action_target_mismatch")
        return RecoveryPolicyDecision(True, "awaiting_approval", None, False, True)

    def evaluate_execution(
        self,
        facts: RecoveryExecutionFacts,
        intent: RecoveryIntentRecord,
        settings: ProductionRecoverySettings,
        approval: RecoveryApprovalRecord | None,
        *,
        now: datetime,
    ) -> RecoveryPolicyDecision:
        if not facts.incident_active:
            return _manual("incident_not_active", intent)
        if not settings.enabled:
            return _manual("recovery_disabled", intent)
        if facts.proposal_fingerprint != intent.proposal_fingerprint:
            return _manual("proposal_changed", intent)
        compose = settings.compose_targets.get(intent.target_key)
        postgres = settings.postgres_targets.get(intent.target_key)
        if compose is None and postgres is None:
            return _manual("target_not_allowlisted", intent)
        if compose is not None:
            if intent.action != "restart_compose_service":
                return _manual("action_target_mismatch", intent)
            if not compose.automatic_recovery_enabled:
                return _manual("automatic_recovery_disabled", intent)
        else:
            if intent.action != "terminate_postgres_blocker":
                return _manual("action_target_mismatch", intent)
            approval_reason = _invalid_approval_reason(approval, intent, now)
            if approval_reason is not None:
                return _manual(approval_reason, intent)
        return RecoveryPolicyDecision(
            True,
            "revalidating",
            None,
            intent.automatic_eligible,
            intent.approval_required,
        )


def _invalid_approval_reason(
    approval: RecoveryApprovalRecord | None,
    intent: RecoveryIntentRecord,
    now: datetime,
) -> str | None:
    if approval is None or approval.decision != "approved":
        return "approval_required"
    if approval.expires_at <= now:
        return "approval_expired"
    if approval.proposal_fingerprint != intent.proposal_fingerprint:
        return "approval_fingerprint_mismatch"
    if (
        approval.owner_user_id != intent.owner_user_id
        or approval.approver_user_id != intent.owner_user_id
        or approval.intent_id != intent.id
        or approval.incident_id != intent.incident_id
    ):
        return "approval_identity_mismatch"
    return None


def _denied_creation(reason: str) -> RecoveryPolicyDecision:
    return RecoveryPolicyDecision(False, "denied", reason, False, False)


def _manual(reason: str, intent: RecoveryIntentRecord) -> RecoveryPolicyDecision:
    return RecoveryPolicyDecision(
        False,
        "manual_intervention",
        reason,
        intent.automatic_eligible,
        intent.approval_required,
    )

