from __future__ import annotations

import json
from pathlib import Path

import pytest

from super_ai.alert_ingestion.config import load_alert_ingestion_settings
from super_ai.project_config import ProjectConfigurationError

ROOT = Path(__file__).resolve().parents[3]


def _source(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "id": "local-alertmanager",
        "enabled": True,
        "ownerUserId": "svc",
        "knowledgeBaseId": "kb_svc",
        "tokenEnvironmentVariable": "HOOK_TOKEN",
        "allowedLabels": {
            "environment": ["test", "prod"],
            "severity": ["warning", "critical"],
        },
    }
    source.update(overrides)
    return source


def _write_config(
    tmp_path: Path, sources: list[dict[str, object]], *, enabled: bool = True
) -> Path:
    path = tmp_path / "project.json"
    path.write_text(
        json.dumps(
            {
                "alertIngestion": {
                    "enabled": enabled,
                    "maxBodyBytes": 262144,
                    "maxAlertsPerDelivery": 50,
                    "redisLeaseMilliseconds": 2000,
                    "sources": sources,
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_enabled_source_binds_owner_knowledge_base_token_and_filters(tmp_path: Path) -> None:
    settings = load_alert_ingestion_settings(
        _write_config(tmp_path, [_source()]),
        environ={"HOOK_TOKEN": "x" * 32},
    )

    source = settings.sources["local-alertmanager"]
    assert source.owner_user_id == "svc"
    assert source.knowledge_base_id == "kb_svc"
    assert source.token == "x" * 32
    assert source.matches({"environment": "test", "severity": "critical"}) is True
    assert source.matches({"environment": "dev", "severity": "critical"}) is False


def test_missing_section_defaults_to_disabled() -> None:
    settings = load_alert_ingestion_settings(Path("config/does-not-exist.json"), raw_config={})

    assert settings.enabled is False
    assert settings.max_body_bytes == 262144
    assert settings.sources == {}


@pytest.mark.parametrize(
    "source_patch",
    [
        {"id": "../unsafe"},
        {"knowledgeBaseId": "kb_other"},
        {"allowedLabels": {}},
        {"tokenEnvironmentVariable": "bad-name"},
    ],
)
def test_invalid_enabled_source_fails_closed(
    source_patch: dict[str, object], tmp_path: Path
) -> None:
    with pytest.raises(ProjectConfigurationError):
        load_alert_ingestion_settings(
            _write_config(tmp_path, [_source(**source_patch)]),
            environ={"HOOK_TOKEN": "x" * 32},
        )


def test_short_or_missing_token_fails_closed(tmp_path: Path) -> None:
    path = _write_config(tmp_path, [_source()])
    with pytest.raises(ProjectConfigurationError):
        load_alert_ingestion_settings(path, environ={"HOOK_TOKEN": "short"})
    with pytest.raises(ProjectConfigurationError):
        load_alert_ingestion_settings(path, environ={})


def test_duplicate_source_id_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigurationError):
        load_alert_ingestion_settings(
            _write_config(tmp_path, [_source(), _source()]),
            environ={"HOOK_TOKEN": "x" * 32},
        )


def test_disabled_source_does_not_require_environment_token(tmp_path: Path) -> None:
    settings = load_alert_ingestion_settings(
        _write_config(tmp_path, [_source(enabled=False)]),
        environ={},
    )

    assert settings.enabled is True
    assert settings.sources == {}


@pytest.mark.parametrize("name", ["project.template.json", "project.test.json"])
def test_tracked_configs_keep_alert_ingestion_disabled_without_tokens(name: str) -> None:
    configuration = json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))

    ingestion = configuration["alertIngestion"]
    assert ingestion == {
        "enabled": False,
        "maxBodyBytes": 262144,
        "maxAlertsPerDelivery": 50,
        "redisLeaseMilliseconds": 2000,
        "sources": [],
    }
