from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from super_ai.alert_ingestion.config import AlertIngestionSettings, AlertSourceConfig
from super_ai.alert_ingestion.metrics import AlertIngestionMetrics
from super_ai.alert_ingestion.repositories import AlertPersistenceError, IngestionResult
from super_ai.alert_ingestion.routes import create_alert_ingestion_router


def _body() -> bytes:
    return json.dumps(
        {
            "version": "4",
            "status": "firing",
            "receiver": "agent-py",
            "groupKey": "private-group",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "HighLatency",
                        "service": "order-service",
                        "severity": "critical",
                        "environment": "test",
                    },
                    "annotations": {"summary": "slow requests"},
                }
            ],
        }
    ).encode()


class Service:
    def __init__(
        self,
        *,
        failure: bool = False,
        result: IngestionResult | None = None,
    ) -> None:
        self.calls = 0
        self.failure = failure
        self.result = result or IngestionResult(
            "incident_created",
            "incident-one",
            "diagnostic-one",
            "job-one",
            "primary",
        )

    async def ingest(self, source: AlertSourceConfig, delivery: object) -> IngestionResult:
        del source, delivery
        self.calls += 1
        if self.failure:
            raise AlertPersistenceError("must not leak")
        return self.result


class Runtime:
    def __init__(self, *, failure: bool = False) -> None:
        self.start_count = 0
        self.failure = failure

    async def start(self) -> None:
        self.start_count += 1
        if self.failure:
            raise RuntimeError("worker unavailable")


def _app(
    *,
    service: Service | None = None,
    runtime: Runtime | None = None,
) -> tuple[FastAPI, Service, Runtime, AlertIngestionMetrics]:
    settings = AlertIngestionSettings(
        enabled=True,
        max_body_bytes=262144,
        max_alerts_per_delivery=50,
        redis_lease_milliseconds=2000,
        sources={
            "local-alertmanager": AlertSourceConfig(
                id="local-alertmanager",
                owner_user_id="owner",
                knowledge_base_id="kb_owner",
                token="correct-token-" + "x" * 32,
                allowed_labels={"environment": frozenset({"test"})},
            )
        },
    )
    composed_service = service or Service()
    composed_runtime = runtime or Runtime()
    metrics = AlertIngestionMetrics()
    app = FastAPI()
    app.include_router(
        create_alert_ingestion_router(
            settings,
            composed_service,
            composed_runtime,
            metrics,
        )
    )
    return app, composed_service, composed_runtime, metrics


async def _post(
    app: FastAPI,
    content: bytes | AsyncIterator[bytes],
    *,
    source_id: str = "local-alertmanager",
    authorization: str | None = "Bearer correct-token-" + "x" * 32,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = {"content-type": "application/json", **(headers or {})}
    if authorization is not None:
        request_headers["authorization"] = authorization
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/aiops/alerts/webhook/alertmanager/{source_id}",
            content=content,
            headers=request_headers,
        )


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer wrong", "Bearer"])
async def test_invalid_authorization_is_401_without_persistence(
    authorization: str | None,
) -> None:
    app, service, runtime, metrics = _app()

    response = await _post(app, _body(), authorization=authorization)

    assert response.status_code == 401
    assert service.calls == 0
    assert runtime.start_count == 0
    assert metrics.snapshot()["webhookReceivedTotal"] == 1
    assert metrics.snapshot()["ingestionFailedTotal"] == 0


async def test_unknown_source_is_404_without_revealing_token_state() -> None:
    app, service, _, metrics = _app()

    response = await _post(app, _body(), source_id="unknown", authorization="Bearer wrong")

    assert response.status_code == 404
    assert service.calls == 0
    assert metrics.snapshot()["ingestionFailedTotal"] == 0


async def test_declared_and_actual_stream_body_limits_are_413() -> None:
    app, service, _, metrics = _app()
    declared = await _post(app, b"{}", headers={"content-length": "262145"})

    async def oversized() -> AsyncIterator[bytes]:
        yield b"x" * 200_000
        yield b"y" * 70_000

    streamed = await _post(app, oversized())

    assert declared.status_code == streamed.status_code == 413
    assert service.calls == 0
    assert metrics.snapshot()["ingestionFailedTotal"] == 2


@pytest.mark.parametrize("content", [b"not-json", b"{}"])
async def test_invalid_json_or_schema_is_422(content: bytes) -> None:
    app, service, _, metrics = _app()

    response = await _post(app, content)

    assert response.status_code == 422
    assert service.calls == 0
    assert metrics.snapshot()["ingestionFailedTotal"] == 1


async def test_database_failure_is_retryable_503_without_runtime_wakeup() -> None:
    app, service, runtime, metrics = _app(service=Service(failure=True))

    response = await _post(app, _body())

    assert response.status_code == 503
    assert service.calls == 1
    assert runtime.start_count == 0
    assert metrics.snapshot()["ingestionFailedTotal"] == 1
    assert "must not leak" not in response.text


async def test_runtime_wakeup_failure_remains_safe_202_after_commit() -> None:
    app, service, runtime, metrics = _app(runtime=Runtime(failure=True))

    response = await _post(app, _body())

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "incidentId": "incident-one",
        "diagnosticTaskId": "diagnostic-one",
        "duplicate": False,
        "filtered": False,
        "redisMode": "primary",
    }
    assert service.calls == 1
    assert runtime.start_count == 1
    assert metrics.snapshot()["ingestionFailedTotal"] == 0
    assert "private-group" not in response.text


@pytest.mark.parametrize(
    ("result", "expected_duplicate", "expected_filtered", "expected_wakeup"),
    [
        (
            IngestionResult(
                "duplicate_updated", "incident-one", "diagnostic-one", "job-one", "contended"
            ),
            True,
            False,
            0,
        ),
        (IngestionResult("filtered", None, None, None, "primary"), False, True, 0),
        (
            IngestionResult(
                "incident_resolved", "incident-one", "diagnostic-one", "job-one", "primary"
            ),
            False,
            False,
            0,
        ),
        (IngestionResult("orphan_resolved", None, None, None, "degraded"), False, False, 0),
    ],
)
async def test_non_creating_dispositions_return_safe_202_without_runtime_wakeup(
    result: IngestionResult,
    expected_duplicate: bool,
    expected_filtered: bool,
    expected_wakeup: int,
) -> None:
    app, _, runtime, _ = _app(service=Service(result=result))

    response = await _post(app, _body())

    assert response.status_code == 202
    assert response.json()["duplicate"] is expected_duplicate
    assert response.json()["filtered"] is expected_filtered
    assert response.json()["redisMode"] == result.redis_mode
    assert runtime.start_count == expected_wakeup
