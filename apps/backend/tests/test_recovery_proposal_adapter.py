from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.memory.repositories import DiagnosticEvidenceRecord
from super_ai.recovery.config import (
    ComposeRecoveryTarget,
    DiagnosticSelector,
    PostgresLockResource,
    PostgresRecoveryTarget,
    ProductionRecoverySettings,
)
from super_ai.recovery.proposal_adapter import (
    RecoveryProposalAdapter,
    ValidatedDiagnosticDecision,
)

NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _settings() -> ProductionRecoverySettings:
    compose = ComposeRecoveryTarget(
        target_key="live-eval-order-api",
        compose_file=Path("D:/project/infra/compose.yaml"),
        service="live-eval-order-api",
        automatic_recovery_enabled=True,
        health_url="http://127.0.0.1:18081/health",
        business_probe_url="http://127.0.0.1:18081/live-eval/probe",
        diagnostic_selector=DiagnosticSelector(
            component="order-api",
            mechanisms=("exception_path_connection_not_released",),
            required_evidence_facts=(
                "InspectOrderPoolState.poolAtCapacity",
                "VerifyOrderDatabaseReachability.businessProbeTimedOut",
            ),
        ),
    )
    postgres = PostgresRecoveryTarget(
        target_key="agent-py-postgres",
        database_config_key="backend",
        database_identity="agent_py_test",
        diagnostic_selector=DiagnosticSelector(
            component="postgresql",
            mechanisms=("row_lock_blocking",),
            required_evidence_facts=(
                "InspectPostgresLockGraph.blockerEdgeConfirmed",
                "InspectPostgresLockGraph.blockerRole",
                "InspectPostgresLockGraph.lockedResource",
            ),
        ),
        lock_resource_mappings={
            "order_row": PostgresLockResource("order_row", "live_eval", "orders")
        },
    )
    return ProductionRecoverySettings(
        enabled=True,
        approval_ttl_seconds=600,
        compose_targets={compose.target_key: compose},
        postgres_targets={postgres.target_key: postgres},
        diagnostic_selectors={
            ("order-api", "exception_path_connection_not_released"): compose.target_key,
            ("postgresql", "row_lock_blocking"): postgres.target_key,
        },
    )


def _evidence(evidence_id: str, source: str, output: dict[str, object]) -> DiagnosticEvidenceRecord:
    return DiagnosticEvidenceRecord(
        id=evidence_id,
        owner_user_id="owner-1",
        task_id="diagnostic-1",
        step_id="step-1",
        tool_call_id="tool-call-1",
        kind="log",
        source=source,
        summary="bounded public evidence",
        payload={"arguments": {"pid": 99999}, "output": output},
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("decision", "evidence", "expected_action", "expected_target"),
    [
        (
            ValidatedDiagnosticDecision(
                component="order-api",
                mechanism="exception_path_connection_not_released",
                evidence_ids=("ev-pool", "ev-health"),
                validator_origin="deterministic",
                evidence_sufficient=True,
                deterministic_checks_passed=True,
            ),
            (
                _evidence("ev-pool", "InspectOrderPoolState", {"poolAtCapacity": True}),
                _evidence(
                    "ev-health",
                    "VerifyOrderDatabaseReachability",
                    {"businessProbeTimedOut": True},
                ),
            ),
            "restart_compose_service",
            "live-eval-order-api",
        ),
        (
            ValidatedDiagnosticDecision(
                component="postgresql",
                mechanism="row_lock_blocking",
                evidence_ids=("ev-lock",),
                validator_origin="deterministic_grounded_fallback",
                evidence_sufficient=True,
                deterministic_checks_passed=True,
            ),
            (
                _evidence(
                    "ev-lock",
                    "InspectPostgresLockGraph",
                    {
                        "blockerEdgeConfirmed": True,
                        "blockerRole": "transaction",
                        "lockedResource": "order_row",
                        "pid": 43210,
                    },
                ),
            ),
            "terminate_postgres_blocker",
            "agent-py-postgres",
        ),
    ],
)
def test_resolves_live_diagnostics_from_validated_facts_only(
    decision: ValidatedDiagnosticDecision,
    evidence: tuple[DiagnosticEvidenceRecord, ...],
    expected_action: str,
    expected_target: str,
) -> None:
    proposal = RecoveryProposalAdapter().resolve(decision, evidence, _settings())

    assert proposal is not None
    assert proposal.action == expected_action
    assert proposal.target_key == expected_target
    assert proposal.canonical_arguments == {}
    assert "pid" not in str(proposal.trusted_snapshot).lower()
    assert "scenario" not in str(proposal.trusted_snapshot).lower()
    if expected_action == "terminate_postgres_blocker":
        fingerprint = proposal.trusted_snapshot["lockRelationshipFingerprint"]
        assert isinstance(fingerprint, str)
        assert len(fingerprint) == 64
        assert "orders" not in str(proposal.trusted_snapshot)


@pytest.mark.parametrize(
    "decision_overrides,evidence",
    [
        ({"mechanism": "unsupported"}, ()),
        ({"evidence_sufficient": False}, ()),
        ({"deterministic_checks_passed": False}, ()),
        ({"validator_origin": "none"}, ()),
        (
            {},
            (_evidence("ev-pool", "InspectOrderPoolState", {"poolAtCapacity": True}),),
        ),
    ],
)
def test_rejects_unvalidated_unsupported_or_incomplete_proposals(
    decision_overrides: dict[str, object],
    evidence: tuple[DiagnosticEvidenceRecord, ...],
) -> None:
    values: dict[str, object] = {
        "component": "order-api",
        "mechanism": "exception_path_connection_not_released",
        "evidence_ids": ("ev-pool", "ev-health"),
        "validator_origin": "deterministic",
        "evidence_sufficient": True,
        "deterministic_checks_passed": True,
    }
    values.update(decision_overrides)
    decision = ValidatedDiagnosticDecision(**values)  # type: ignore[arg-type]

    assert RecoveryProposalAdapter().resolve(decision, evidence, _settings()) is None
