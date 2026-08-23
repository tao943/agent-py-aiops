"""Fail-closed configuration for governed production recovery targets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from super_ai.project_config import ProjectConfigurationError, load_project_config

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_FACT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,159}$")
_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True, slots=True)
class DiagnosticSelector:
    component: str
    mechanisms: tuple[str, ...]
    required_evidence_facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComposeRecoveryTarget:
    target_key: str
    compose_file: Path
    service: str
    automatic_recovery_enabled: bool
    health_url: str
    business_probe_url: str
    diagnostic_selector: DiagnosticSelector

    def public_summary(self) -> dict[str, object]:
        return {
            "targetKey": self.target_key,
            "service": self.service,
            "automaticRecoveryEnabled": self.automatic_recovery_enabled,
            "verificationCapabilities": [
                "container_identity_changed",
                "service_health",
                "business_probe",
                "incident_resolved",
            ],
        }


@dataclass(frozen=True, slots=True)
class PostgresLockResource:
    logical_resource: str
    schema: str
    relation: str


@dataclass(frozen=True, slots=True)
class PostgresRecoveryTarget:
    target_key: str
    database_config_key: str
    database_identity: str
    diagnostic_selector: DiagnosticSelector
    lock_resource_mappings: dict[str, PostgresLockResource]

    def public_summary(self) -> dict[str, object]:
        return {
            "targetKey": self.target_key,
            "automaticRecoveryEnabled": False,
            "approvalRequired": True,
            "verificationCapabilities": [
                "blocker_gone",
                "waiter_progressed",
                "lock_wait_recovered",
                "incident_resolved",
            ],
        }


@dataclass(frozen=True, slots=True)
class ProductionRecoverySettings:
    enabled: bool
    approval_ttl_seconds: int
    compose_targets: dict[str, ComposeRecoveryTarget]
    postgres_targets: dict[str, PostgresRecoveryTarget]
    diagnostic_selectors: dict[tuple[str, str], str]

    def selector_target(self, component: str, mechanism: str) -> str | None:
        return self.diagnostic_selectors.get((component, mechanism))

    @property
    def all_target_keys(self) -> frozenset[str]:
        return frozenset((*self.compose_targets, *self.postgres_targets))


def load_production_recovery_settings(
    config_path: Path | str | None = None,
    *,
    raw_config: Mapping[str, object] | None = None,
    project_root: Path | None = None,
) -> ProductionRecoverySettings:
    """Load explicit recovery allowlists without exposing resolved private values."""

    configuration = (
        raw_config
        if raw_config is not None
        else cast(Mapping[str, object], load_project_config(config_path))
    )
    section_value = configuration.get("productionRecovery")
    if section_value is None:
        return ProductionRecoverySettings(False, 600, {}, {}, {})
    section = _mapping(section_value, "productionRecovery")
    enabled = _boolean(section, "enabled")
    approval_ttl_seconds = _bounded_int(
        section,
        "approvalTtlSeconds",
        minimum=60,
        maximum=3600,
    )
    root = (project_root or _DEFAULT_PROJECT_ROOT).resolve()
    compose_targets = _compose_targets(section.get("composeTargets"), root)
    postgres_targets = _postgres_targets(section.get("postgresTargets"))
    duplicate_target_keys = set(compose_targets).intersection(postgres_targets)
    if duplicate_target_keys:
        raise ProjectConfigurationError("Production recovery target keys must be unique")
    selectors = _selector_index((*compose_targets.values(), *postgres_targets.values()))
    return ProductionRecoverySettings(
        enabled=enabled,
        approval_ttl_seconds=approval_ttl_seconds,
        compose_targets=compose_targets,
        postgres_targets=postgres_targets,
        diagnostic_selectors=selectors,
    )


def _compose_targets(
    value: object,
    project_root: Path,
) -> dict[str, ComposeRecoveryTarget]:
    items = _array(value, "productionRecovery.composeTargets")
    targets: dict[str, ComposeRecoveryTarget] = {}
    for item in items:
        raw = _mapping(item, "productionRecovery compose target")
        target_key = _identifier(raw, "targetKey")
        if target_key in targets:
            raise ProjectConfigurationError("Production recovery target keys must be unique")
        targets[target_key] = ComposeRecoveryTarget(
            target_key=target_key,
            compose_file=_compose_file(raw, project_root),
            service=_identifier(raw, "service"),
            automatic_recovery_enabled=_boolean(raw, "automaticRecoveryEnabled"),
            health_url=_loopback_url(raw, "healthUrl"),
            business_probe_url=_loopback_url(raw, "businessProbeUrl"),
            diagnostic_selector=_diagnostic_selector(raw.get("diagnosticSelector")),
        )
    return targets


def _postgres_targets(value: object) -> dict[str, PostgresRecoveryTarget]:
    items = _array(value, "productionRecovery.postgresTargets")
    targets: dict[str, PostgresRecoveryTarget] = {}
    for item in items:
        raw = _mapping(item, "productionRecovery postgres target")
        target_key = _identifier(raw, "targetKey")
        if target_key in targets:
            raise ProjectConfigurationError("Production recovery target keys must be unique")
        targets[target_key] = PostgresRecoveryTarget(
            target_key=target_key,
            database_config_key=_identifier(raw, "databaseConfigKey"),
            database_identity=_identifier(raw, "databaseIdentity"),
            diagnostic_selector=_diagnostic_selector(raw.get("diagnosticSelector")),
            lock_resource_mappings=_lock_resource_mappings(
                raw.get("lockResourceMappings")
            ),
        )
        if set(targets[target_key].diagnostic_selector.required_evidence_facts) != {
            "InspectPostgresLockGraph.blockerEdgeConfirmed",
            "InspectPostgresLockGraph.blockerRole",
            "InspectPostgresLockGraph.lockedResource",
        }:
            raise ProjectConfigurationError(
                "PostgreSQL diagnosticSelector has invalid relationship facts"
            )
    return targets


def _selector_index(
    targets: tuple[ComposeRecoveryTarget | PostgresRecoveryTarget, ...],
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for target in targets:
        selector = target.diagnostic_selector
        for mechanism in selector.mechanisms:
            key = (selector.component, mechanism)
            if key in result:
                raise ProjectConfigurationError(
                    "Production recovery diagnostic selectors must be unique"
                )
            result[key] = target.target_key
    return result


def _diagnostic_selector(value: object) -> DiagnosticSelector:
    raw = _mapping(value, "diagnosticSelector")
    component = _identifier(raw, "component")
    mechanisms = _unique_strings(
        raw.get("mechanisms"),
        label="diagnosticSelector.mechanisms",
        pattern=_IDENTIFIER,
    )
    required_facts = _unique_strings(
        raw.get("requiredEvidenceFacts"),
        label="diagnosticSelector.requiredEvidenceFacts",
        pattern=_FACT_KEY,
    )
    return DiagnosticSelector(component, mechanisms, required_facts)


def _lock_resource_mappings(value: object) -> dict[str, PostgresLockResource]:
    items = _array(value, "lockResourceMappings")
    if not items:
        raise ProjectConfigurationError(
            "Project config array is invalid: lockResourceMappings"
        )
    result: dict[str, PostgresLockResource] = {}
    physical: set[tuple[str, str]] = set()
    for item in items:
        raw = _mapping(item, "lockResourceMappings item")
        logical = _identifier(raw, "logicalResource")
        schema = _sql_identifier(raw, "schema")
        relation = _sql_identifier(raw, "relation")
        if logical in result or (schema, relation) in physical:
            raise ProjectConfigurationError(
                "Project config array is invalid: lockResourceMappings"
            )
        result[logical] = PostgresLockResource(logical, schema, relation)
        physical.add((schema, relation))
    return result


def _compose_file(raw: Mapping[str, object], project_root: Path) -> Path:
    value = raw.get("composeFile")
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigurationError("Project config string is invalid: composeFile")
    candidate = Path(value.strip())
    if candidate.is_absolute():
        raise ProjectConfigurationError("Project config path is invalid: composeFile")
    resolved = (project_root / candidate).resolve(strict=False)
    if not resolved.is_relative_to(project_root):
        raise ProjectConfigurationError("Project config path is invalid: composeFile")
    return resolved


def _loopback_url(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigurationError(f"Project config URL is invalid: {key}")
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ProjectConfigurationError(f"Project config URL is invalid: {key}") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProjectConfigurationError(f"Project config URL is invalid: {key}")
    return normalized


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProjectConfigurationError(f"Project config section must be an object: {label}")
    return cast(Mapping[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProjectConfigurationError(f"Project config field must be an array: {label}")
    return cast(list[object], value)


def _boolean(raw: Mapping[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ProjectConfigurationError(f"Project config field must be boolean: {key}")
    return value


def _bounded_int(
    raw: Mapping[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProjectConfigurationError(f"Project config integer is out of range: {key}")
    return value


def _identifier(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.strip()):
        raise ProjectConfigurationError(f"Project config identifier is invalid: {key}")
    return value.strip()


def _sql_identifier(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not _SQL_IDENTIFIER.fullmatch(value.strip()):
        raise ProjectConfigurationError(
            "Project config array is invalid: lockResourceMappings"
        )
    return value.strip()


def _unique_strings(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    items = _array(value, label)
    if not items:
        raise ProjectConfigurationError(f"Project config array is invalid: {label}")
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not pattern.fullmatch(item.strip()):
            raise ProjectConfigurationError(f"Project config array is invalid: {label}")
        result.append(item.strip())
    if len(result) != len(set(result)):
        raise ProjectConfigurationError(f"Project config array is invalid: {label}")
    return tuple(result)
