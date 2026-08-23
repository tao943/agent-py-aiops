from __future__ import annotations

from pathlib import Path

import pytest

from super_ai.project_config import ProjectConfigurationError
from super_ai.recovery.config import load_production_recovery_settings


def _compose_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "targetKey": "live-eval-order-api",
        "composeFile": "infra/compose.yaml",
        "service": "live-eval-order-api",
        "automaticRecoveryEnabled": True,
        "healthUrl": "http://127.0.0.1:18081/health",
        "businessProbeUrl": "http://127.0.0.1:18081/live-eval/probe",
        "diagnosticSelector": {
            "component": "order-service",
            "mechanisms": ["application_connection_pool_leak"],
            "requiredEvidenceFacts": [
                "InspectConnectionPool.poolExhausted",
                "InspectConnectionPool.checkoutWithoutCheckin",
            ],
        },
    }
    target.update(overrides)
    return target


def _postgres_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "targetKey": "agent-py-postgres",
        "databaseConfigKey": "backend",
        "diagnosticSelector": {
            "component": "postgresql",
            "mechanisms": ["transaction_blocker_lock_wait"],
            "requiredEvidenceFacts": [
                "InspectPostgresLockGraph.blockerEdgeConfirmed",
                "InspectPostgresLockGraph.blockerRole",
            ],
        },
    }
    target.update(overrides)
    return target


def _config(
    *,
    enabled: object = True,
    ttl: object = 600,
    compose_targets: object | None = None,
    postgres_targets: object | None = None,
) -> dict[str, object]:
    return {
        "productionRecovery": {
            "enabled": enabled,
            "approvalTtlSeconds": ttl,
            "composeTargets": [_compose_target()]
            if compose_targets is None
            else compose_targets,
            "postgresTargets": [_postgres_target()]
            if postgres_targets is None
            else postgres_targets,
        }
    }


def test_missing_section_is_disabled_with_empty_allowlists(tmp_path: Path) -> None:
    settings = load_production_recovery_settings(raw_config={}, project_root=tmp_path)

    assert settings.enabled is False
    assert settings.approval_ttl_seconds == 600
    assert settings.compose_targets == {}
    assert settings.postgres_targets == {}


def test_loads_isolated_compose_and_postgres_targets(tmp_path: Path) -> None:
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "compose.yaml").write_text("services: {}", encoding="utf-8")

    settings = load_production_recovery_settings(
        raw_config=_config(), project_root=tmp_path
    )

    compose = settings.compose_targets["live-eval-order-api"]
    assert compose.compose_file == (tmp_path / "infra" / "compose.yaml").resolve()
    assert compose.service == "live-eval-order-api"
    assert compose.automatic_recovery_enabled is True
    assert settings.postgres_targets["agent-py-postgres"].database_config_key == "backend"
    assert settings.selector_target("order-service", "application_connection_pool_leak") == (
        "live-eval-order-api"
    )


@pytest.mark.parametrize("ttl", [59, 3601, True, 600.0])
def test_rejects_invalid_approval_ttl(tmp_path: Path, ttl: object) -> None:
    with pytest.raises(ProjectConfigurationError, match="approvalTtlSeconds"):
        load_production_recovery_settings(
            raw_config=_config(ttl=ttl), project_root=tmp_path
        )


@pytest.mark.parametrize(
    "compose_target, message",
    [
        (_compose_target(composeFile="../compose.yaml"), "composeFile"),
        (_compose_target(composeFile="C:/outside/compose.yaml"), "composeFile"),
        (_compose_target(service="api; shutdown"), "service"),
        (_compose_target(healthUrl="https://127.0.0.1:18081/health"), "healthUrl"),
        (_compose_target(healthUrl="http://localhost:18081/health"), "healthUrl"),
        (_compose_target(healthUrl="http://10.0.0.1:18081/health"), "healthUrl"),
    ],
)
def test_rejects_unsafe_compose_boundaries(
    tmp_path: Path, compose_target: dict[str, object], message: str
) -> None:
    with pytest.raises(ProjectConfigurationError, match=message):
        load_production_recovery_settings(
            raw_config=_config(compose_targets=[compose_target]),
            project_root=tmp_path,
        )


def test_rejects_duplicate_target_keys(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigurationError, match="target keys"):
        load_production_recovery_settings(
            raw_config=_config(
                compose_targets=[_compose_target()],
                postgres_targets=[_postgres_target(targetKey="live-eval-order-api")],
            ),
            project_root=tmp_path,
        )


def test_rejects_ambiguous_diagnostic_selectors(tmp_path: Path) -> None:
    conflicting = _postgres_target(
        diagnosticSelector={
            "component": "order-service",
            "mechanisms": ["application_connection_pool_leak"],
            "requiredEvidenceFacts": ["InspectPostgresLockGraph.blockerEdgeConfirmed"],
        }
    )
    with pytest.raises(ProjectConfigurationError, match="diagnostic selectors"):
        load_production_recovery_settings(
            raw_config=_config(postgres_targets=[conflicting]), project_root=tmp_path
        )


@pytest.mark.parametrize(
    "selector",
    [
        {"component": "order-service", "mechanisms": [], "requiredEvidenceFacts": ["a"]},
        {
            "component": "order-service",
            "mechanisms": ["pool_leak", "pool_leak"],
            "requiredEvidenceFacts": ["a"],
        },
        {
            "component": "order-service",
            "mechanisms": ["pool_leak"],
            "requiredEvidenceFacts": [],
        },
    ],
)
def test_rejects_empty_or_duplicate_selector_values(
    tmp_path: Path, selector: dict[str, object]
) -> None:
    with pytest.raises(ProjectConfigurationError, match="diagnosticSelector"):
        load_production_recovery_settings(
            raw_config=_config(
                compose_targets=[_compose_target(diagnosticSelector=selector)]
            ),
            project_root=tmp_path,
        )


def test_public_target_summary_never_exposes_resolved_path(tmp_path: Path) -> None:
    settings = load_production_recovery_settings(
        raw_config=_config(postgres_targets=[]), project_root=tmp_path
    )

    summary = settings.compose_targets["live-eval-order-api"].public_summary()
    assert summary == {
        "targetKey": "live-eval-order-api",
        "service": "live-eval-order-api",
        "automaticRecoveryEnabled": True,
        "verificationCapabilities": [
            "container_identity_changed",
            "service_health",
            "business_probe",
            "incident_resolved",
        ],
    }
    assert str(tmp_path).lower() not in str(summary).lower()
