"""Publish the explicit e-commerce quant-service fixture to local Alertmanager."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from super_ai.aiops.fixtures import (
    build_java_ecommerce_alert_payload,
    build_quant_alert_payload,
)
from super_ai.project_config import (
    ProjectConfigurationError,
    project_config_section,
    required_float,
    required_str,
)


def main() -> None:
    args = _build_parser().parse_args()
    config = project_config_section("prometheusAlerts")
    alerts_api = args.alertmanager_url or _local_alertmanager_api(config)
    timeout_seconds = required_float(config, "timeoutSeconds")
    now = datetime.now(timezone.utc)
    base_payload = (
        build_java_ecommerce_alert_payload(now)
        if args.profile == "java-ecommerce"
        else build_quant_alert_payload(now)
    )
    payload = _apply_lifecycle(
        base_payload,
        status=cast(Literal["firing", "resolved"], args.status),
        group_key=args.group_key,
        now=now,
    )
    request = Request(
        alerts_api,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status not in {200, 201, 202}:
                raise SystemExit(f"Alertmanager returned HTTP {response.status}.")
    except (HTTPError, URLError) as exc:
        raise SystemExit("Unable to publish the local Alertmanager fixture.") from exc
    print(
        f"Published {len(payload)} {args.profile} {args.status} alerts "
        "to local Alertmanager."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("java-ecommerce", "quant"),
        default="java-ecommerce",
    )
    parser.add_argument("--status", choices=("firing", "resolved"), default="firing")
    parser.add_argument("--group-key", default=f"agentpy-{uuid4().hex[:12]}")
    parser.add_argument("--alertmanager-url")
    return parser


def _apply_lifecycle(
    payload: list[dict[str, object]],
    *,
    status: Literal["firing", "resolved"],
    group_key: str,
    now: datetime,
) -> list[dict[str, object]]:
    if not group_key.strip():
        raise ValueError("group_key must not be empty")
    resolved_at = _iso8601(now) if status == "resolved" else None
    result: list[dict[str, object]] = []
    for raw_alert in payload:
        raw_labels = raw_alert.get("labels")
        if not isinstance(raw_labels, Mapping):
            raise ValueError("fixture alert labels must be an object")
        typed_labels = cast(Mapping[object, object], raw_labels)
        labels = {str(key): str(value) for key, value in typed_labels.items()}
        labels["run_id"] = group_key
        alert = dict(raw_alert)
        alert["labels"] = labels
        if resolved_at is not None:
            alert["endsAt"] = resolved_at
        result.append(alert)
    return result


def _iso8601(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _local_alertmanager_api(config: object) -> str:
    if not isinstance(config, Mapping):
        raise ProjectConfigurationError("Alert configuration must be an object.")
    typed_config = cast(Mapping[str, object], config)
    sources = typed_config.get("sources")
    if not isinstance(sources, list):
        raise ProjectConfigurationError("Alert sources must be a list.")
    for source in cast(list[object], sources):
        if not isinstance(source, Mapping):
            continue
        typed_source = cast(Mapping[str, object], source)
        if typed_source.get("id") == "local-alertmanager":
            return required_str(typed_source, "alertsApi")
    raise ProjectConfigurationError("Local Alertmanager source is not configured.")


if __name__ == "__main__":
    main()
