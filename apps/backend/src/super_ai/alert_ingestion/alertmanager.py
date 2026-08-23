"""Safe Alertmanager Webhook v4 parsing and normalization."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from typing import cast
from urllib.parse import urlsplit

from .domain import AlertDeliveryStatus, AlertmanagerDelivery, AlertPayloadError, NormalizedAlert

_LABEL_KEYS = frozenset(
    {
        "alertname",
        "service",
        "severity",
        "environment",
        "cluster",
        "namespace",
        "pod",
        "instance",
        "job",
        "run_id",
        "scenario_id",
        "incident_id",
        "trace_id",
    }
)
_ANNOTATION_KEYS = frozenset({"summary", "description", "sop"})
_LIVE_SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "executionpermitted",
        "groundtruth",
        "oracle",
        "primarycause",
        "readgroundtruth",
        "recoveryaction",
        "recoverytarget",
    }
)


def parse_alertmanager_delivery(
    raw_body: bytes,
    *,
    max_alerts: int,
) -> AlertmanagerDelivery:
    """Validate an Alertmanager v4 body and discard all untrusted fields."""
    raw = _load_object(raw_body)
    if raw.get("version") != "4":
        raise AlertPayloadError("Unsupported Alertmanager Webhook version.")
    status_value = raw.get("status")
    if status_value not in {"firing", "resolved"}:
        raise AlertPayloadError("Invalid Alertmanager delivery status.")
    group_key = raw.get("groupKey")
    if not isinstance(group_key, str) or not group_key.strip():
        raise AlertPayloadError("Alertmanager groupKey is required.")
    raw_alerts = raw.get("alerts")
    if not isinstance(raw_alerts, list):
        raise AlertPayloadError("Alertmanager alerts count is invalid.")
    typed_alerts = cast(list[object], raw_alerts)
    if not 1 <= len(typed_alerts) <= max_alerts:
        raise AlertPayloadError("Alertmanager alerts count is invalid.")
    alerts = tuple(_normalize_alert(value) for value in typed_alerts)
    _validate_group_correlation(alerts)
    external_origin = _safe_origin(raw.get("externalURL"))
    receiver, receiver_truncated = _bounded(raw.get("receiver"), 256)
    truncated_alerts = _non_negative_int(raw.get("truncatedAlerts", 0))
    status = cast(AlertDeliveryStatus, status_value)
    normalized_alerts = [_alert_payload(alert) for alert in alerts]
    normalized: dict[str, object] = {
        "version": "4",
        "status": status,
        "receiver": receiver,
        "externalURL": external_origin,
        "truncatedAlerts": truncated_alerts,
        "alerts": normalized_alerts,
    }
    first = alerts[0]
    return AlertmanagerDelivery(
        status=status,
        receiver=receiver,
        group_key_hash=sha256(group_key.encode("utf-8")).hexdigest(),
        payload_sha256=sha256(raw_body).hexdigest(),
        external_origin=external_origin,
        truncated_alerts=truncated_alerts,
        alerts=alerts,
        normalized_payload=normalized,
        query=_diagnostic_query(first),
        truncated=receiver_truncated or any(alert.truncated for alert in alerts),
    )


def _load_object(raw_body: bytes) -> Mapping[str, object]:
    try:
        value: object = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlertPayloadError("Alertmanager body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise AlertPayloadError("Alertmanager body must be an object.")
    return cast(Mapping[str, object], value)


def _normalize_alert(value: object) -> NormalizedAlert:
    if not isinstance(value, dict):
        raise AlertPayloadError("Each Alertmanager alert must be an object.")
    raw = cast(Mapping[str, object], value)
    _reject_authority_fields(raw.get("labels"))
    _reject_authority_fields(raw.get("annotations"))
    labels, label_truncated = _allowlisted_map(raw.get("labels"), _LABEL_KEYS, 256)
    annotations, annotation_truncated = _allowlisted_map(
        raw.get("annotations"), _ANNOTATION_KEYS, 2048
    )
    starts_at, starts_truncated = _optional_bounded(raw.get("startsAt"), 256)
    ends_at, ends_truncated = _optional_bounded(raw.get("endsAt"), 256)
    _validate_live_correlation(labels)
    return NormalizedAlert(
        labels=labels,
        annotations=annotations,
        starts_at=starts_at,
        ends_at=ends_at,
        generator_origin=_safe_origin(raw.get("generatorURL")),
        truncated=(label_truncated or annotation_truncated or starts_truncated or ends_truncated),
    )


def _reject_authority_fields(value: object) -> None:
    if not isinstance(value, dict):
        return
    mapping = cast(Mapping[object, object], value)
    for key in mapping:
        if not isinstance(key, str):
            continue
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        if normalized in _FORBIDDEN_AUTHORITY_KEYS:
            raise AlertPayloadError("Alertmanager labels cannot grant diagnostic authority.")


def _validate_live_correlation(labels: Mapping[str, str]) -> None:
    scenario_id = labels.get("scenario_id")
    if scenario_id is None:
        return
    if scenario_id != _LIVE_SCENARIO_ID:
        raise AlertPayloadError("Alertmanager Live scenario is not allowlisted.")
    run_id = labels.get("run_id")
    if (
        run_id is None
        or not _RUN_ID_RE.fullmatch(run_id)
        or ".." in run_id
        or "/" in run_id
        or "\\" in run_id
    ):
        raise AlertPayloadError("Alertmanager Live run ID is invalid.")


def _validate_group_correlation(alerts: tuple[NormalizedAlert, ...]) -> None:
    correlations = {
        (alert.labels.get("scenario_id"), alert.labels.get("run_id"))
        for alert in alerts
    }
    if len(correlations) != 1:
        raise AlertPayloadError("Alertmanager group has mixed Live correlation labels.")


def _allowlisted_map(
    value: object,
    allowed_keys: frozenset[str],
    limit: int,
) -> tuple[dict[str, str], bool]:
    if not isinstance(value, dict):
        return {}, False
    mapping = cast(Mapping[str, object], value)
    result: dict[str, str] = {}
    truncated = False
    for key in sorted(allowed_keys):
        item = mapping.get(key)
        if not isinstance(item, str):
            continue
        result[key], item_truncated = _bounded(item, limit)
        truncated = truncated or item_truncated
    return result, truncated


def _bounded(value: object, limit: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    return value[:limit], len(value) > limit


def _optional_bounded(value: object, limit: int) -> tuple[str | None, bool]:
    if not isinstance(value, str) or not value:
        return None, False
    bounded, truncated = _bounded(value, limit)
    return bounded, truncated


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AlertPayloadError("truncatedAlerts must be a non-negative integer.")
    return value


def _safe_origin(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}{f':{port}' if port is not None else ''}"


def _alert_payload(alert: NormalizedAlert) -> dict[str, object]:
    return {
        "labels": alert.labels,
        "annotations": alert.annotations,
        "startsAt": alert.starts_at,
        "endsAt": alert.ends_at,
        "generatorURL": alert.generator_origin,
        "truncated": alert.truncated,
    }


def _diagnostic_query(alert: NormalizedAlert) -> str:
    labels = alert.labels
    annotations = alert.annotations
    return (
        f"Investigate {labels.get('alertname', 'unknown alert')} affecting "
        f"{labels.get('service', 'unknown service')}. Severity: "
        f"{labels.get('severity', 'unknown')}. Summary: "
        f"{annotations.get('summary', 'not provided')}"
    )
