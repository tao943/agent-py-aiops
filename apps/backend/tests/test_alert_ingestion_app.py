from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from super_ai.alert_ingestion.config import AlertSourceConfig
from super_ai.alert_ingestion.domain import AlertmanagerDelivery
from super_ai.alert_ingestion.repositories import IngestionResult
from super_ai.api.app import create_app
from super_ai.project_config import ProjectConfigurationError
from super_ai.vector_store import MilvusHealthCheckResult

ROOT = Path(__file__).resolve().parents[3]
WEBHOOK_PATH = "/aiops/alerts/webhook/alertmanager/{source_id}"


class FakeVectorStore:
    def health_check(self) -> MilvusHealthCheckResult:
        return MilvusHealthCheckResult(True, "http://milvus.test", "chunks", 1.0)


class FakeIngestionService:
    async def ingest(
        self,
        source: AlertSourceConfig,
        delivery: AlertmanagerDelivery,
    ) -> IngestionResult:
        del source, delivery
        return IngestionResult("filtered", None, None, None, "primary")


def _config(tmp_path: Path, *, enabled: bool) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    configuration = json.loads(
        (ROOT / "config" / "project.test.json").read_text(encoding="utf-8")
    )
    configuration["alertIngestion"] = {
        "enabled": enabled,
        "maxBodyBytes": 262144,
        "maxAlertsPerDelivery": 50,
        "redisLeaseMilliseconds": 2000,
        "sources": [
            {
                "id": "local-alertmanager",
                "enabled": True,
                "ownerUserId": "owner",
                "knowledgeBaseId": "kb_owner",
                "tokenEnvironmentVariable": "TEST_ALERT_TOKEN",
                "allowedLabels": {"environment": ["test"]},
            }
        ],
    }
    path = tmp_path / "project.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    return path


def test_create_app_mounts_webhook_only_when_ingestion_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_ALERT_TOKEN", "x" * 32)
    disabled = create_app(
        project_config_path=_config(tmp_path / "disabled", enabled=False),
        vector_store=FakeVectorStore(),
    )
    enabled = create_app(
        project_config_path=_config(tmp_path / "enabled", enabled=True),
        vector_store=FakeVectorStore(),
        alert_ingestion_service=FakeIngestionService(),
    )

    disabled_paths = cast(dict[str, object], disabled.openapi()["paths"])
    enabled_paths = cast(dict[str, object], enabled.openapi()["paths"])
    assert WEBHOOK_PATH not in disabled_paths
    assert WEBHOOK_PATH in enabled_paths


def test_enabled_source_with_missing_token_fails_during_app_creation(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigurationError, match="missing or too short"):
        create_app(
            project_config_path=_config(tmp_path, enabled=True),
            vector_store=FakeVectorStore(),
            alert_ingestion_service=FakeIngestionService(),
        )
