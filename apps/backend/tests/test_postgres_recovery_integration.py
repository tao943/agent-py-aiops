from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.recovery.config import (
    DiagnosticSelector,
    PostgresLockResource,
    PostgresRecoveryTarget,
)
from super_ai.recovery.postgres import (
    PostgresPreflightExpectation,
    PostgresRecoveryExecutor,
    SQLAlchemyPostgresRecoveryAdapter,
)
from super_ai.recovery.proposal_adapter import lock_relationship_fingerprint
from super_ai.recovery.repository import RecoveryApprovalRecord

NOW = datetime(2026, 8, 23, 9, 30, tzinfo=timezone.utc)


class ResolvedIncidents:
    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        return AlertIncidentRecord(
            incident_id,
            owner_user_id,
            "resolved",
            "PostgresLockWait",
            "postgresql",
            "critical",
            NOW,
            "diagnostic-1",
        )


async def _safe_close(connection: AsyncConnection | None) -> None:
    if connection is None:
        return
    try:
        await connection.close()
    except (DBAPIError, SQLAlchemyError):
        pass


@pytest.mark.asyncio
async def test_real_postgres_terminates_only_fresh_unique_blocker_and_verifies(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    blocker: AsyncConnection | None = None
    waiter: AsyncConnection | None = None
    unrelated: AsyncConnection | None = None
    waiter_task: asyncio.Task[object] | None = None
    resource = PostgresLockResource("order_row", "recovery_test", "orders")
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
            database_identity = str(
                (await setup.execute(text("SELECT current_database()"))).scalar_one()
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
                text("UPDATE recovery_test.orders SET status = 'recovered' WHERE id = 1")
            )
        )
        adapter = SQLAlchemyPostgresRecoveryAdapter(session_factory)
        snapshot = None
        for _ in range(40):
            snapshot = await adapter.probe(resource)
            if len(snapshot.relations) == 1:
                break
            await asyncio.sleep(0.05)
        assert snapshot is not None
        assert len(snapshot.relations) == 1
        relation = snapshot.relations[0]
        unrelated_pid = int(
            (await unrelated.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        target = PostgresRecoveryTarget(
            "agent-py-postgres",
            "backend",
            database_identity,
            DiagnosticSelector(
                "postgresql",
                ("row_lock_blocking",),
                (
                    "InspectPostgresLockGraph.blockerEdgeConfirmed",
                    "InspectPostgresLockGraph.blockerRole",
                    "InspectPostgresLockGraph.lockedResource",
                ),
            ),
            {resource.logical_resource: resource},
        )
        relationship_fingerprint = lock_relationship_fingerprint(
            target_key=target.target_key,
            database_config_key=target.database_config_key,
            database_identity=target.database_identity,
            component="postgresql",
            mechanism="row_lock_blocking",
            required_values={
                "InspectPostgresLockGraph.blockerEdgeConfirmed": True,
                "InspectPostgresLockGraph.blockerRole": "transaction",
                "InspectPostgresLockGraph.lockedResource": "order_row",
            },
        )
        expectation = PostgresPreflightExpectation(
            "owner-1",
            "incident-1",
            "intent-1",
            "a" * 64,
            relationship_fingerprint,
            "order_row",
        )
        approval = RecoveryApprovalRecord(
            "approval-1",
            "intent-1",
            "owner-1",
            "owner-1",
            "incident-1",
            "a" * 64,
            "approved",
            NOW,
            NOW + timedelta(minutes=10),
        )
        executor = PostgresRecoveryExecutor(
            target=target,
            adapter=adapter,
            incidents=ResolvedIncidents(),
            now=lambda: NOW,
        )
        preflight = await executor.preflight(expectation)
        assert preflight.allowed is True

        execution = await executor.execute_once(expectation, preflight, approval)
        assert execution.succeeded is True
        assert execution.outcome_known is True
        assert waiter_task is not None
        await asyncio.wait_for(waiter_task, timeout=5.0)
        await waiter_transaction.commit()
        verification = await executor.verify(expectation, relation)
        unrelated_still_alive = int(
            (await unrelated.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

        assert verification.passed is True
        assert unrelated_still_alive == unrelated_pid
        try:
            await blocker_transaction.rollback()
        except (DBAPIError, SQLAlchemyError):
            pass
    finally:
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

