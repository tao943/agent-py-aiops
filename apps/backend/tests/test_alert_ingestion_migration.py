from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_alert_ingestion_schema_has_required_tables_and_columns(
    migrated_database_url: str,
) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            incident_columns = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_columns("aiops_alert_incidents")
                }
            )
            event_columns = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_columns("aiops_alert_events")
                }
            )
    finally:
        await engine.dispose()

    assert {"aiops_alert_incidents", "aiops_alert_events"} <= tables
    assert {
        "id",
        "owner_user_id",
        "source_id",
        "group_key_hash",
        "status",
        "alert_name",
        "service",
        "severity",
        "starts_at",
        "last_seen_at",
        "resolved_at",
        "delivery_count",
        "diagnostic_task_id",
        "run_id",
        "scenario_id",
        "verification_status",
        "verified_at",
        "verification_summary",
        "created_at",
        "updated_at",
    } == incident_columns
    assert {
        "id",
        "incident_id",
        "owner_user_id",
        "source_id",
        "status",
        "disposition",
        "payload_sha256",
        "normalized_payload",
        "received_at",
    } == event_columns


async def test_alert_ingestion_schema_has_partial_active_uniqueness_and_checks(
    migrated_database_url: str,
) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.connect() as connection:
            indexes = list(
                (
                    await connection.execute(
                        text(
                            "select indexdef from pg_indexes "
                            "where schemaname = 'public' "
                            "and tablename = 'aiops_alert_incidents'"
                        )
                    )
                ).scalars()
            )
            checks = list(
                (
                    await connection.execute(
                        text(
                            "select pg_get_constraintdef(oid) from pg_constraint "
                            "where conrelid in "
                            "('aiops_alert_incidents'::regclass, 'aiops_alert_events'::regclass) "
                            "and contype = 'c'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert any(
        "UNIQUE" in definition
        and "owner_user_id" in definition
        and "source_id" in definition
        and "group_key_hash" in definition
        and "WHERE" in definition
        and "active" in definition
        for definition in indexes
    )
    constraints = " ".join(checks)
    assert "active" in constraints and "resolved" in constraints
    for value in (
        "incident_created",
        "duplicate_updated",
        "incident_resolved",
        "filtered",
        "orphan_resolved",
    ):
        assert value in constraints
