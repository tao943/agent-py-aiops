from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, Request

from super_ai.aiops.incident_routes import create_incident_router
from super_ai.alert_ingestion.repositories import (
    AlertIncidentPage,
    AlertIncidentRecord,
    DiagnosticScheduleResult,
)
from super_ai.alert_ingestion.sqlalchemy import SQLAlchemyAlertIngestionRepository
from super_ai.api.responses import ApiErrorException, error_response
from super_ai.auth.repositories import UserRecord
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import (
    AlertIncidentModel,
    DiagnosticReportModel,
    DiagnosticTaskModel,
    ProductionRecoveryIntentModel,
    UserModel,
)

NOW = datetime(2026, 8, 23, 8, 5, tzinfo=timezone.utc)


def _record(
    incident_id: str,
    owner_id: str,
    *,
    updated_at: datetime = NOW,
    recovery_intent_id: str | None = None,
    recovery_status: str | None = None,
) -> AlertIncidentRecord:
    return AlertIncidentRecord(
        id=incident_id,
        owner_user_id=owner_id,
        status="active",
        alert_name="OrderPoolExhausted",
        service="order-service",
        severity="critical",
        last_seen_at=updated_at,
        diagnostic_task_id=f"diagnostic_{incident_id}",
        source_id="local-alertmanager",
        first_seen_at=updated_at,
        updated_at=updated_at,
        delivery_count=2,
        diagnostic_status="succeeded",
        verification_status="pending",
        environment="test",
        agent_mode="multi",
        recovery_mode="automatic" if recovery_intent_id else "not_available",
        approval_status=None,
        recovery_intent_id=recovery_intent_id,
        recovery_execution_status=recovery_status,
    )


class IncidentRepository:
    def __init__(self) -> None:
        self.records = {
            "incident_a": _record(
                "incident_a",
                "owner-a",
                recovery_intent_id="intent_latest",
                recovery_status="executing",
            ),
            "incident_b": _record("incident_b", "owner-b"),
        }

    async def list_owned(
        self,
        *,
        owner_user_id: str,
        status: str,
        limit: int,
        cursor: str | None,
    ) -> AlertIncidentPage:
        del status, cursor
        items = tuple(
            item for item in self.records.values() if item.owner_user_id == owner_user_id
        )[:limit]
        return AlertIncidentPage(items=items, next_cursor=None)

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        record = self.records.get(incident_id)
        return record if record is not None and record.owner_user_id == owner_user_id else None


class Scheduler:
    def __init__(self) -> None:
        self.results: dict[tuple[str, str], DiagnosticScheduleResult] = {}

    async def schedule_for_incident(
        self,
        *,
        owner_user_id: str,
        incident_id: str,
        note: str | None,
    ) -> DiagnosticScheduleResult:
        del note
        key = (owner_user_id, incident_id)
        existing = self.results.get(key)
        if existing is not None:
            return DiagnosticScheduleResult(
                diagnostic_task_id=existing.diagnostic_task_id,
                background_job_id=existing.background_job_id,
                reused=True,
            )
        result = DiagnosticScheduleResult(
            diagnostic_task_id=f"diagnostic_{incident_id}",
            background_job_id=f"job_{incident_id}",
            reused=False,
        )
        self.results[key] = result
        return result


class Runtime:
    def __init__(self) -> None:
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1


def _user(owner_id: str) -> UserRecord:
    return UserRecord(
        id=owner_id,
        email=f"{owner_id}@example.test",
        display_name=owner_id,
        password_hash="hash",
        created_at=NOW,
        updated_at=NOW,
    )


def _app() -> tuple[FastAPI, Runtime]:
    repository = IncidentRepository()
    scheduler = Scheduler()
    runtime = Runtime()

    async def current_user(x_owner: str = Header(alias="x-owner")) -> UserRecord:
        return _user(x_owner)

    app = FastAPI()

    @app.exception_handler(ApiErrorException)
    async def handle_error(request: Request, exc: ApiErrorException) -> object:
        return error_response(request, exc.code, message=exc.message)

    app.include_router(
        create_incident_router(
            current_user_dependency=current_user,
            repository=repository,
            scheduler=scheduler,
            runtime=runtime,
        )
    )
    return app, runtime


async def test_incident_api_is_owner_scoped_and_projects_formal_recovery() -> None:
    app, _ = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get("/aiops/incidents", headers={"x-owner": "owner-a"})
        detail = await client.get(
            "/aiops/incidents/incident_a", headers={"x-owner": "owner-a"}
        )
        cross_owner = await client.get(
            "/aiops/incidents/incident_b", headers={"x-owner": "owner-a"}
        )
        absent = await client.get(
            "/aiops/incidents/missing", headers={"x-owner": "owner-a"}
        )

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]["items"]] == ["incident_a"]
    assert listed.json()["data"]["nextCursor"] is None
    assert detail.json()["data"]["incident"] | {
        "recoveryIntentId": "intent_latest",
        "recoveryExecutionStatus": "executing",
        "productionRecoveryExecution": True,
    } == detail.json()["data"]["incident"]
    assert cross_owner.status_code == absent.status_code == 404
    assert cross_owner.json()["error"] == absent.json()["error"]


async def test_duplicate_diagnose_reuses_task_and_wakes_runtime_once() -> None:
    app, runtime = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/aiops/incidents/incident_a:diagnose", headers={"x-owner": "owner-a"}
        )
        second = await client.post(
            "/aiops/incidents/incident_a:diagnose", headers={"x-owner": "owner-a"}
        )

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["data"]["diagnosticTaskId"] == second.json()["data"][
        "diagnosticTaskId"
    ]
    assert first.json()["data"]["reused"] is False
    assert second.json()["data"]["reused"] is True
    assert runtime.starts == 1


async def test_postgresql_projection_paginates_stably_and_selects_latest_formal_intent(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    older = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
    try:
        async with session_factory() as session, session.begin():
            for owner_id in ("owner-a", "owner-b"):
                session.add(
                    UserModel(
                        id=owner_id,
                        email=f"{owner_id}@example.test",
                        display_name=owner_id,
                        password_hash="hash",
                        created_at=older,
                        updated_at=older,
                    )
                )
            await session.flush()
            for suffix in ("a", "b", "c"):
                task_id = f"diagnostic_{suffix}"
                session.add(
                    DiagnosticTaskModel(
                        id=task_id,
                        owner_user_id="owner-a",
                        status="succeeded",
                        query="Diagnose order service",
                        input_payload={},
                        result_payload={"agentMode": "multi"},
                        created_at=older,
                        updated_at=NOW,
                        completed_at=NOW,
                    )
                )
                await session.flush()
                session.add(
                    DiagnosticReportModel(
                        id=f"report_{suffix}",
                        owner_user_id="owner-a",
                        task_id=task_id,
                        title=f"Report {suffix}",
                        content="Safe report",
                        payload={"recoveryPlan": {"mode": "manual_review"}},
                        created_at=NOW,
                    )
                )
                await session.flush()
                session.add(
                    _incident_model(
                        incident_id=f"incident_{suffix}",
                        owner_id="owner-a",
                        task_id=task_id,
                        group_hash=(suffix * 64),
                    )
                )
            other_task = DiagnosticTaskModel(
                id="diagnostic_other",
                owner_user_id="owner-b",
                status="succeeded",
                query="Other owner",
                input_payload={},
                result_payload={},
                created_at=older,
                updated_at=NOW,
                completed_at=NOW,
            )
            session.add(other_task)
            await session.flush()
            session.add(
                _incident_model(
                    incident_id="incident_other",
                    owner_id="owner-b",
                    task_id=other_task.id,
                    group_hash="z" * 64,
                )
            )
            await session.flush()
            session.add_all(
                [
                    _intent_model(
                        intent_id="intent_old",
                        incident_id="incident_a",
                        created_at=older,
                        status="denied",
                        fingerprint="1" * 64,
                    ),
                    _intent_model(
                        intent_id="intent_latest",
                        incident_id="incident_a",
                        created_at=NOW,
                        status="executing",
                        fingerprint="2" * 64,
                    ),
                ]
            )

        repository = SQLAlchemyAlertIngestionRepository(session_factory)
        first = await repository.list_owned(
            owner_user_id="owner-a", status="active", limit=2, cursor=None
        )
        second = await repository.list_owned(
            owner_user_id="owner-a",
            status="active",
            limit=2,
            cursor=first.next_cursor,
        )
        projected = await repository.get_owned(
            owner_user_id="owner-a", incident_id="incident_a"
        )
        cross_owner = await repository.get_owned(
            owner_user_id="owner-a", incident_id="incident_other"
        )
    finally:
        await engine.dispose()

    assert [item.id for item in first.items] == ["incident_a", "incident_b"]
    assert first.next_cursor is not None
    assert [item.id for item in second.items] == ["incident_c"]
    assert second.next_cursor is None
    assert projected is not None
    assert projected.recovery_intent_id == "intent_latest"
    assert projected.recovery_execution_status == "executing"
    assert cross_owner is None


def _incident_model(
    *, incident_id: str, owner_id: str, task_id: str, group_hash: str
) -> AlertIncidentModel:
    return AlertIncidentModel(
        id=incident_id,
        owner_user_id=owner_id,
        source_id="local-alertmanager",
        group_key_hash=group_hash,
        run_id=None,
        scenario_id=None,
        status="active",
        alert_name="OrderPoolExhausted",
        service="order-service",
        severity="critical",
        starts_at=NOW,
        last_seen_at=NOW,
        resolved_at=None,
        verification_status="pending",
        verified_at=None,
        verification_summary=None,
        delivery_count=1,
        diagnostic_task_id=task_id,
        created_at=NOW,
        updated_at=NOW,
    )


def _intent_model(
    *,
    intent_id: str,
    incident_id: str,
    created_at: datetime,
    status: str,
    fingerprint: str,
) -> ProductionRecoveryIntentModel:
    return ProductionRecoveryIntentModel(
        id=intent_id,
        owner_user_id="owner-a",
        incident_id=incident_id,
        diagnostic_task_id="diagnostic_a",
        report_id="report_a",
        action="restart_compose_service",
        target_key="order-api",
        canonical_arguments={},
        proposal_fingerprint=fingerprint,
        evidence_ids=[],
        validator_origin="deterministic",
        policy_authorization_code="allowed",
        risk_tier="low",
        automatic_eligible=True,
        approval_required=False,
        status=status,
        execution_key=None,
        background_job_id=None,
        approval_expires_at=None,
        trusted_snapshot={},
        execution_summary=None,
        verification_checks=[],
        safe_reason_code=None,
        created_at=created_at,
        updated_at=created_at,
        started_at=None,
        completed_at=created_at if status == "denied" else None,
    )
