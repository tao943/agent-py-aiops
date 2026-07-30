from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from super_ai.memory.database import load_memory_database_settings

ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = ROOT / "apps" / "backend"
TEST_PROJECT_CONFIG = ROOT / "config" / "project.test.json"


@pytest.fixture(scope="session")
def migrated_database_url_session() -> str:
    database_url = load_memory_database_settings(TEST_PROJECT_CONFIG).database_url
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    try:
        command.upgrade(config, "head")
    except (OSError, SQLAlchemyError) as exc:
        safe_url = make_url(database_url).render_as_string(hide_password=True)
        raise pytest.UsageError(
            "PostgreSQL integration database is unreachable. "
            f"Start the test database and verify connectivity to {safe_url}."
        ) from exc
    return database_url


@pytest.fixture
async def migrated_database_url(migrated_database_url_session: str) -> AsyncIterator[str]:
    engine = create_async_engine(migrated_database_url_session)
    try:
        async with engine.begin() as connection:
            table_names = (
                await connection.execute(
                    text(
                        "select tablename from pg_tables "
                        "where schemaname = 'public' and tablename <> 'alembic_version'"
                    )
                )
            ).scalars()
            preparer = connection.dialect.identifier_preparer
            quoted_schema = preparer.quote("public")
            quoted_tables = [
                f"{quoted_schema}.{preparer.quote(table_name)}" for table_name in table_names
            ]
            if quoted_tables:
                await connection.execute(
                    text(
                        f"TRUNCATE {', '.join(quoted_tables)} "  # noqa: S608
                        "RESTART IDENTITY CASCADE"
                    )
                )
        yield migrated_database_url_session
    finally:
        await engine.dispose()
