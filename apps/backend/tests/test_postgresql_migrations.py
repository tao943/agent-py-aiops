from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
AGENT_PY_TEST_DATABASE_URL = (
    "postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py_test"
)


def test_alembic_head_renders_offline_for_postgresql() -> None:
    output_buffer = StringIO()
    config = Config(
        str(BACKEND_ROOT / "alembic.ini"),
        output_buffer=output_buffer,
    )
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", AGENT_PY_TEST_DATABASE_URL)

    command.upgrade(config, "head", sql=True)

    assert "202607300001" in output_buffer.getvalue()


async def test_alembic_head_exists_in_postgresql(migrated_database_url: str) -> None:
    engine = create_async_engine(migrated_database_url)
    async with engine.connect() as connection:
        dialect = await connection.scalar(text("select current_setting('server_version_num')"))
        revision = await connection.scalar(text("select version_num from alembic_version"))
    await engine.dispose()
    assert int(dialect) >= 160000
    assert revision


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("background_jobs", "payload"),
        ("background_job_events", "payload"),
        ("mcp_connections", "last_tools"),
        ("knowledge_documents", "metadata"),
        ("user_chat_configurations", "skill_ids"),
        ("chat_messages", "metadata"),
        ("tool_call_audits", "arguments"),
        ("aiops_diagnostic_tasks", "input_payload"),
        ("aiops_diagnostic_tasks", "result_payload"),
        ("aiops_diagnostic_reports", "payload"),
        ("aiops_diagnostic_cases", "keywords"),
        ("aiops_diagnostic_cases", "evidence_ids"),
        ("aiops_diagnostic_steps", "payload"),
        ("aiops_diagnostic_evidence", "payload"),
        ("aiops_tool_call_audits", "arguments"),
        ("aiops_tool_call_audits", "result_payload"),
        ("aiops_graph_checkpoints", "checkpoint_payload"),
        ("aiops_graph_checkpoints", "metadata"),
    ],
)
async def test_json_columns_use_jsonb(
    migrated_database_url: str,
    table_name: str,
    column_name: str,
) -> None:
    engine = create_async_engine(migrated_database_url)
    async with engine.connect() as connection:
        data_type = await connection.scalar(
            text(
                "select data_type from information_schema.columns "
                "where table_schema = 'public' "
                "and table_name = :table_name "
                "and column_name = :column_name"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
    await engine.dispose()
    assert data_type == "jsonb"
