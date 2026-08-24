from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.memory.repositories import (
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticTaskRecord,
    ReportEvidenceLinkRecord,
)
from super_ai.recovery.auto_dispatch import AutoRecoveryIntentDispatcher
from super_ai.recovery.config import (
    DiagnosticSelector,
    PostgresLockResource,
    PostgresRecoveryTarget,
    ProductionRecoverySettings,
)
from super_ai.recovery.contracts import RecoveryIntentRecord
from super_ai.recovery.intent_service import (
    RecoveryIntentNotEligible,
    RecoveryIntentService,
)
from super_ai.recovery.repository import (
    RecoveryIntentCreate,
    RecoveryIntentCreateResult,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _task(
    *, status: str = "succeeded", source: str | None = "alertmanager"
) -> DiagnosticTaskRecord:
    payload: dict[str, object] = {"query": "diagnose"}
    if source is not None:
        payload["triggerSource"] = source
    return DiagnosticTaskRecord(
        "diagnostic-1",
        "owner-1",
        status,
        "diagnose",
        payload,
        {},
        NOW,
        NOW,
        NOW if status in {"succeeded", "failed", "cancelled"} else None,
    )


def _intent(*, status: str = "queued") -> RecoveryIntentRecord:
    return RecoveryIntentRecord(
        "intent-1",
        "owner-1",
        "incident-1",
        "diagnostic-1",
        "report-1",
        "restart_compose_service",
        "order-api",
        "low",
        True,
        False,
        status,  # type: ignore[arg-type]
        "f" * 64,
        ("evidence-1",),
        {},
        {"secret": "must-not-leak"},
        NOW,
        None,
        None,
        None,
        None,
        None,
        (),
    )


class Diagnostics:
    def __init__(self, task: DiagnosticTaskRecord | None) -> None:
        self.task = task

    async def get_task(self, **_: str) -> DiagnosticTaskRecord | None:
        return self.task


class Intents:
    def __init__(
        self,
        result: RecoveryIntentCreateResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or RecoveryIntentCreateResult(_intent(), False)
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def create_result(self, **kwargs: object) -> RecoveryIntentCreateResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "manual", "alertmanager ", "ALERTMANAGER"])
async def test_non_alert_task_is_skipped_without_calling_intent_service(
    source: str | None,
) -> None:
    intents = Intents()
    dispatcher = AutoRecoveryIntentDispatcher(
        diagnostics=Diagnostics(_task(source=source)),  # type: ignore[arg-type]
        recovery_intents=intents,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
    )

    assert result.outcome == "skipped"
    assert result.reason_code == "not_alert_triggered"
    assert intents.calls == []


@pytest.mark.asyncio
async def test_missing_and_incomplete_tasks_have_stable_skip_reasons() -> None:
    intents = Intents()
    missing = AutoRecoveryIntentDispatcher(
        diagnostics=Diagnostics(None),  # type: ignore[arg-type]
        recovery_intents=intents,  # type: ignore[arg-type]
    )
    incomplete = AutoRecoveryIntentDispatcher(
        diagnostics=Diagnostics(_task(status="running")),  # type: ignore[arg-type]
        recovery_intents=intents,  # type: ignore[arg-type]
    )

    unavailable = await missing.dispatch(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
    )
    unfinished = await incomplete.dispatch(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
    )

    assert unavailable.reason_code == "task_unavailable"
    assert unfinished.reason_code == "diagnostic_not_succeeded"
    assert intents.calls == []


@pytest.mark.asyncio
async def test_ineligible_proposal_is_safely_skipped() -> None:
    intents = Intents(error=RecoveryIntentNotEligible("private selector details"))
    dispatcher = AutoRecoveryIntentDispatcher(
        diagnostics=Diagnostics(_task()),  # type: ignore[arg-type]
        recovery_intents=intents,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
    )

    assert result.outcome == "skipped"
    assert result.reason_code == "proposal_not_eligible"
    assert result.public_event()["reasonCode"] == "proposal_not_eligible"
    assert "private selector details" not in str(result.public_event())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reused", "expected_outcome"), [(False, "created"), (True, "reused")]
)
async def test_created_and_reused_intents_return_public_identity(
    reused: bool, expected_outcome: str
) -> None:
    intents = Intents(RecoveryIntentCreateResult(_intent(), reused))
    dispatcher = AutoRecoveryIntentDispatcher(
        diagnostics=Diagnostics(_task()),  # type: ignore[arg-type]
        recovery_intents=intents,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
    )

    assert result.outcome == expected_outcome
    assert result.intent_id == "intent-1"
    assert result.status == "queued"
    assert intents.calls == [
        {
            "owner_user_id": "owner-1",
            "diagnostic_task_id": "diagnostic-1",
            "note": None,
        }
    ]
    assert result.public_event() == {
        "type": "recovery.intent.dispatch",
        "outcome": expected_outcome,
        "reasonCode": None,
        "intentId": "intent-1",
        "status": "queued",
    }
    assert "secret" not in str(result.public_event()).lower()


@pytest.mark.asyncio
async def test_unexpected_service_error_propagates_for_job_retry() -> None:
    dispatcher = AutoRecoveryIntentDispatcher(
        diagnostics=Diagnostics(_task()),  # type: ignore[arg-type]
        recovery_intents=Intents(error=RuntimeError("database unavailable")),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await dispatcher.dispatch(
            owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
        )


class GroundedPostgresDiagnostics(Diagnostics):
    def __init__(self) -> None:
        super().__init__(_task())
        self.report = DiagnosticReportRecord(
            "report-1",
            "owner-1",
            "diagnostic-1",
            "PostgreSQL blocker",
            "A transaction blocks the order row.",
            {
                "status": "succeeded",
                "rootCauseDecision": {
                    "component": "postgresql",
                    "mechanism": "row_lock_blocking",
                    "evidenceIds": ["evidence-1"],
                },
                "decisionValidation": {
                    "status": "valid",
                    "validationOrigin": "deterministic",
                    "deterministicChecks": [{"code": "grounded", "passed": True}],
                },
                "evidenceSufficiency": {"status": "sufficient"},
            },
            NOW,
        )
        self.evidence = DiagnosticEvidenceRecord(
            "evidence-1",
            "owner-1",
            "diagnostic-1",
            "step-1",
            "call-1",
            "metric",
            "InspectPostgresLockGraph",
            "A unique transaction blocker is confirmed.",
            {
                "output": {
                    "blockerEdgeConfirmed": True,
                    "blockerRole": "transaction",
                    "lockedResource": "order_row",
                }
            },
            NOW,
        )

    async def list_reports(self, **_: str) -> list[DiagnosticReportRecord]:
        return [self.report]

    async def list_evidence(self, **_: str) -> list[DiagnosticEvidenceRecord]:
        return [self.evidence]

    async def list_report_evidence_links(
        self, **_: str
    ) -> list[ReportEvidenceLinkRecord]:
        return [
            ReportEvidenceLinkRecord(
                "link-1",
                "owner-1",
                "diagnostic-1",
                "report-1",
                "evidence-1",
                NOW,
            )
        ]


class PostgresIncident:
    async def get_by_diagnostic_task(
        self, *, owner_user_id: str, diagnostic_task_id: str
    ) -> AlertIncidentRecord | None:
        if (owner_user_id, diagnostic_task_id) != ("owner-1", "diagnostic-1"):
            return None
        return AlertIncidentRecord(
            "incident-1",
            "owner-1",
            "active",
            "PostgresLockWait",
            "postgresql",
            "critical",
            NOW,
            "diagnostic-1",
        )


class CapturingIntentRepository:
    def __init__(self) -> None:
        self.background_job_ids: list[str | None] = []

    async def create_intent_with_job_and_event(
        self,
        request: RecoveryIntentCreate,
        *,
        background_job_id: str | None,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentCreateResult:
        del event_id, now
        self.background_job_ids.append(background_job_id)
        return RecoveryIntentCreateResult(
            replace(
                _intent(),
                id=request.id,
                action=request.action,
                target_key=request.target_key,
                risk_tier=request.risk_tier,
                automatic_eligible=request.automatic_eligible,
                approval_required=request.approval_required,
                status=request.status,
                trusted_snapshot=request.trusted_snapshot,
            ),
            False,
        )


@pytest.mark.asyncio
async def test_alert_triggered_postgres_intent_stops_at_owner_approval() -> None:
    diagnostics = GroundedPostgresDiagnostics()
    repository = CapturingIntentRepository()
    target = PostgresRecoveryTarget(
        "agent-py-postgres",
        "backend",
        "agent_py_test",
        DiagnosticSelector(
            "postgresql",
            ("row_lock_blocking",),
            (
                "InspectPostgresLockGraph.blockerEdgeConfirmed",
                "InspectPostgresLockGraph.blockerRole",
                "InspectPostgresLockGraph.lockedResource",
            ),
        ),
        {"order_row": PostgresLockResource("order_row", "public", "orders")},
    )
    service = RecoveryIntentService(
        diagnostics=diagnostics,  # type: ignore[arg-type]
        incidents=PostgresIncident(),  # type: ignore[arg-type]
        intents=repository,  # type: ignore[arg-type]
        settings=ProductionRecoverySettings(
            True,
            600,
            {},
            {target.target_key: target},
            {("postgresql", "row_lock_blocking"): target.target_key},
        ),
        now=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}-fixed",
    )
    dispatcher = AutoRecoveryIntentDispatcher(
        diagnostics=diagnostics,  # type: ignore[arg-type]
        recovery_intents=service,
    )

    result = await dispatcher.dispatch(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1"
    )

    assert result.outcome == "created"
    assert result.status == "awaiting_approval"
    assert repository.background_job_ids == [None]
