import asyncio
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
LEGACY_SKILL_DESCRIPTION = "由旧版聊天配置迁移的 Skill；请重新上传标准 SKILL.md 以补充准确描述。"
PYTHON_LSTRIP_WHITESPACE_CODE_POINTS = (
    *range(9, 14),
    *range(28, 33),
    133,
    160,
    5760,
    *range(8192, 8203),
    8232,
    8233,
    8239,
    8287,
    12288,
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


def test_chat_skill_metadata_backfill_matches_legacy_python_behavior(
    migrated_database_url: str,
) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    owner_user_id = "legacy-skill-owner"
    samples = [
        ("legacy-skill-empty", ""),
        ("legacy-skill-plain", "legacy skill body"),
        ("legacy-skill-ascii", " \t\n---\nname: existing\n---\n"),
        *[
            (
                f"legacy-skill-unicode-{code_point}",
                f"{chr(code_point)}---\nname: existing-{code_point}\n---\n",
            )
            for code_point in PYTHON_LSTRIP_WHITESPACE_CODE_POINTS
        ],
    ]

    async def seed_legacy_skills() -> None:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, display_name, password_hash, created_at, updated_at) "
                        "VALUES (:id, :email, :display_name, :password_hash, now(), now())"
                    ),
                    {
                        "id": owner_user_id,
                        "email": "legacy-skill-owner@example.test",
                        "display_name": "Legacy skill owner",
                        "password_hash": "not-a-real-password-hash",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO user_chat_skills "
                        "(id, owner_user_id, filename, content, size_bytes, "
                        "created_at, updated_at) "
                        "VALUES (:id, :owner_user_id, :filename, :content, :size_bytes, "
                        "now(), now())"
                    ),
                    [
                        {
                            "id": skill_id,
                            "owner_user_id": owner_user_id,
                            "filename": f"{skill_id}.md",
                            "content": content,
                            "size_bytes": len(content.encode()),
                        }
                        for skill_id, content in samples
                    ],
                )
        finally:
            await engine.dispose()

    async def read_backfilled_skills() -> dict[str, dict[str, str]]:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT id, name, description, content FROM user_chat_skills "
                            "WHERE owner_user_id = :owner_user_id ORDER BY id"
                        ),
                        {"owner_user_id": owner_user_id},
                    )
                ).mappings()
                return {str(row["id"]): dict(row) for row in rows}
        finally:
            await engine.dispose()

    async def clean_legacy_skills() -> None:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM user_chat_skills WHERE owner_user_id = :owner_user_id"),
                    {"owner_user_id": owner_user_id},
                )
                await connection.execute(
                    text("DELETE FROM users WHERE id = :owner_user_id"),
                    {"owner_user_id": owner_user_id},
                )
        finally:
            await engine.dispose()

    command.downgrade(config, "202607110003")
    try:
        asyncio.run(seed_legacy_skills())

        command.upgrade(config, "202607110004")
        actual = asyncio.run(read_backfilled_skills())

        for skill_id, content in samples:
            expected_name = f"legacy-skill-{skill_id[-12:].lower()}"
            expected_content = content
            if not expected_content.lstrip().startswith("---"):
                expected_content = (
                    f"---\nname: {expected_name}\ndescription: {LEGACY_SKILL_DESCRIPTION}"
                    f"\n---\n\n{expected_content}"
                )
            assert actual[skill_id] == {
                "id": skill_id,
                "name": expected_name,
                "description": LEGACY_SKILL_DESCRIPTION,
                "content": expected_content,
            }
    finally:
        asyncio.run(clean_legacy_skills())
        command.upgrade(config, "head")


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
