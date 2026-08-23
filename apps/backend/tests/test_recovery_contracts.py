from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from super_ai.recovery.contracts import (
    RecoveryCheck,
    RecoveryIntentRecord,
    canonical_json,
    proposal_fingerprint,
)


def _intent() -> RecoveryIntentRecord:
    return RecoveryIntentRecord(
        id="recovery_1",
        owner_user_id="owner_1",
        incident_id="incident_1",
        diagnostic_task_id="diagnostic_1",
        report_id="report_1",
        action="restart_compose_service",
        target_key="live-eval-order-api",
        risk_tier="low",
        automatic_eligible=True,
        approval_required=False,
        status="queued",
        proposal_fingerprint="a" * 64,
        evidence_ids=("evidence_2", "evidence_1"),
        canonical_arguments={"service": "live-eval-order-api"},
        trusted_snapshot={"privateContainerIdentity": "container-secret"},
        created_at=datetime(2026, 8, 23, 8, tzinfo=timezone.utc),
        approval_expires_at=None,
        started_at=None,
        completed_at=None,
        safe_reason_code=None,
        execution_summary=None,
        verification=(
            RecoveryCheck(
                key="service_health",
                status="pending",
                safe_summary="Waiting for the configured health probe.",
                checked_at=None,
            ),
        ),
    )


def test_recovery_intent_is_frozen_and_public_payload_omits_private_facts() -> None:
    intent = _intent()

    with pytest.raises(FrozenInstanceError):
        intent.status = "executing"  # type: ignore[misc]

    payload = intent.public_payload()
    serialized = canonical_json(payload).lower()
    assert payload["targetKey"] == "live-eval-order-api"
    assert payload["verification"] == [
        {
            "key": "service_health",
            "status": "pending",
            "safeSummary": "Waiting for the configured health probe.",
            "checkedAt": None,
        }
    ]
    for forbidden in (
        "privatecontaineridentity",
        "canonicalarguments",
        "command",
        "composepath",
        "connectionstring",
        "sql",
        "pid",
        "exception",
    ):
        assert forbidden not in serialized


def test_canonical_json_and_proposal_fingerprint_are_order_stable() -> None:
    assert canonical_json({"b": 2, "a": {"z": 3, "x": 1}}) == (
        '{"a":{"x":1,"z":3},"b":2}'
    )
    first = proposal_fingerprint(
        owner_user_id="owner_1",
        incident_id="incident_1",
        diagnostic_task_id="diagnostic_1",
        report_id="report_1",
        action="restart_compose_service",
        target_key="live-eval-order-api",
        canonical_arguments={"b": 2, "a": 1},
        evidence_ids=("evidence_2", "evidence_1"),
    )
    second = proposal_fingerprint(
        owner_user_id="owner_1",
        incident_id="incident_1",
        diagnostic_task_id="diagnostic_1",
        report_id="report_1",
        action="restart_compose_service",
        target_key="live-eval-order-api",
        canonical_arguments={"a": 1, "b": 2},
        evidence_ids=("evidence_1", "evidence_2"),
    )
    assert first == second
    assert len(first) == 64
