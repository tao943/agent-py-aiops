"""Application service for Alertmanager incident ingestion."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Protocol

from .config import AlertSourceConfig
from .domain import AlertmanagerDelivery, NormalizedAlert
from .metrics import AlertIngestionMetrics
from .redis_runtime import AlertLease
from .repositories import AlertIngestionRepository, IngestionResult, IngestionWrite

_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
_LIVE_SCENARIO_ROOT = _REPOSITORY_ROOT / "benchmarks" / "agentpy" / "live"
_LIVE_SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"
_LIVE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class AlertLeaseProvider(Protocol):
    async def acquire(self, source_id: str, group_key_hash: str) -> AlertLease: ...


class AlertIngestionService:
    """Filter one delivery, acquire optional lease, and persist its state transition."""

    def __init__(
        self,
        repository: AlertIngestionRepository,
        leases: AlertLeaseProvider,
        metrics: AlertIngestionMetrics,
    ) -> None:
        self._repository = repository
        self._leases = leases
        self._metrics = metrics

    async def ingest(
        self,
        source: AlertSourceConfig,
        delivery: AlertmanagerDelivery,
    ) -> IngestionResult:
        started = monotonic()
        filtered = not any(source.matches(alert.labels) for alert in delivery.alerts)
        lease = await self._leases.acquire(source.id, delivery.group_key_hash)
        try:
            result = await self._repository.apply(
                self.build_write(
                    source,
                    delivery,
                    filtered=filtered,
                    received_at=datetime.now(timezone.utc),
                )
            )
        finally:
            await lease.release()
        latency_ms = (monotonic() - started) * 1000
        self._metrics.record_success(result.disposition, lease.mode, latency_ms=latency_ms)
        return replace(result, redis_mode=lease.mode)

    @staticmethod
    def build_write(
        source: AlertSourceConfig,
        delivery: AlertmanagerDelivery,
        *,
        filtered: bool,
        received_at: datetime,
    ) -> IngestionWrite:
        first = delivery.alerts[0]
        safe_alert = _safe_alert(first)
        task_input_payload = _live_task_input(
            scenario_id=first.labels.get("scenario_id"),
            run_id=first.labels.get("run_id"),
            starts_at=first.starts_at,
            query=delivery.query,
            safe_alert=safe_alert,
        )
        return IngestionWrite(
            owner_user_id=source.owner_user_id,
            source_id=source.id,
            status=delivery.status,
            group_key_hash=delivery.group_key_hash,
            payload_sha256=delivery.payload_sha256,
            normalized_payload=delivery.normalized_payload,
            query=delivery.query,
            safe_alert=safe_alert,
            filtered=filtered,
            received_at=received_at,
            alert_name=first.labels.get("alertname", "unknown alert"),
            service=first.labels.get("service", "unknown service"),
            severity=first.labels.get("severity", "unknown"),
            starts_at=_parse_datetime(first.starts_at),
            scenario_id=first.labels.get("scenario_id"),
            run_id=(
                first.labels.get("run_id")
                if first.labels.get("scenario_id") is not None
                else None
            ),
            task_input_payload=task_input_payload,
        )


def _live_task_input(
    *,
    scenario_id: str | None,
    run_id: str | None,
    starts_at: str | None,
    query: str,
    safe_alert: dict[str, object],
) -> dict[str, object] | None:
    if scenario_id is None:
        return None
    started = _parse_datetime(starts_at)
    if (
        scenario_id != _LIVE_SCENARIO_ID
        or run_id is None
        or not _LIVE_RUN_ID.fullmatch(run_id)
        or ".." in run_id
        or "/" in run_id
        or "\\" in run_id
        or started is None
    ):
        raise ValueError("Live evidence scope is invalid.")
    from super_ai.evaluation.live.diagnostics import build_live_diagnostic_input
    from super_ai.evaluation.live.scenarios import (
        load_live_scenario,
        resolve_live_scenario_directory,
    )

    try:
        scenario_directory = resolve_live_scenario_directory(
            _LIVE_SCENARIO_ROOT,
            scenario_id,
        )
        scenario = load_live_scenario(scenario_directory)
    except ValueError as exc:
        raise ValueError("Live evidence scope is invalid.") from exc
    if scenario.id != _LIVE_SCENARIO_ID:
        raise ValueError("Live evidence scope is invalid.")
    payload = build_live_diagnostic_input(
        scenario,
        workflow_version="evidence-driven-v4",
        investigation_strategy="single",
    )
    payload["query"] = query
    payload["alert"] = safe_alert
    payload["liveEvidenceScope"] = {
        "runId": run_id,
        "scenarioId": scenario_id,
        "incidentId": f"{scenario_id}-{run_id}",
        "fromMs": int((started - timedelta(minutes=5)).timestamp() * 1000),
        "toMs": int((started + timedelta(minutes=30)).timestamp() * 1000),
    }
    return payload


def _safe_alert(alert: NormalizedAlert) -> dict[str, object]:
    return {
        "labels": alert.labels,
        "annotations": alert.annotations,
        "startsAt": alert.starts_at,
        "endsAt": alert.ends_at,
        "generatorURL": alert.generator_origin,
        "truncated": alert.truncated,
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed

