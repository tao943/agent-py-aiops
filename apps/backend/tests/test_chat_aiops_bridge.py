from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.alert_ingestion.sqlalchemy import SQLAlchemyAlertIngestionRepository
from super_ai.chat.aiops_bridge import (
    AiopsBridgeService,
    BridgeResourceNotFound,
    build_aiops_bridge_tools,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import AlertIncidentModel, UserModel
from super_ai.memory.repositories import (
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticTaskRecord,
)

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


def _bridge() -> AiopsBridgeService:
    return AiopsBridgeService(
        incidents=FakeIncidentQueries(),
        diagnostics=FakeDiagnostics(),  # type: ignore[arg-type]
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

    assert "owner_user_id" not in json.dumps(tool.args_schema.model_json_schema())
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate(
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


async def _seed_incidents(session_factory: async_sessionmaker[AsyncSession]) -> None:
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
                    status="active",
                    alert_name="HighLatency",
                    service="order-service",
                    severity="critical",
                    starts_at=NOW,
                    last_seen_at=NOW,
                    resolved_at=None,
                    delivery_count=1,
                    diagnostic_task_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
                for suffix in ("a", "b")
            ]
        )
