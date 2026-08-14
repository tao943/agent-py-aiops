"""Run-scoped Tencent CLS evidence preparation for Docker Live evaluation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from importlib import import_module
from time import monotonic
from typing import Any, Protocol, cast

from super_ai.evaluation.live.domain import (
    LiveClsScope,
    LiveEvidenceContext,
    LiveEvidenceReadiness,
    LiveFaultObservation,
    LiveInfrastructureError,
    LiveRunIdentity,
    LiveScenario,
)


class ClsUploadBoundary(Protocol):
    async def put(
        self,
        records: Sequence[Mapping[str, str]],
        *,
        filename: str,
    ) -> int: ...


class ClsSearchBoundary(Protocol):
    async def search(self, scope: LiveClsScope) -> Sequence[Mapping[str, object]]: ...


def build_live_cls_records(
    *,
    run_id: str,
    scenario_id: str,
    incident_id: str,
    now: datetime,
) -> tuple[dict[str, str], ...]:
    """Build safe business symptoms without exposing the evaluator-only cause."""
    timestamp = now.astimezone(timezone.utc).isoformat()
    common = {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "incident_id": incident_id,
        "service": "order-service",
        "environment": "live-eval",
        "trace_id": f"{run_id}-order-status-update",
        "component": "orders-api",
        "timestamp": timestamp,
    }
    return (
        {
            **common,
            "event": "order_update_received",
            "level": "INFO",
            "message": "Order status update request was accepted.",
        },
        {
            **common,
            "event": "order_update_timeout",
            "level": "ERROR",
            "message": "Order status update exceeded the bounded database response timeout.",
        },
        {
            **common,
            "event": "benchmark_alert_emitted",
            "level": "WARN",
            "message": "Order update latency alert was emitted for investigation.",
        },
    )


def build_cls_log_group(
    records: Sequence[Mapping[str, str]],
    *,
    filename: str,
    source: str = "127.0.0.1",
) -> Any:
    """Convert safe string fields into the existing CLS protobuf contract."""
    protobuf = cast(Any, import_module("tencentcloud.log.cls_pb2"))
    groups = protobuf.LogGroupList()
    group = groups.logGroupList.add()
    group.filename = filename
    group.source = source
    log_time = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    for fields in records:
        log = group.logs.add()
        log.time = log_time
        for key, value in fields.items():
            content = log.contents.add()
            content.key = key
            content.value = value
    return groups


def put_cls_records(
    *,
    endpoint: str,
    topic_id: str,
    secret_id: str,
    secret_key: str,
    records: Sequence[Mapping[str, str]],
    filename: str,
) -> None:
    """Upload one bounded group using the project's existing official SDK."""
    logclient = cast(Any, import_module("tencentcloud.log.logclient"))
    client = logclient.LogClient(endpoint, secret_id, secret_key)
    client.put_log_raw(topic_id, build_cls_log_group(records, filename=filename))


class LiveClsLogUploader:
    """Async boundary around the synchronous official CLS SDK."""

    __slots__ = ("_endpoint", "_secret_id", "_secret_key", "_topic_id")

    def __init__(
        self,
        *,
        endpoint: str,
        topic_id: str,
        secret_id: str,
        secret_key: str,
    ) -> None:
        self._endpoint = endpoint
        self._topic_id = topic_id
        self._secret_id = secret_id
        self._secret_key = secret_key

    async def put(
        self,
        records: Sequence[Mapping[str, str]],
        *,
        filename: str,
    ) -> int:
        await asyncio.to_thread(
            put_cls_records,
            endpoint=self._endpoint,
            topic_id=self._topic_id,
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            records=records,
            filename=filename,
        )
        return len(records)


class LiveClsEvidencePreparer:
    """Upload and poll until every run-scoped Live log is searchable."""

    def __init__(
        self,
        *,
        region: str,
        topic_id: str,
        uploader: ClsUploadBoundary,
        searcher: ClsSearchBoundary,
        timeout_seconds: float = 90.0,
        poll_interval_seconds: float = 2.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("CLS readiness timing must be positive.")
        self._region = region
        self._topic_id = topic_id
        self._uploader = uploader
        self._searcher = searcher
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep

    async def prepare(
        self,
        *,
        identity: LiveRunIdentity,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
    ) -> LiveEvidenceContext:
        del observation
        incident_id = f"{scenario.id}-{identity.run_id}"
        now = self._now()
        records = build_live_cls_records(
            run_id=identity.run_id,
            scenario_id=scenario.id,
            incident_id=incident_id,
            now=now,
        )
        try:
            uploaded_count = await self._uploader.put(
                records,
                filename="agentpy-live-postgres.log",
            )
        except asyncio.CancelledError:
            raise
        except LiveInfrastructureError:
            raise
        except Exception as exc:
            raise LiveInfrastructureError("cls_upload_failed") from exc
        if uploaded_count != len(records):
            raise LiveInfrastructureError("cls_upload_incomplete")

        uploaded_at_ms = int(now.timestamp() * 1_000)
        scope = LiveClsScope(
            region=self._region,
            topic_id=self._topic_id,
            from_ms=uploaded_at_ms - 5_000,
            to_ms=uploaded_at_ms + int(self._timeout_seconds * 1_000) + 5_000,
            run_id=identity.run_id,
            scenario_id=scenario.id,
            incident_id=incident_id,
        )
        deadline = self._monotonic() + self._timeout_seconds
        attempts = 0
        while True:
            attempts += 1
            try:
                visible = await self._searcher.search(scope)
            except asyncio.CancelledError:
                raise
            except LiveInfrastructureError:
                raise
            except Exception as exc:
                raise LiveInfrastructureError("cls_mcp_unavailable") from exc
            matching = _matching_records(visible, scope)
            if len(matching) >= len(records):
                searchable_at_ms = int(self._now().timestamp() * 1_000)
                return LiveEvidenceContext(
                    source="cls",
                    incident_id=incident_id,
                    cls_scope=scope,
                    readiness=LiveEvidenceReadiness(
                        expected_log_count=len(records),
                        indexed_log_count=len(matching),
                        attempts=attempts,
                        uploaded_at_ms=uploaded_at_ms,
                        searchable_at_ms=searchable_at_ms,
                    ),
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LiveInfrastructureError("cls_index_timeout")
            await self._sleep(min(self._poll_interval_seconds, remaining))


def _matching_records(
    records: Sequence[Mapping[str, object]],
    scope: LiveClsScope,
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        record
        for record in records
        if record.get("run_id") == scope.run_id
        and record.get("scenario_id") == scope.scenario_id
        and record.get("incident_id") == scope.incident_id
    )
