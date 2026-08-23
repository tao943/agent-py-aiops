from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from super_ai.api.app import create_app
from super_ai.memory.database import create_memory_engine
from super_ai.memory.models import (
    AiopsExecutionModel,
    AlertIncidentModel,
    ProductionRecoveryAuditEventModel,
    ProductionRecoveryIntentModel,
)
from super_ai.redis_runtime.rate_limit import RateLimitDecision
from super_ai.vector_store import MilvusHealthCheckResult

ROOT = Path(__file__).resolve().parents[4]


class FakeVectorStore:
    def health_check(self) -> MilvusHealthCheckResult:
        return MilvusHealthCheckResult(True, "http://milvus.test", "chunks", 1.0)


class AllowAllRateLimits:
    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision:
        del owner_id, action
        return RateLimitDecision(True, 1, 0, "local_fallback")


def _project_config(tmp_path: Path) -> Path:
    configuration = json.loads(
        (ROOT / "config" / "project.test.json").read_text(encoding="utf-8")
    )
    configuration["productionRecovery"] = {
        "enabled": True,
        "approvalTtlSeconds": 600,
        "composeTargets": [],
        "postgresTargets": [
            {
                "targetKey": "agent-py-postgres",
                "databaseConfigKey": "backend",
                "databaseIdentity": "agent_py_test",
                "diagnosticSelector": {
                    "component": "postgresql",
                    "mechanisms": ["row_lock_blocking"],
                    "requiredEvidenceFacts": [
                        "InspectPostgresLockGraph.blockerEdgeConfirmed",
                        "InspectPostgresLockGraph.blockerRole",
                        "InspectPostgresLockGraph.lockedResource",
                    ],
                },
                "lockResourceMappings": [
                    {
                        "logicalResource": "order_row",
                        "schema": "recovery_test",
                        "relation": "orders",
                    }
                ],
            }
        ],
    }
    path = tmp_path / "project.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    return path


async def _register(client: httpx.AsyncClient, label: str) -> Mapping[str, Any]:
    response = await client.post(
        "/auth/register",
        json={
            "email": f"{label}-{uuid4().hex}@example.test",
            "displayName": label,
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    return cast(Mapping[str, Any], response.json()["data"])


def _auth(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_grounded_diagnosis(
    app: Any,
    *,
    owner_user_id: str,
    task_id: str,
    incident_id: str,
) -> None:
    now = datetime.now(timezone.utc)
    report_id = f"report-{task_id}"
    evidence_id = f"evidence-{task_id}"
    diagnostics = app.state.memory_repositories.diagnostics
    await diagnostics.create_task(
        owner_user_id=owner_user_id,
        task_id=task_id,
        status="succeeded",
        query="Investigate the observed PostgreSQL row lock wait.",
        input_payload={"alert": {"service": "postgresql"}},
        result_payload={"status": "succeeded"},
        created_at=now,
        completed_at=now,
    )
    await diagnostics.add_report(
        owner_user_id=owner_user_id,
        report_id=report_id,
        task_id=task_id,
        title="Grounded PostgreSQL lock diagnosis",
        content="One transaction is the unique blocker for the order row waiter.",
        payload={
            "status": "succeeded",
            "rootCauseDecision": {
                "component": "postgresql",
                "mechanism": "row_lock_blocking",
                "evidenceIds": [evidence_id],
            },
            "decisionValidation": {
                "status": "valid",
                "validationOrigin": "deterministic",
                "deterministicChecks": [{"code": "grounded", "passed": True}],
            },
            "evidenceSufficiency": {"status": "sufficient"},
        },
        created_at=now,
    )
    await diagnostics.create_evidence(
        owner_user_id=owner_user_id,
        evidence_id=evidence_id,
        task_id=task_id,
        kind="database",
        source="InspectPostgresLockGraph",
        summary="A unique transaction blocker holds the mapped order row.",
        payload={
            "output": {
                "blockerEdgeConfirmed": True,
                "blockerRole": "transaction",
                "lockedResource": "order_row",
            }
        },
        created_at=now,
    )
    await diagnostics.link_report_evidence(
        owner_user_id=owner_user_id,
        link_id=f"link-{task_id}",
        task_id=task_id,
        report_id=report_id,
        evidence_id=evidence_id,
        created_at=now,
    )
    async with app.state.memory_session_factory() as session, session.begin():
        session.add(
            AlertIncidentModel(
                id=incident_id,
                owner_user_id=owner_user_id,
                source_id="production-recovery-live",
                group_key_hash=uuid4().hex + uuid4().hex,
                run_id=task_id,
                scenario_id="PRODUCTION-RECOVERY-POSTGRES-001",
                status="active",
                alert_name="PostgresRowLockWait",
                service="postgresql",
                severity="critical",
                starts_at=now,
                last_seen_at=now,
                resolved_at=None,
                verification_status="pending",
                verified_at=None,
                verification_summary=None,
                delivery_count=1,
                diagnostic_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
        )


async def _resolve_after_waiter_progress(
    app: Any,
    *,
    incident_id: str,
    waiter_task: asyncio.Task[object],
) -> None:
    for _ in range(300):
        if waiter_task.done():
            await waiter_task
            now = datetime.now(timezone.utc)
            async with app.state.memory_session_factory() as session, session.begin():
                await session.execute(
                    update(AlertIncidentModel)
                    .where(AlertIncidentModel.id == incident_id)
                    .values(status="resolved", resolved_at=now, updated_at=now)
                )
            return
        await asyncio.sleep(0.05)
    raise AssertionError("PostgreSQL waiter did not progress after recovery.")


async def _safe_close(connection: AsyncConnection | None) -> None:
    if connection is None:
        return
    try:
        await connection.close()
    except (DBAPIError, SQLAlchemyError):
        pass


@pytest.mark.asyncio
async def test_postgres_recovery_requires_owner_approval_and_closes_once(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    blocker: AsyncConnection | None = None
    waiter: AsyncConnection | None = None
    unrelated: AsyncConnection | None = None
    waiter_task: asyncio.Task[object] | None = None
    app: Any | None = None
    try:
        async with engine.begin() as setup:
            await setup.execute(text("DROP SCHEMA IF EXISTS recovery_test CASCADE"))
            await setup.execute(text("CREATE SCHEMA recovery_test"))
            await setup.execute(
                text(
                    "CREATE TABLE recovery_test.orders "
                    "(id integer PRIMARY KEY, status text NOT NULL)"
                )
            )
            await setup.execute(
                text(
                    "INSERT INTO recovery_test.orders (id, status) "
                    "VALUES (1, 'pending')"
                )
            )
        blocker = await engine.connect()
        waiter = await engine.connect()
        unrelated = await engine.connect()
        await blocker.execute(text("SET application_name = 'order-worker-blocker'"))
        await waiter.execute(text("SET application_name = 'order-api-waiter'"))
        await unrelated.execute(text("SET application_name = 'unrelated-session'"))
        await blocker.commit()
        await waiter.commit()
        await unrelated.commit()
        blocker_transaction = await blocker.begin()
        waiter_transaction = await waiter.begin()
        await blocker.execute(
            text("UPDATE recovery_test.orders SET status = 'held' WHERE id = 1")
        )
        waiter_task = asyncio.create_task(
            waiter.execute(
                text(
                    "UPDATE recovery_test.orders SET status = 'recovered' WHERE id = 1"
                )
            )
        )
        unrelated_identity = int(
            (await unrelated.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

        app = create_app(
            database_url=migrated_database_url,
            project_config_path=_project_config(tmp_path),
            vector_store=cast(Any, FakeVectorStore()),
            rate_limit_service=AllowAllRateLimits(),  # type: ignore[arg-type]
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            owner = await _register(client, "Recovery Owner")
            other = await _register(client, "Other Owner")
            owner_user_id = cast(str, cast(Mapping[str, object], owner["user"])["id"])
            task_id = f"diagnostic-{uuid4().hex}"
            incident_id = f"incident-{uuid4().hex}"
            await _seed_grounded_diagnosis(
                app,
                owner_user_id=owner_user_id,
                task_id=task_id,
                incident_id=incident_id,
            )
            created = await client.post(
                f"/aiops/diagnostics/{task_id}/recovery-intents",
                headers=_auth(owner["accessToken"]),
                json={"note": "Prepare the governed recovery."},
            )
            assert created.status_code == 201, created.text
            assert created.json()["data"]["status"] == "awaiting_approval"
            intent_id = created.json()["data"]["id"]

            wrong_owner = await client.post(
                f"/aiops/recovery-intents/{intent_id}:approve",
                headers=_auth(other["accessToken"]),
                json={"incidentIdConfirmation": incident_id},
            )
            assert wrong_owner.status_code == 403
            wrong_confirmation = await client.post(
                f"/aiops/recovery-intents/{intent_id}:approve",
                headers=_auth(owner["accessToken"]),
                json={"incidentIdConfirmation": f"{incident_id}-wrong"},
            )
            assert wrong_confirmation.status_code == 400

            resolver = asyncio.create_task(
                _resolve_after_waiter_progress(
                    app,
                    incident_id=incident_id,
                    waiter_task=waiter_task,
                )
            )
            approved = await client.post(
                f"/aiops/recovery-intents/{intent_id}:approve",
                headers=_auth(owner["accessToken"]),
                json={"incidentIdConfirmation": incident_id},
            )
            assert approved.status_code == 202, approved.text
            terminal: Mapping[str, object] | None = None
            for _ in range(300):
                response = await client.get(
                    f"/aiops/recovery-intents/{intent_id}",
                    headers=_auth(owner["accessToken"]),
                )
                assert response.status_code == 200
                candidate = cast(Mapping[str, object], response.json()["data"])
                if candidate["status"] in {
                    "recovered",
                    "verification_failed",
                    "manual_intervention",
                    "denied",
                }:
                    terminal = candidate
                    break
                await asyncio.sleep(0.1)
            await resolver
            assert terminal is not None
            assert terminal["status"] == "recovered", terminal
            assert all(
                check["status"] == "passed"
                for check in cast(list[Mapping[str, object]], terminal["verification"])
            )
            events = await client.get(
                f"/aiops/recovery-intents/{intent_id}/events?afterSequence=0",
                headers=_auth(owner["accessToken"]),
            )
            assert [
                item["toStatus"] for item in events.json()["data"]["items"]
            ] == [
                "awaiting_approval",
                "queued",
                "revalidating",
                "executing",
                "verifying",
                "recovered",
            ]

        await waiter_transaction.commit()
        unrelated_after = int(
            (await unrelated.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        assert unrelated_after == unrelated_identity
        try:
            await blocker_transaction.rollback()
        except (DBAPIError, SQLAlchemyError):
            pass
        async with app.state.memory_session_factory() as session:
            intent_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryIntentModel)
            )
            execution_count = await session.scalar(
                select(func.count())
                .select_from(AiopsExecutionModel)
                .where(AiopsExecutionModel.execution_kind == "recovery")
            )
            audit_count = await session.scalar(
                select(func.count()).select_from(ProductionRecoveryAuditEventModel)
            )
        assert intent_count == 1
        assert execution_count == 1
        assert audit_count == 6
    finally:
        if app is not None:
            await app.state.background_job_runtime.stop()
        if waiter_task is not None and not waiter_task.done():
            waiter_task.cancel()
        await _safe_close(waiter)
        await _safe_close(blocker)
        await _safe_close(unrelated)
        try:
            async with engine.begin() as cleanup:
                await cleanup.execute(text("DROP SCHEMA IF EXISTS recovery_test CASCADE"))
        finally:
            await engine.dispose()
