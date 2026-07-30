from __future__ import annotations

from sqlalchemy import Connection, inspect

from super_ai.memory.database import create_memory_engine
from super_ai.memory.models import Base


async def test_auth_tables_are_migrated_without_plaintext_secret_columns(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        async with engine.connect() as connection:
            table_names, user_columns, session_columns, index_names = await connection.run_sync(
                _inspect_auth_schema
            )
    finally:
        await engine.dispose()

    assert {"users", "auth_sessions"} <= table_names
    expected_user_columns = {
        "id",
        "email",
        "display_name",
        "password_hash",
        "created_at",
        "updated_at",
    }
    expected_session_columns = {
        "id",
        "user_id",
        "token_hash",
        "created_at",
        "last_seen_at",
        "revoked_at",
    }

    assert expected_user_columns <= user_columns
    assert {"password", "plain_password"} & user_columns == set()
    assert expected_session_columns <= session_columns
    assert {"token", "access_token"} & session_columns == set()
    assert {"ix_users_email", "ix_auth_sessions_token_hash"} <= index_names


def test_auth_metadata_exposes_user_and_session_tables() -> None:
    tables = Base.metadata.tables

    assert {"users", "auth_sessions"} <= set(tables)
    assert "password_hash" in tables["users"].c
    assert "token_hash" in tables["auth_sessions"].c
    assert "password" not in tables["users"].c
    assert "token" not in tables["auth_sessions"].c


def _inspect_auth_schema(
    connection: Connection,
) -> tuple[set[str], set[str], set[str], set[str | None]]:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    session_columns = {column["name"] for column in inspector.get_columns("auth_sessions")}
    index_names = {
        index["name"]
        for table_name in ("users", "auth_sessions")
        for index in inspector.get_indexes(table_name)
    }
    return table_names, user_columns, session_columns, index_names
