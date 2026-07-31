"""Small in-process observability primitives for local application operations."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Literal


@dataclass(frozen=True, slots=True)
class RequestMetricsSnapshot:
    request_count: int
    failure_count: int
    total_latency_ms: float


class RequestMetrics:
    def __init__(self) -> None:
        self._failure_count = 0
        self._lock = Lock()
        self._request_count = 0
        self._total_latency_ms = 0.0

    def record(self, *, latency_ms: float, status_code: int) -> None:
        with self._lock:
            self._request_count += 1
            self._total_latency_ms += latency_ms
            if status_code >= 500:
                self._failure_count += 1

    def snapshot(self) -> RequestMetricsSnapshot:
        with self._lock:
            return RequestMetricsSnapshot(
                request_count=self._request_count,
                failure_count=self._failure_count,
                total_latency_ms=self._total_latency_ms,
            )


class RedisFeatureMetrics:
    """Bounded-label counters for cache and rate-limit runtime behavior."""

    _CACHE_PURPOSES = {"mcp-discovery", "knowledge-retrieval", "unknown"}
    _RATE_ACTIONS = {
        "diagnostic.create",
        "chat.stream",
        "mcp.tool_call",
        "recovery.execute",
        "unknown",
    }

    def __init__(self) -> None:
        self._cache_counts: dict[tuple[str, str], int] = {}
        self._cache_latency: dict[str, tuple[int, float]] = {}
        self._lock = Lock()
        self._rate_counts: dict[tuple[str, str, str], int] = {}

    def record_cache_lookup(
        self,
        purpose: str,
        state: Literal["hit", "miss", "degraded"],
        latency_ms: float,
    ) -> None:
        purpose = purpose if purpose in self._CACHE_PURPOSES else "unknown"
        with self._lock:
            key = (purpose, state)
            self._cache_counts[key] = self._cache_counts.get(key, 0) + 1
            count, total = self._cache_latency.get(purpose, (0, 0.0))
            self._cache_latency[purpose] = (count + 1, total + latency_ms)

    def record_rate_limit(
        self,
        action: str,
        *,
        allowed: bool,
        mode: Literal["redis", "local_fallback", "fail_closed"],
    ) -> None:
        action = action if action in self._RATE_ACTIONS else "unknown"
        outcome = "allow" if allowed else "reject"
        with self._lock:
            key = (action, outcome, mode)
            self._rate_counts[key] = self._rate_counts.get(key, 0) + 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            cache_latency = {
                purpose: round(total / count, 3) if count else 0.0
                for purpose, (count, total) in self._cache_latency.items()
            }
            return {
                "cache": {
                    f"{purpose}:{state}": count
                    for (purpose, state), count in self._cache_counts.items()
                },
                "cacheAverageLatencyMs": cache_latency,
                "rateLimit": {
                    f"{action}:{outcome}:{mode}": count
                    for (action, outcome, mode), count in self._rate_counts.items()
                },
            }
