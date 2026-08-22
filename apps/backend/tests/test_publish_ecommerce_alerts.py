from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from super_ai.aiops.fixtures import build_quant_alert_payload

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_ecommerce_quant_alert.py"
SPEC = importlib.util.spec_from_file_location("publish_ecommerce_quant_alert", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_apply_lifecycle = cast(Any, MODULE)._apply_lifecycle
_build_parser = cast(Any, MODULE)._build_parser


def test_firing_and_resolved_preserve_identical_grouping_labels() -> None:
    now = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)
    firing = _apply_lifecycle(
        build_quant_alert_payload(now), status="firing", group_key="demo", now=now
    )
    resolved = _apply_lifecycle(
        build_quant_alert_payload(now), status="resolved", group_key="demo", now=now
    )
    grouping = ("alertname", "service", "environment", "run_id")

    assert {key: firing[0]["labels"][key] for key in grouping} == {  # type: ignore[index]
        key: resolved[0]["labels"][key] for key in grouping  # type: ignore[index]
    }
    assert firing[0]["labels"]["run_id"] == "demo"  # type: ignore[index]
    assert resolved[0]["endsAt"] == "2026-08-22T01:00:00Z"


def test_changing_developer_group_key_changes_only_run_id() -> None:
    now = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)
    first = _apply_lifecycle(
        build_quant_alert_payload(now), status="firing", group_key="first", now=now
    )[0]
    second = _apply_lifecycle(
        build_quant_alert_payload(now), status="firing", group_key="second", now=now
    )[0]

    assert first["labels"]["run_id"] == "first"  # type: ignore[index]
    assert second["labels"]["run_id"] == "second"  # type: ignore[index]
    first_labels = dict(first["labels"])  # type: ignore[arg-type]
    second_labels = dict(second["labels"])  # type: ignore[arg-type]
    first_labels.pop("run_id")
    second_labels.pop("run_id")
    assert first_labels == second_labels


def test_cli_accepts_status_group_key_and_alertmanager_override() -> None:
    args = _build_parser().parse_args(
        [
            "--profile",
            "quant",
            "--status",
            "resolved",
            "--group-key",
            "demo",
            "--alertmanager-url",
            "http://127.0.0.1:9093/api/v2/alerts",
        ]
    )

    assert args.status == "resolved"
    assert args.group_key == "demo"
    assert args.alertmanager_url.endswith("/api/v2/alerts")


def test_cli_rejects_unknown_status() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--status", "unknown"])
