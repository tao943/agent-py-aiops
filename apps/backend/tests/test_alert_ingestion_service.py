from __future__ import annotations

from datetime import datetime, timezone

import pytest

from super_ai.alert_ingestion.config import AlertSourceConfig
from super_ai.alert_ingestion.domain import AlertmanagerDelivery, NormalizedAlert
from super_ai.alert_ingestion.metrics import AlertIngestionMetrics
from super_ai.alert_ingestion.redis_runtime import AlertLease
from super_ai.alert_ingestion.repositories import IngestionResult, IngestionWrite, RedisMode
from super_ai.alert_ingestion.service import AlertIngestionService


def _delivery(
    *,
    environment: str = "test",
    live_correlation: bool = False,
) -> AlertmanagerDelivery:
    correlation = (
        {
            "scenario_id": "APY-LIVE-ORDER-POOL-LEAK-001",
            "run_id": "closure-001",
        }
        if live_correlation
        else {}
    )
    alert = NormalizedAlert(
        labels={
            "alertname": "HighLatency",
            "service": "order-service",
            "severity": "critical",
            "environment": environment,
            **correlation,
        },
        annotations={"summary": "slow requests"},
        starts_at="2026-08-22T01:00:00Z",
        ends_at=None,
        generator_origin=None,
        truncated=False,
    )
    safe_alert = {
        "labels": alert.labels,
        "annotations": alert.annotations,
        "startsAt": alert.starts_at,
        "endsAt": None,
        "generatorURL": None,
        "truncated": False,
    }
    return AlertmanagerDelivery(
        status="firing",
        receiver="agent-py",
        group_key_hash="a" * 64,
        payload_sha256="b" * 64,
        external_origin=None,
        truncated_alerts=0,
        alerts=(alert,),
        normalized_payload={"version": "4", "status": "firing", "alerts": [safe_alert]},
        query="Investigate HighLatency affecting order-service.",
        truncated=False,
    )


def _source() -> AlertSourceConfig:
    return AlertSourceConfig(
        id="local-alertmanager",
        owner_user_id="owner",
        knowledge_base_id="kb_owner",
        token="x" * 32,
        allowed_labels={
            "environment": frozenset({"test"}),
            "severity": frozenset({"critical"}),
        },
    )


class RecordingRepository:
    def __init__(self, disposition: str = "incident_created") -> None:
        self.disposition = disposition
        self.writes: list[IngestionWrite] = []

    async def apply(self, write: IngestionWrite) -> IngestionResult:
        self.writes.append(write)
        if self.disposition == "filtered":
            return IngestionResult("filtered", None, None, None)
        return IngestionResult(
            "incident_created",
            "incident-one",
            "diagnostic-one",
            "job-one",
        )


class LeaseProvider:
    def __init__(self, mode: RedisMode) -> None:
        self.mode: RedisMode = mode
        self.release_count = 0

    async def acquire(self, source_id: str, group_key_hash: str) -> AlertLease:
        assert source_id == "local-alertmanager"
        assert group_key_hash == "a" * 64

        async def release() -> bool:
            self.release_count += 1
            return True

        return AlertLease(self.mode, release)


async def test_filtered_delivery_is_audited_without_authority_from_payload() -> None:
    repository = RecordingRepository("filtered")
    leases = LeaseProvider("primary")
    metrics = AlertIngestionMetrics()
    service = AlertIngestionService(repository, leases, metrics)

    result = await service.ingest(_source(), _delivery(environment="dev"))

    assert result.disposition == "filtered"
    assert repository.writes[0].filtered is True
    assert repository.writes[0].owner_user_id == "owner"
    assert leases.release_count == 1
    assert metrics.snapshot()["filteredTotal"] == 1


async def test_redis_failure_still_calls_repository_and_marks_degraded() -> None:
    repository = RecordingRepository()
    leases = LeaseProvider("degraded")
    metrics = AlertIngestionMetrics()
    service = AlertIngestionService(repository, leases, metrics)

    result = await service.ingest(_source(), _delivery())

    assert result.redis_mode == "degraded"
    assert len(repository.writes) == 1
    assert leases.release_count == 1
    assert metrics.snapshot()["redisDegradedTotal"] == 1


def test_metrics_have_exact_request_failure_and_latency_semantics() -> None:
    metrics = AlertIngestionMetrics()
    metrics.record_received()
    metrics.record_received()
    metrics.record_request_failure(latency_ms=4.0)
    metrics.record_success("incident_created", "primary", latency_ms=6.0)
    metrics.record_success("duplicate_updated", "contended", latency_ms=2.0)
    metrics.record_success("incident_resolved", "primary", latency_ms=3.0)
    metrics.record_success("orphan_resolved", "primary", latency_ms=1.0)
    metrics.record_success("filtered", "degraded", latency_ms=5.0)

    snapshot = metrics.snapshot()

    assert snapshot["webhookReceivedTotal"] == 2
    assert snapshot["ingestionFailedTotal"] == 1
    assert snapshot["incidentCreatedTotal"] == 1
    assert snapshot["duplicateSuppressedTotal"] == 1
    assert snapshot["resolvedTotal"] == 1
    assert snapshot["orphanResolvedTotal"] == 1
    assert snapshot["filteredTotal"] == 1
    assert snapshot["redisDegradedTotal"] == 1
    assert snapshot["ingestionLatencyMs"] == {"count": 6, "sum": 21.0, "max": 6.0}
    assert snapshot["diagnosisEnqueueLatencyMs"] == {"count": 1, "sum": 6.0, "max": 6.0}


async def test_lease_is_released_when_repository_fails() -> None:
    class FailingRepository:
        async def apply(self, write: IngestionWrite) -> IngestionResult:
            del write
            raise RuntimeError("database failed")

    leases = LeaseProvider("primary")
    service = AlertIngestionService(FailingRepository(), leases, AlertIngestionMetrics())

    with pytest.raises(RuntimeError, match="database failed"):
        await service.ingest(_source(), _delivery())

    assert leases.release_count == 1


def test_service_parses_start_time_and_builds_only_safe_alert_input() -> None:
    write = AlertIngestionService.build_write(
        _source(),
        _delivery(),
        filtered=False,
        received_at=datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc),
    )

    assert write.starts_at == datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)
    assert write.safe_alert["labels"] == _delivery().alerts[0].labels
    assert "ownerUserId" not in write.safe_alert
    assert "executionPermitted" not in write.safe_alert


def test_service_builds_exact_live_correlation_without_webhook_authority() -> None:
    write = AlertIngestionService.build_write(
        _source(),
        _delivery(live_correlation=True),
        filtered=False,
        received_at=datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc),
    )

    assert write.scenario_id == "APY-LIVE-ORDER-POOL-LEAK-001"
    assert write.run_id == "closure-001"
