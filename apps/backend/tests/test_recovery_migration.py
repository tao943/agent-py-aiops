from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_recovery_migration_preserves_legacy_request_and_downgrades_only_new_tables(
    migrated_database_url: str,
) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", migrated_database_url)

    async def seed_legacy() -> None:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id,email,display_name,password_hash,created_at,updated_at) "
                        "VALUES ('recovery-owner','recovery-owner@example.test',"
                        "'Recovery Owner','hash',now(),now())"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO aiops_diagnostic_tasks "
                        "(id,owner_user_id,status,query,input_payload,result_payload,"
                        "created_at,updated_at) VALUES "
                        "('legacy-diagnostic','recovery-owner','succeeded','legacy',"
                        "'{}'::jsonb,'{}'::jsonb,now(),now())"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO aiops_recovery_approval_requests "
                        "(id,owner_user_id,diagnostic_task_id,proposal_fingerprint,"
                        "request_reason,chat_run_id,status,execution_permitted,"
                        "created_at,updated_at) "
                        "VALUES ('legacy-recovery','recovery-owner','legacy-diagnostic',"
                        ":fingerprint,'legacy request',NULL,'pending',false,now(),now())"
                    ),
                    {"fingerprint": "a" * 64},
                )
        finally:
            await engine.dispose()

    async def inspect_state() -> tuple[set[str], tuple[str, bool] | None]:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                tables = set(
                    str(name)
                    for name in (
                        await connection.execute(
                            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                        )
                    ).scalars()
                )
                row = (
                    await connection.execute(
                        text(
                            "SELECT status, execution_permitted "
                            "FROM aiops_recovery_approval_requests "
                            "WHERE id='legacy-recovery'"
                        )
                    )
                ).one_or_none()
                return tables, (str(row[0]), bool(row[1])) if row else None
        finally:
            await engine.dispose()

    async def cleanup() -> None:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM aiops_recovery_approval_requests WHERE id='legacy-recovery'")
                )
                await connection.execute(
                    text("DELETE FROM aiops_diagnostic_tasks WHERE id='legacy-diagnostic'")
                )
                await connection.execute(text("DELETE FROM users WHERE id='recovery-owner'"))
        finally:
            await engine.dispose()

    command.downgrade(config, "202608220007")
    try:
        asyncio.run(seed_legacy())
        command.upgrade(config, "202608230001")
        upgraded_tables, upgraded_legacy = asyncio.run(inspect_state())
        assert {
            "production_recovery_intents",
            "production_recovery_approvals",
            "production_recovery_audit_events",
        }.issubset(upgraded_tables)
        assert upgraded_legacy == ("pending", False)

        command.downgrade(config, "202608220007")
        downgraded_tables, downgraded_legacy = asyncio.run(inspect_state())
        assert "production_recovery_intents" not in downgraded_tables
        assert "production_recovery_approvals" not in downgraded_tables
        assert "production_recovery_audit_events" not in downgraded_tables
        assert downgraded_legacy == ("pending", False)
        asyncio.run(cleanup())
    finally:
        command.upgrade(config, "head")
