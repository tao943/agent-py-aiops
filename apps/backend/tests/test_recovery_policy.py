from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from super_ai.recovery.config import (
    ComposeRecoveryTarget,
    DiagnosticSelector,
    PostgresLockResource,
    PostgresRecoveryTarget,
    ProductionRecoverySettings,
)
from super_ai.recovery.contracts import RecoveryIntentRecord
from super_ai.recovery.policy import (
    RecoveryCreationFacts,
    RecoveryExecutionFacts,
    RecoveryPolicy,
)
from super_ai.recovery.proposal_adapter import RecoveryProposal
from super_ai.recovery.repository import RecoveryApprovalRecord

NOW = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)


def _settings(*, enabled: bool = True, automatic: bool = True) -> ProductionRecoverySettings:
    selector = DiagnosticSelector("order-api", ("pool_leak",), ("Tool.fact",))
    compose = ComposeRecoveryTarget(
        "compose-target",
        Path("D:/project/compose.yaml"),
        "order-api",
        automatic,
        "http://127.0.0.1:18081/health",
        "http://127.0.0.1:18081/probe",
        selector,
    )
    postgres = PostgresRecoveryTarget(
        "postgres-target",
        "backend",
        "agent_py_test",
        DiagnosticSelector("postgresql", ("row_lock_blocking",), ("Lock.edge",)),
        {"order_row": PostgresLockResource("order_row", "live_eval", "orders")},
    )
    return ProductionRecoverySettings(
        enabled,
        600,
        {compose.target_key: compose},
        {postgres.target_key: postgres},
        {
            ("order-api", "pool_leak"): compose.target_key,
            ("postgresql", "row_lock_blocking"): postgres.target_key,
        },
    )


def _proposal(*, postgres: bool = False) -> RecoveryProposal:
    return RecoveryProposal(
        action="terminate_postgres_blocker" if postgres else "restart_compose_service",
        target_key="postgres-target" if postgres else "compose-target",
        canonical_arguments={},
        evidence_ids=("ev-1",),
        validator_origin="deterministic",
        trusted_snapshot={
            "component": "postgresql" if postgres else "order-api",
            "mechanism": "row_lock_blocking" if postgres else "pool_leak",
            "evidenceFactKeys": ["Lock.edge" if postgres else "Tool.fact"],
        },
    )


def _creation(**overrides: object) -> RecoveryCreationFacts:
    values: dict[str, object] = {
        "diagnostic_succeeded": True,
        "report_available": True,
        "incident_active": True,
        "evidence_sufficient": True,
        "deterministic_validation_passed": True,
        "proposal": _proposal(),
    }
    values.update(overrides)
    return RecoveryCreationFacts(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("facts", "settings", "reason"),
    [
        (_creation(diagnostic_succeeded=False), _settings(), "diagnostic_incomplete"),
        (_creation(report_available=False), _settings(), "report_unavailable"),
        (_creation(incident_active=False), _settings(), "incident_not_active"),
        (_creation(evidence_sufficient=False), _settings(), "evidence_insufficient"),
        (_creation(deterministic_validation_passed=False), _settings(), "validation_failed"),
        (_creation(proposal=None), _settings(), "proposal_not_grounded"),
        (_creation(), _settings(enabled=False), "recovery_disabled"),
        (_creation(), _settings(automatic=False), "automatic_recovery_disabled"),
        (
            _creation(proposal=replace(_proposal(), target_key="missing")),
            _settings(),
            "target_not_allowlisted",
        ),
        (
            _creation(
                proposal=replace(_proposal(), action="terminate_postgres_blocker")
            ),
            _settings(),
            "action_target_mismatch",
        ),
    ],
)
def test_creation_policy_fails_closed(
    facts: RecoveryCreationFacts,
    settings: ProductionRecoverySettings,
    reason: str,
) -> None:
    result = RecoveryPolicy().evaluate_creation(facts, settings)

    assert result.allowed is False
    assert result.next_status == "denied"
    assert result.safe_reason_code == reason


def test_creation_policy_queues_only_explicit_low_risk_compose() -> None:
    result = RecoveryPolicy().evaluate_creation(_creation(), _settings())

    assert result.allowed is True
    assert result.next_status == "queued"
    assert result.automatic_eligible is True
    assert result.approval_required is False


def test_creation_policy_requires_approval_for_postgres() -> None:
    result = RecoveryPolicy().evaluate_creation(
        _creation(proposal=_proposal(postgres=True)), _settings()
    )

    assert result.allowed is True
    assert result.next_status == "awaiting_approval"
    assert result.automatic_eligible is False
    assert result.approval_required is True


def _intent(*, postgres: bool = False) -> RecoveryIntentRecord:
    proposal = _proposal(postgres=postgres)
    return RecoveryIntentRecord(
        id="intent-1",
        owner_user_id="owner-1",
        incident_id="incident-1",
        diagnostic_task_id="diagnostic-1",
        report_id="report-1",
        action=proposal.action,
        target_key=proposal.target_key,
        risk_tier="high" if postgres else "low",
        automatic_eligible=not postgres,
        approval_required=postgres,
        status="queued",
        proposal_fingerprint="a" * 64,
        evidence_ids=("ev-1",),
        canonical_arguments={},
        trusted_snapshot=proposal.trusted_snapshot,
        created_at=NOW,
        approval_expires_at=None,
        started_at=None,
        completed_at=None,
        safe_reason_code=None,
        execution_summary=None,
        verification=(),
    )


def _execution(**overrides: object) -> RecoveryExecutionFacts:
    values: dict[str, object] = {
        "incident_active": True,
        "proposal_fingerprint": "a" * 64,
    }
    values.update(overrides)
    return RecoveryExecutionFacts(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("facts", "settings", "reason"),
    [
        (_execution(incident_active=False), _settings(), "incident_not_active"),
        (_execution(), _settings(enabled=False), "recovery_disabled"),
        (_execution(proposal_fingerprint="b" * 64), _settings(), "proposal_changed"),
        (_execution(), _settings(automatic=False), "automatic_recovery_disabled"),
    ],
)
def test_execution_policy_denies_compose_drift_before_claim(
    facts: RecoveryExecutionFacts,
    settings: ProductionRecoverySettings,
    reason: str,
) -> None:
    result = RecoveryPolicy().evaluate_execution(
        facts, _intent(), settings, approval=None, now=NOW
    )

    assert result.allowed is False
    assert result.next_status == "manual_intervention"
    assert result.safe_reason_code == reason


@pytest.mark.parametrize("approval", [None, "expired", "changed"])
def test_execution_policy_requires_fresh_fingerprint_bound_postgres_approval(
    approval: str | None,
) -> None:
    record = None
    if approval is not None:
        record = RecoveryApprovalRecord(
            id="approval-1",
            intent_id="intent-1",
            owner_user_id="owner-1",
            approver_user_id="owner-1",
            incident_id="incident-1",
            proposal_fingerprint="b" * 64 if approval == "changed" else "a" * 64,
            decision="approved",
            created_at=NOW - timedelta(minutes=1),
            expires_at=(
                NOW - timedelta(seconds=1)
                if approval == "expired"
                else NOW + timedelta(minutes=9)
            ),
        )

    result = RecoveryPolicy().evaluate_execution(
        _execution(), _intent(postgres=True), _settings(), approval=record, now=NOW
    )

    assert result.allowed is False
    assert result.next_status == "manual_intervention"
    assert result.safe_reason_code in {
        "approval_required",
        "approval_expired",
        "approval_fingerprint_mismatch",
    }


def test_execution_policy_allows_fresh_approved_postgres_intent() -> None:
    approval = RecoveryApprovalRecord(
        id="approval-1",
        intent_id="intent-1",
        owner_user_id="owner-1",
        approver_user_id="owner-1",
        incident_id="incident-1",
        proposal_fingerprint="a" * 64,
        decision="approved",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    result = RecoveryPolicy().evaluate_execution(
        _execution(), _intent(postgres=True), _settings(), approval=approval, now=NOW
    )

    assert result.allowed is True
    assert result.next_status == "revalidating"
