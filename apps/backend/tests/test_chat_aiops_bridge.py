from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.alert_ingestion.repositories import AlertIncidentRecord, IncidentNotActive
from super_ai.alert_ingestion.sqlalchemy import SQLAlchemyAlertIngestionRepository
from super_ai.chat.aiops_bridge import (
    AiopsBridgeService,
    BridgeResourceNotFound,
    RecoveryApprovalNotAllowed,
    RecoveryApprovalRequest,
    build_aiops_bridge_tools,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import AlertIncidentModel, DiagnosticTaskModel, UserModel
from super_ai.memory.repositories import (
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticTaskRecord,
)
from super_ai.recovery.contracts import RecoveryIntentRecord
from super_ai.recovery.repository import RecoveryIntentCreateResult

NOW = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)


class FakeIncidentQueries:
    def __init__(self) -> None:
        self.items = {
            "incident_a": AlertIncidentRecord(
                id="incident_a",
                owner_user_id="owner_a",
                status="active",
                alert_name="HighLatency",
                service="order-service",
                severity="critical",
                last_seen_at=NOW,
                diagnostic_task_id="diagnostic_a",
            ),
            "incident_b": AlertIncidentRecord(
                id="incident_b",
                owner_user_id="owner_b",
                status="active",
                alert_name="PoolExhausted",
                service="payment-service",
                severity="warning",
                last_seen_at=NOW,
                diagnostic_task_id=None,
            ),
        }

    async def list_active(
        self, *, owner_user_id: str, limit: int
    ) -> list[AlertIncidentRecord]:
        return [
            item for item in self.items.values() if item.owner_user_id == owner_user_id
        ][:limit]

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        item = self.items.get(incident_id)
        return item if item is not None and item.owner_user_id == owner_user_id else None

    async def get_by_diagnostic_task(
        self, *, owner_user_id: str, diagnostic_task_id: str
    ) -> AlertIncidentRecord | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.owner_user_id == owner_user_id
                and item.diagnostic_task_id == diagnostic_task_id
            ),
            None,
        )


class FakeDiagnostics:
    async def get_task(
        self, *, owner_user_id: str, task_id: str
    ) -> DiagnosticTaskRecord | None:
        if owner_user_id != "owner_a" or task_id != "diagnostic_a":
            return None
        return DiagnosticTaskRecord(
            id=task_id,
            owner_user_id=owner_user_id,
            status="succeeded",
            query="Investigate latency",
            input_payload={},
            result_payload={},
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )

    async def list_reports(
        self, *, owner_user_id: str, task_id: str
    ) -> list[DiagnosticReportRecord]:
        if owner_user_id != "owner_a" or task_id != "diagnostic_a":
            return []
        return [
            DiagnosticReportRecord(
                id="report_a",
                owner_user_id=owner_user_id,
                task_id=task_id,
                title="Latency diagnosis",
                content="Evidence-backed public report.",
                payload={
                    "rootCauseDecision": {"primaryCause": "database_lock"},
                    "recoveryPlan": {"mode": "manual_review"},
                    "recoveryPolicy": {
                        "executionPermitted": False,
                        "humanApprovalRequired": True,
                    },
                    "decisionValidation": {
                        "validationOrigin": "deterministic_grounded_fallback"
                    },
                    "evidenceIds": ["evidence_a"],
                    "checkpoint": {"private": True},
                    "reasoning": "must not escape",
                },
                created_at=NOW,
            )
        ]

    async def list_evidence(
        self, *, owner_user_id: str, task_id: str
    ) -> list[DiagnosticEvidenceRecord]:
        if owner_user_id != "owner_a" or task_id != "diagnostic_a":
            return []
        return [
            DiagnosticEvidenceRecord(
                id="evidence_a",
                owner_user_id=owner_user_id,
                task_id=task_id,
                step_id="step_a",
                tool_call_id="tool_a",
                kind="log",
                source="cls",
                summary="Lock wait observed.",
                payload={"evaluation": "supports", "reasoning": "private"},
                created_at=NOW,
            )
        ]


class FakeScheduler:
    def __init__(self) -> None:
        self.calls = 0

    async def schedule_for_incident(
        self, *, owner_user_id: str, incident_id: str, note: str | None
    ) -> object:
        from super_ai.chat.aiops_bridge import DiagnosticScheduleResult

        del owner_user_id, incident_id, note
        self.calls += 1
        return DiagnosticScheduleResult(
            diagnostic_task_id="diagnostic_a",
            background_job_id="job_a",
            reused=self.calls > 1,
        )


class FakeApprovalRequests:
    def __init__(self) -> None:
        self.created = 0

    async def create_or_get(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
        proposal_fingerprint: str,
        request_reason: str,
        chat_run_id: str | None,
    ) -> RecoveryApprovalRequest:
        del proposal_fingerprint, request_reason, chat_run_id
        self.created += 1
        return RecoveryApprovalRequest(
            id="approval_a",
            owner_user_id=owner_user_id,
            diagnostic_task_id=diagnostic_task_id,
            status="pending",
            execution_permitted=False,
            reused=self.created > 1,
        )

def _bridge() -> AiopsBridgeService:
    return AiopsBridgeService(
        incidents=FakeIncidentQueries(),
        diagnostics=FakeDiagnostics(),  # type: ignore[arg-type]
        scheduler=FakeScheduler(),  # type: ignore[arg-type]
        approval_requests=FakeApprovalRequests(),
    )


class FakeFormalRecoveryIntents:
    async def create_result(self, **_: object) -> RecoveryIntentCreateResult:
        return RecoveryIntentCreateResult(
            RecoveryIntentRecord(
                "intent_a", "owner_a", "incident_a", "diagnostic_a", "report_a",
                "terminate_postgres_blocker", "postgres", "high", False, True,
                "awaiting_approval", "a" * 64, ("evidence_a",), {}, {}, NOW,
                None, None, None, None, None, (),
            ),
            False,
        )


@pytest.mark.asyncio
async def test_list_active_incidents_returns_only_owned_safe_summaries() -> None:
    items = await _bridge().list_active_incidents(owner_user_id="owner_a", limit=10)

    assert [item.id for item in items] == ["incident_a"]
    serialized = json.dumps([item.to_payload() for item in items])
    assert "normalized_payload" not in serialized
    assert "group_key" not in serialized


@pytest.mark.asyncio
async def test_report_and_evidence_exclude_checkpoint_and_reasoning() -> None:
    bridge = _bridge()

    report = await bridge.get_diagnostic_report(
        owner_user_id="owner_a", task_id="diagnostic_a"
    )
    evidence = await bridge.get_diagnostic_evidence(
        owner_user_id="owner_a", task_id="diagnostic_a"
    )

    serialized = json.dumps(
        {"report": report.to_payload(), "evidence": [item.to_payload() for item in evidence]}
    ).lower()
    assert report.execution_permitted is False
    assert report.recovery_mode == "manual_review"
    assert "reasoning" not in serialized
    assert "checkpoint" not in serialized
    assert "must not escape" not in serialized


def test_structured_tool_schema_cannot_accept_owner_identity() -> None:
    tools = build_aiops_bridge_tools(owner_user_id="owner_a", service=_bridge())
    tool = tools["get_incident"]
    assert isinstance(tool.args_schema, type)
    args_schema = cast(type[BaseModel], tool.args_schema)

    assert "owner_user_id" not in json.dumps(args_schema.model_json_schema())
    with pytest.raises(ValidationError):
        args_schema.model_validate(
            {"incident_id": "incident_b", "owner_user_id": "owner_b"}
        )


@pytest.mark.asyncio
async def test_bound_tool_cannot_read_other_owner_incident() -> None:
    tool = build_aiops_bridge_tools(owner_user_id="owner_a", service=_bridge())[
        "get_incident"
    ]

    with pytest.raises(BridgeResourceNotFound):
        await tool.ainvoke({"incident_id": "incident_b"})


@pytest.mark.asyncio
async def test_start_diagnostic_returns_bounded_scheduler_result() -> None:
    bridge = _bridge()

    first = await bridge.start_incident_diagnostic(
        owner_user_id="owner_a", incident_id="incident_a", note="check current state"
    )
    second = await bridge.start_incident_diagnostic(
        owner_user_id="owner_a", incident_id="incident_a", note=None
    )

    assert first.diagnostic_task_id == second.diagnostic_task_id == "diagnostic_a"
    assert first.background_job_id == second.background_job_id == "job_a"
    assert first.reused is False
    assert second.reused is True


@pytest.mark.asyncio
async def test_recovery_request_only_creates_pending_non_executable_approval() -> None:
    bridge = _bridge()

    result = await bridge.create_recovery_approval_request(
        owner_user_id="owner_a",
        task_id="diagnostic_a",
        reason="Please have an operator review the proposal.",
        chat_run_id="run_a",
    )

    assert result.status == "pending"
    assert result.execution_permitted is False
    assert "approve" not in result.to_payload()
    assert "execute" not in result.to_payload()
    assert result.to_payload()["legacy"] is True


@pytest.mark.asyncio
async def test_confirmed_chat_action_creates_formal_intent_without_approval() -> None:
    bridge = AiopsBridgeService(
        incidents=FakeIncidentQueries(),
        diagnostics=FakeDiagnostics(),  # type: ignore[arg-type]
        recovery_intents=FakeFormalRecoveryIntents(),  # type: ignore[arg-type]
    )

    result = await bridge.create_recovery_approval_request(
        owner_user_id="owner_a",
        task_id="diagnostic_a",
        reason="Create an intent for operator review.",
        chat_run_id="run_a",
    )

    assert result.id == "intent_a"
    assert result.status == "awaiting_approval"
    assert result.execution_permitted is False
    assert result.legacy is False


@pytest.mark.asyncio
async def test_recovery_request_without_proposal_is_rejected() -> None:
    class NoActionDiagnostics(FakeDiagnostics):
        async def list_reports(
            self, *, owner_user_id: str, task_id: str
        ) -> list[DiagnosticReportRecord]:
            reports = await super().list_reports(
                owner_user_id=owner_user_id, task_id=task_id
            )
            report = reports[0]
            return [
                DiagnosticReportRecord(
                    id=report.id,
                    owner_user_id=report.owner_user_id,
                    task_id=report.task_id,
                    title=report.title,
                    content=report.content,
                    payload={
                        **report.payload,
                        "recoveryPlan": {"mode": "no_action"},
                        "recoveryPolicy": {"executionPermitted": False},
                    },
                    created_at=report.created_at,
                )
            ]

    bridge = AiopsBridgeService(
        incidents=FakeIncidentQueries(),
        diagnostics=NoActionDiagnostics(),  # type: ignore[arg-type]
        scheduler=FakeScheduler(),  # type: ignore[arg-type]
        approval_requests=FakeApprovalRequests(),
    )

    with pytest.raises(RecoveryApprovalNotAllowed):
        await bridge.create_recovery_approval_request(
            owner_user_id="owner_a",
            task_id="diagnostic_a",
            reason="review",
            chat_run_id=None,
        )


@pytest.mark.asyncio
async def test_postgresql_incident_query_is_owner_scoped(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_incidents(session_factory)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        items = await repository.list_active(owner_user_id="owner_a", limit=50)
        other = await repository.get_owned(
            owner_user_id="owner_a", incident_id="incident_b"
        )
    finally:
        await engine.dispose()

    assert [item.id for item in items] == ["incident_a"]
    assert other is None


@pytest.mark.asyncio
async def test_postgresql_scheduler_reuses_one_task_and_job(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_incidents(session_factory)
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        first = await repository.schedule_for_incident(
            owner_user_id="owner_a", incident_id="incident_a", note=None
        )
        second = await repository.schedule_for_incident(
            owner_user_id="owner_a", incident_id="incident_a", note="retry"
        )
        async with session_factory() as session:
            task = (
                await session.scalars(
                    select(DiagnosticTaskModel).where(
                        DiagnosticTaskModel.id == first.diagnostic_task_id
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert first.diagnostic_task_id == second.diagnostic_task_id
    assert first.background_job_id == second.background_job_id
    assert first.reused is False
    assert second.reused is True
    assert "triggerSource" not in task.input_payload


@pytest.mark.asyncio
async def test_postgresql_scheduler_rejects_resolved_incident(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    await _seed_incidents(session_factory, status="resolved")
    repository = SQLAlchemyAlertIngestionRepository(session_factory)
    try:
        with pytest.raises(IncidentNotActive):
            await repository.schedule_for_incident(
                owner_user_id="owner_a", incident_id="incident_a", note=None
            )
    finally:
        await engine.dispose()


async def _seed_incidents(
    session_factory: async_sessionmaker[AsyncSession], *, status: str = "active"
) -> None:
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                UserModel(
                    id=owner,
                    email=f"{owner}@example.test",
                    display_name=owner,
                    password_hash="unused",
                )
                for owner in ("owner_a", "owner_b")
            ]
        )
        await session.flush()
        session.add_all(
            [
                AlertIncidentModel(
                    id=f"incident_{suffix}",
                    owner_user_id=f"owner_{suffix}",
                    source_id="test",
                    group_key_hash=suffix * 64,
                    status=status,
                    alert_name="HighLatency",
                    service="order-service",
                    severity="critical",
                    starts_at=NOW,
                    last_seen_at=NOW,
                    resolved_at=NOW if status == "resolved" else None,
                    delivery_count=1,
                    diagnostic_task_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
                for suffix in ("a", "b")
            ]
        )
