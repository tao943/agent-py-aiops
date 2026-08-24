from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine


async def test_agent_configuration_schema_is_migrated(migrated_database_url: str) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            chat_columns = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_columns("chat_agent_runs")
                }
            )
            diagnostic_columns = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_columns("aiops_diagnostic_tasks")
                }
            )
        assert {
            "agent_config_resources",
            "agent_config_versions",
            "agent_config_bindings",
            "agent_config_audit_events",
        } <= set(table_names)
        assert "agent_configuration_snapshot" in chat_columns
        assert "agent_configuration_snapshot" in diagnostic_columns
    finally:
        await engine.dispose()
