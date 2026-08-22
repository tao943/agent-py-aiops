from __future__ import annotations

import hashlib
import json

import pytest

from super_ai.alert_ingestion.alertmanager import parse_alertmanager_delivery
from super_ai.alert_ingestion.domain import AlertPayloadError


def _payload() -> dict[str, object]:
    return {
        "version": "4",
        "status": "firing",
        "receiver": "agent-py",
        "groupKey": "secret-group",
        "externalURL": "https://alerts.example.test/path?token=secret",
        "truncatedAlerts": 0,
        "ownerUserId": "attacker",
        "knowledgeBaseId": "kb_attacker",
        "executionPermitted": True,
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighLatency",
                    "service": "order-service",
                    "severity": "critical",
                    "environment": "test",
                    "unknown": "drop-me",
                },
                "annotations": {
                    "summary": "slow requests",
                    "description": "latency above threshold",
                    "runbook_url": "drop-me",
                },
                "startsAt": "2026-08-22T01:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "https://prom.example.test/graph?token=secret",
            }
        ],
    }


def test_parser_keeps_only_allowlisted_fields_and_hashes_sensitive_values() -> None:
    raw_body = json.dumps(_payload()).encode()

    delivery = parse_alertmanager_delivery(raw_body, max_alerts=50)

    serialized = json.dumps(delivery.normalized_payload)
    assert delivery.group_key_hash == hashlib.sha256(b"secret-group").hexdigest()
    assert delivery.payload_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert "secret-group" not in serialized
    assert "attacker" not in serialized
    assert "unknown" not in serialized
    assert "runbook_url" not in serialized
    assert delivery.external_origin == "https://alerts.example.test"
    assert delivery.alerts[0].generator_origin == "https://prom.example.test"


def test_parser_builds_stable_diagnostic_query() -> None:
    delivery = parse_alertmanager_delivery(json.dumps(_payload()).encode(), max_alerts=50)

    assert delivery.query == (
        "Investigate HighLatency affecting order-service. Severity: critical. "
        "Summary: slow requests"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("version", "3"), ("status", "unknown"), ("groupKey", ""), ("alerts", [])],
)
def test_parser_rejects_invalid_required_fields(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(AlertPayloadError):
        parse_alertmanager_delivery(json.dumps(payload).encode(), max_alerts=50)


def test_parser_rejects_invalid_json_and_non_object_alerts() -> None:
    with pytest.raises(AlertPayloadError):
        parse_alertmanager_delivery(b"not-json", max_alerts=50)
    payload = _payload()
    payload["alerts"] = ["not-an-object"]
    with pytest.raises(AlertPayloadError):
        parse_alertmanager_delivery(json.dumps(payload).encode(), max_alerts=50)


def test_parser_rejects_more_than_configured_alert_limit() -> None:
    payload = _payload()
    payload["alerts"] = [payload["alerts"][0]] * 51  # type: ignore[index]

    with pytest.raises(AlertPayloadError):
        parse_alertmanager_delivery(json.dumps(payload).encode(), max_alerts=50)


def test_parser_deterministically_truncates_bounded_fields() -> None:
    payload = _payload()
    alert = payload["alerts"][0]  # type: ignore[index]
    alert["labels"]["instance"] = "i" * 300  # type: ignore[index]
    alert["annotations"]["sop"] = "s" * 2200  # type: ignore[index]

    delivery = parse_alertmanager_delivery(json.dumps(payload).encode(), max_alerts=50)

    assert delivery.truncated is True
    assert delivery.alerts[0].truncated is True
    assert len(delivery.alerts[0].labels["instance"]) == 256
    assert len(delivery.alerts[0].annotations["sop"]) == 2048


@pytest.mark.parametrize("url", ["javascript:alert(1)", "not-a-url", "file:///secret"])
def test_parser_drops_unsafe_url_origins(url: str) -> None:
    payload = _payload()
    payload["externalURL"] = url
    alert = payload["alerts"][0]  # type: ignore[index]
    alert["generatorURL"] = url  # type: ignore[index]

    delivery = parse_alertmanager_delivery(json.dumps(payload).encode(), max_alerts=50)

    assert delivery.external_origin is None
    assert delivery.alerts[0].generator_origin is None
