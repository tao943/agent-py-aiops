from pathlib import Path

import pytest

import super_ai.memory.database as memory_database
from super_ai.memory.database import create_memory_engine, load_memory_database_settings

ROOT = Path(__file__).resolve().parents[3]


def test_development_config_uses_postgresql_asyncpg() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.json")
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "sqlite" not in settings.database_url


def test_test_config_targets_isolated_database() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.test.json")
    assert settings.database_url.endswith("/agent_py_test")


def test_engine_rejects_non_postgresql_urls() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        create_memory_engine("sqlite+aiosqlite:///memory.sqlite3")


def test_shared_url_validation_rejects_non_postgresql_urls() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        memory_database.validate_memory_database_url("sqlite+aiosqlite:///memory.sqlite3")


def test_engine_uses_asyncpg_driver() -> None:
    engine = create_memory_engine(
        "postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py_test"
    )
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"
