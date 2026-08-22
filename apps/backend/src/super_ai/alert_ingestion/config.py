"""Configuration for source-bound alert ingestion."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from super_ai.project_config import ProjectConfigurationError, load_project_config

_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AlertSourceConfig:
    id: str
    owner_user_id: str
    knowledge_base_id: str
    token: str
    allowed_labels: dict[str, frozenset[str]]

    def matches(self, labels: Mapping[str, str]) -> bool:
        return all(labels.get(key) in values for key, values in self.allowed_labels.items())


@dataclass(frozen=True, slots=True)
class AlertIngestionSettings:
    enabled: bool
    max_body_bytes: int
    max_alerts_per_delivery: int
    redis_lease_milliseconds: int
    sources: dict[str, AlertSourceConfig]


def load_alert_ingestion_settings(
    config_path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    raw_config: Mapping[str, object] | None = None,
) -> AlertIngestionSettings:
    """Load safe ingestion settings without ever returning token environment names."""
    configuration = raw_config if raw_config is not None else load_project_config(config_path)
    section_value = configuration.get("alertIngestion")
    if section_value is None:
        return AlertIngestionSettings(False, 262144, 50, 2000, {})
    if not isinstance(section_value, dict):
        raise ProjectConfigurationError("Project config section must be an object: alertIngestion")
    section = cast(Mapping[str, object], section_value)
    enabled = _boolean(section, "enabled")
    max_body_bytes = _bounded_int(section, "maxBodyBytes", minimum=1, maximum=10_485_760)
    max_alerts = _bounded_int(section, "maxAlertsPerDelivery", minimum=1, maximum=50)
    lease_ms = _bounded_int(section, "redisLeaseMilliseconds", minimum=100, maximum=30_000)
    sources_value = section.get("sources")
    if not isinstance(sources_value, list):
        raise ProjectConfigurationError("alertIngestion.sources must be an array")
    raw_sources = cast(list[object], sources_value)
    environment = environ if environ is not None else os.environ
    sources: dict[str, AlertSourceConfig] = {}
    seen_ids: set[str] = set()
    for source_value in raw_sources:
        source = _mapping(source_value, "alertIngestion source")
        source_id = _non_empty_string(source, "id", maximum=120)
        if not _SOURCE_ID.fullmatch(source_id):
            raise ProjectConfigurationError("Alert ingestion source id is invalid")
        if source_id in seen_ids:
            raise ProjectConfigurationError("Alert ingestion source ids must be unique")
        seen_ids.add(source_id)
        source_enabled = _boolean(source, "enabled")
        if not enabled or not source_enabled:
            continue
        owner = _non_empty_string(source, "ownerUserId", maximum=80)
        knowledge_base = _non_empty_string(source, "knowledgeBaseId", maximum=160)
        if knowledge_base != f"kb_{owner}":
            raise ProjectConfigurationError("Alert source knowledge base must match its owner")
        environment_name = _non_empty_string(source, "tokenEnvironmentVariable", maximum=160)
        if not _ENVIRONMENT_NAME.fullmatch(environment_name):
            raise ProjectConfigurationError("Alert source token environment name is invalid")
        token = environment.get(environment_name, "")
        if len(token) < 32:
            raise ProjectConfigurationError("Alert source token is missing or too short")
        allowed_labels = _allowed_labels(source.get("allowedLabels"))
        sources[source_id] = AlertSourceConfig(
            id=source_id,
            owner_user_id=owner,
            knowledge_base_id=knowledge_base,
            token=token,
            allowed_labels=allowed_labels,
        )
    return AlertIngestionSettings(enabled, max_body_bytes, max_alerts, lease_ms, sources)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProjectConfigurationError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _boolean(mapping: Mapping[str, object], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ProjectConfigurationError(f"Project config field must be boolean: {key}")
    return value


def _bounded_int(
    mapping: Mapping[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProjectConfigurationError(f"Project config integer is out of range: {key}")
    return value


def _non_empty_string(
    mapping: Mapping[str, object],
    key: str,
    *,
    maximum: int,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ProjectConfigurationError(f"Project config string is invalid: {key}")
    return value.strip()


def _allowed_labels(value: object) -> dict[str, frozenset[str]]:
    raw = _mapping(value, "allowedLabels")
    if not raw or len(raw) > 20:
        raise ProjectConfigurationError("allowedLabels must contain between 1 and 20 keys")
    result: dict[str, frozenset[str]] = {}
    for key, values in raw.items():
        if not key or len(key) > 64 or not isinstance(values, list) or not values:
            raise ProjectConfigurationError("allowedLabels entry is invalid")
        typed_values = cast(list[object], values)
        if len(typed_values) > 20 or any(
            not isinstance(item, str) or not item or len(item) > 256 for item in typed_values
        ):
            raise ProjectConfigurationError("allowedLabels values are invalid")
        result[key] = frozenset(cast(list[str], typed_values))
    return result
