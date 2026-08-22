"""Bounded in-process metrics for alert ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .repositories import AlertDisposition, RedisMode


@dataclass(slots=True)
class _Latency:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0

    def add(self, value: float) -> None:
        bounded = max(0.0, value)
        self.count += 1
        self.total += bounded
        self.maximum = max(self.maximum, bounded)

    def payload(self) -> dict[str, int | float]:
        return {
            "count": self.count,
            "sum": round(self.total, 3),
            "max": round(self.maximum, 3),
        }


class AlertIngestionMetrics:
    """Fixed-cardinality counters and latency aggregates."""

    def __init__(self) -> None:
        self._counts = {
            "webhookReceivedTotal": 0,
            "incidentCreatedTotal": 0,
            "duplicateSuppressedTotal": 0,
            "filteredTotal": 0,
            "resolvedTotal": 0,
            "orphanResolvedTotal": 0,
            "ingestionFailedTotal": 0,
            "redisDegradedTotal": 0,
        }
        self._ingestion_latency = _Latency()
        self._enqueue_latency = _Latency()
        self._lock = Lock()

    def record_received(self) -> None:
        with self._lock:
            self._counts["webhookReceivedTotal"] += 1

    def record_request_failure(self, *, latency_ms: float) -> None:
        with self._lock:
            self._counts["ingestionFailedTotal"] += 1
            self._ingestion_latency.add(latency_ms)

    def record_success(
        self,
        disposition: AlertDisposition,
        redis_mode: RedisMode,
        *,
        latency_ms: float,
    ) -> None:
        counter = {
            "incident_created": "incidentCreatedTotal",
            "duplicate_updated": "duplicateSuppressedTotal",
            "filtered": "filteredTotal",
            "incident_resolved": "resolvedTotal",
            "orphan_resolved": "orphanResolvedTotal",
        }[disposition]
        with self._lock:
            self._counts[counter] += 1
            if redis_mode == "degraded":
                self._counts["redisDegradedTotal"] += 1
            self._ingestion_latency.add(latency_ms)
            if disposition == "incident_created":
                self._enqueue_latency.add(latency_ms)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                **self._counts,
                "ingestionLatencyMs": self._ingestion_latency.payload(),
                "diagnosisEnqueueLatencyMs": self._enqueue_latency.payload(),
            }

