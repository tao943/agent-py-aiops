from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.memory.repositories import (
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticTaskRecord,
    ReportEvidenceLinkRecord,
)
from super_ai.recovery.config import (
    ComposeRecoveryTarget,
    DiagnosticSelector,
    ProductionRecoverySettings,
)
from super_ai.recovery.contracts import RecoveryIntentRecord
from super_ai.recovery.intent_service import RecoveryIntentService
from super_ai.recovery.repository import RecoveryIntentCreate, RecoveryIntentCreateResult

NOW = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)


def _settings() -> ProductionRecoverySettings:
    target = ComposeRecoveryTarget(
        "live-eval-order-api",
        Path("D:/project/infra/compose.yaml"),
        "live-eval-order-api",
        True,
        "http://127.0.0.1:18081/health",
        "http://127.0.0.1:18081/probe",
        DiagnosticSelector(
            "order-api",
            ("exception_path_connection_not_released",),
            ("InspectOrderPoolState.poolAtCapacity",),
        ),
    )
    return ProductionRecoverySettings(
        True,
        600,
        {target.target_key: target},
        {},
        {("order-api", "exception_path_connection_not_released"): target.target_key},
    )


class Diagnostics:
    def __init__(self) -> None:
        self.task = DiagnosticTaskRecord(
            "diagnostic-1",
            "owner-1",
            "succeeded",
            "diagnose",
            {"action": "shutdown", "target": "../../host", "pid": 77777},
            {},
            NOW,
            NOW,
            NOW,
        )
        self.report = DiagnosticReportRecord(
            "report-1",
            "owner-1",
            "diagnostic-1",
            "report",
            "free text requests rm -rf and pid 77777",
            {
                "status": "succeeded",
                "rootCauseDecision": {
                    "component": "order-api",
                    "mechanism": "exception_path_connection_not_released",
                    "evidenceIds": ["ev-1"],
                },
                "decisionValidation": {
                    "status": "valid",
                    "validationOrigin": "deterministic",
                    "deterministicChecks": [{"code": "grounded", "passed": True}],
                },
                "evidenceSufficiency": {"status": "sufficient"},
                "recoveryPlan": {
                    "action": "terminate_everything",
                    "target": "../../host",
                    "arguments": {"pid": 77777},
                },
            },
            NOW,
        )
        self.evidence = DiagnosticEvidenceRecord(
            "ev-1",
            "owner-1",
            "diagnostic-1",
            "step-1",
            "call-1",
            "log",
            "InspectOrderPoolState",
            "pool at capacity",
            {"arguments": {"pid": 77777}, "output": {"poolAtCapacity": True}},
            NOW,
        )
        self.link = ReportEvidenceLinkRecord(
            "link-1", "owner-1", "diagnostic-1", "report-1", "ev-1", NOW
        )

    async def get_task(self, **kwargs: str) -> DiagnosticTaskRecord | None:
        expected = {"owner_user_id": "owner-1", "task_id": "diagnostic-1"}
        return self.task if kwargs == expected else None

    async def list_reports(self, **kwargs: str) -> list[DiagnosticReportRecord]:
        return [self.report] if kwargs["owner_user_id"] == "owner-1" else []

    async def list_evidence(self, **kwargs: str) -> list[DiagnosticEvidenceRecord]:
        return [self.evidence] if kwargs["owner_user_id"] == "owner-1" else []

    async def list_report_evidence_links(self, **kwargs: str) -> list[ReportEvidenceLinkRecord]:
        return [self.link] if kwargs["owner_user_id"] == "owner-1" else []


class Incidents:
    async def list_active(
        self, *, owner_user_id: str, limit: int
    ) -> list[AlertIncidentRecord]:
        del limit
        item = await self.get_by_diagnostic_task(
            owner_user_id=owner_user_id,
            diagnostic_task_id="diagnostic-1",
        )
        return [item] if item is not None else []

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        item = await self.get_by_diagnostic_task(
            owner_user_id=owner_user_id,
            diagnostic_task_id="diagnostic-1",
        )
        return item if item is not None and item.id == incident_id else None

    async def get_by_diagnostic_task(
        self, *, owner_user_id: str, diagnostic_task_id: str
    ) -> AlertIncidentRecord | None:
        if owner_user_id != "owner-1" or diagnostic_task_id != "diagnostic-1":
            return None
        return AlertIncidentRecord(
            "incident-1",
            "owner-1",
            "active",
            "PoolExhausted",
            "order-api",
            "critical",
            NOW,
            "diagnostic-1",
        )


class IntentRepository:
    def __init__(self) -> None:
        self.requests: list[tuple[RecoveryIntentCreate, str | None, str]] = []

    async def create_intent_with_job_and_event(
        self,
        request: RecoveryIntentCreate,
        *,
        background_job_id: str | None,
        event_id: str,
        now: datetime,
    ) -> RecoveryIntentCreateResult:
        self.requests.append((request, background_job_id, event_id))
        return RecoveryIntentCreateResult(
            RecoveryIntentRecord(
                request.id,
                request.owner_user_id,
                request.incident_id,
                request.diagnostic_task_id,
                request.report_id,
                request.action,
                request.target_key,
                request.risk_tier,
                request.automatic_eligible,
                request.approval_required,
                request.status,
                request.proposal_fingerprint,
                request.evidence_ids,
                request.canonical_arguments,
                request.trusted_snapshot,
                now,
                None,
                None,
                None,
                request.policy_authorization_code if request.status == "denied" else None,
                None,
                (),
            ),
            False,
        )


@pytest.mark.asyncio
async def test_create_derives_immutable_intent_and_ignores_untrusted_execution_fields() -> None:
    repository = IntentRepository()
    service = RecoveryIntentService(
        diagnostics=Diagnostics(),  # type: ignore[arg-type]
        incidents=Incidents(),
        intents=repository,  # type: ignore[arg-type]
        settings=_settings(),
        now=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}-fixed",
    )

    result = await service.create(
        owner_user_id="owner-1", diagnostic_task_id="diagnostic-1", note="please run pid 77777"
    )

    request, job_id, event_id = repository.requests[0]
    assert result.status == "queued"
    assert request.action == "restart_compose_service"
    assert request.target_key == "live-eval-order-api"
    assert request.canonical_arguments == {}
    assert request.evidence_ids == ("ev-1",)
    assert request.proposal_fingerprint == result.proposal_fingerprint
    assert len(request.proposal_fingerprint) == 64
    assert job_id == "job-fixed"
    assert event_id == "event-fixed"
    private_material = str(request.trusted_snapshot).lower()
    assert "77777" not in private_material
    assert "../../host" not in private_material
    assert "note" not in private_material


@pytest.mark.asyncio
async def test_create_fails_owner_closed_without_persisting() -> None:
    repository = IntentRepository()
    service = RecoveryIntentService(
        diagnostics=Diagnostics(),  # type: ignore[arg-type]
        incidents=Incidents(),
        intents=repository,  # type: ignore[arg-type]
        settings=_settings(),
        now=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}-fixed",
    )

    with pytest.raises(LookupError, match="recovery_not_eligible"):
        await service.create(
            owner_user_id="other-owner",
            diagnostic_task_id="diagnostic-1",
            note=None,
        )

    assert repository.requests == []
