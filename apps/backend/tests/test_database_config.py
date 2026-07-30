from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

import super_ai.memory.database as memory_database
from super_ai.memory.database import create_memory_engine, load_memory_database_settings

ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = ROOT / "apps" / "backend"
UNSUPPORTED_POSTGRESQL_URL = (
    "postgresql+psycopg://agent_py:agent_py_dev@localhost:5432/agent_py_test"
)


def test_committed_alembic_config_has_no_legacy_sqlite_default() -> None:
    alembic_config = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_config_text = (BACKEND_ROOT / "alembic.ini").read_text(encoding="utf-8")
    alembic_environment_text = (BACKEND_ROOT / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )

    assert alembic_config.get_main_option("sqlalchemy.url") == ""
    assert "sqlite" not in alembic_config_text.lower()
    assert "aiosqlite" not in alembic_config_text.lower()
    assert "sqlite" not in alembic_environment_text.lower()
    assert "aiosqlite" not in alembic_environment_text.lower()
    assert "legacy_database_url_placeholder" not in alembic_environment_text.lower()


def test_current_product_metadata_has_no_legacy_sqlite_terminology() -> None:
    metadata_sources = (
        ROOT / "packages" / "api-contracts" / "src" / "openapi.ts",
        ROOT / "scripts" / "generate_architecture_diagrams.py",
    )

    for metadata_source in metadata_sources:
        metadata_text = metadata_source.read_text(encoding="utf-8").lower()
        assert "sqlite" not in metadata_text
        assert "aiosqlite" not in metadata_text


def test_development_config_uses_postgresql_asyncpg() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.json")
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "sqlite" not in settings.database_url


def test_test_config_targets_isolated_database() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.test.json")
    assert settings.database_url.endswith("/agent_py_test")


def test_engine_rejects_non_asyncpg_postgresql_urls() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        create_memory_engine(UNSUPPORTED_POSTGRESQL_URL)


def test_shared_url_validation_rejects_non_asyncpg_postgresql_urls() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        memory_database.validate_memory_database_url(UNSUPPORTED_POSTGRESQL_URL)


def test_alembic_rejects_explicit_postgresql_url_without_asyncpg() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", UNSUPPORTED_POSTGRESQL_URL)

    with pytest.raises(ValueError, match="asyncpg"):
        command.upgrade(config, "202607080001", sql=True)


def test_engine_uses_asyncpg_driver() -> None:
    engine = create_memory_engine(
        "postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py_test"
    )
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"
