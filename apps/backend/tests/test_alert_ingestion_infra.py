from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_webhook_has_independent_gateway_budget_and_body_limit() -> None:
    nginx = _read("infra/nginx/default.conf")

    assert "zone=alert_webhook_per_ip:10m rate=5r/s" in nginx
    assert "location ~ ^/aiops/alerts/webhook/alertmanager/" in nginx
    assert "limit_req zone=alert_webhook_per_ip burst=20 nodelay" in nginx
    assert "client_max_body_size 256k" in nginx


def test_alertmanager_groups_lifecycles_and_uses_bearer_secret_file() -> None:
    configuration = _read("infra/alertmanager/alertmanager.yml")

    assert "receiver: agent-py-webhook" in configuration
    assert "group_by: [alertname, service, environment, run_id]" in configuration
    assert "http://nginx/aiops/alerts/webhook/alertmanager/local-alertmanager" in configuration
    assert "send_resolved: true" in configuration
    assert "max_alerts: 50" in configuration
    assert "type: Bearer" in configuration
    assert "credentials_file: /run/secrets/alert_webhook_token" in configuration


def test_compose_mounts_ignored_secret_read_only_without_literal_token() -> None:
    compose = _read("infra/compose.yaml")
    ignore = _read(".gitignore")

    assert "./secrets/alert_webhook_token:/run/secrets/alert_webhook_token:ro" in compose
    assert "depends_on:" in compose
    assert "- nginx" in compose
    assert "infra/secrets/*" in ignore
    assert "AGENTPY_ALERT_WEBHOOK_TOKEN_LOCAL=" not in compose
    assert "Bearer " not in compose

