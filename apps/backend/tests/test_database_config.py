from pathlib import Path

from super_ai.memory.database import load_memory_database_settings

ROOT = Path(__file__).resolve().parents[3]


def test_development_config_uses_postgresql_asyncpg() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.json")
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "sqlite" not in settings.database_url


def test_test_config_targets_isolated_database() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.test.json")
    assert settings.database_url.endswith("/agent_py_test")
