"""FastAPI router for authenticated Alertmanager Webhook v4 ingestion."""

from __future__ import annotations

import hmac
import logging
from time import monotonic
from typing import Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from super_ai.observability import emit_event

from .alertmanager import parse_alertmanager_delivery
from .config import AlertIngestionSettings, AlertSourceConfig
from .domain import AlertmanagerDelivery, AlertPayloadError
from .metrics import AlertIngestionMetrics
from .repositories import AlertPersistenceError, IngestionResult

logger = logging.getLogger(__name__)


class AlertIngestionHandler(Protocol):
    async def ingest(
        self,
        source: AlertSourceConfig,
        delivery: AlertmanagerDelivery,
    ) -> IngestionResult: ...


class BackgroundRuntimeWakeup(Protocol):
    async def start(self) -> None: ...


def create_alert_ingestion_router(
    settings: AlertIngestionSettings,
    service: AlertIngestionHandler,
    runtime: BackgroundRuntimeWakeup,
    metrics: AlertIngestionMetrics,
) -> APIRouter:
    """Build a source-bound webhook router with no user-session dependency."""
    router = APIRouter(prefix="/aiops/alerts/webhook/alertmanager")

    @router.post("/{source_id}", status_code=202)
    async def receive(source_id: str, request: Request) -> JSONResponse:
        started = monotonic()
        metrics.record_received()
        source = settings.sources.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Alert source not found.")
        _authenticate(request.headers.get("authorization"), source.token)
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            metrics.record_request_failure(latency_ms=_elapsed_ms(started))
            raise HTTPException(status_code=422, detail="JSON body required.")
        try:
            raw_body = await _read_bounded_body(request, settings.max_body_bytes)
        except _BodyTooLarge as exc:
            metrics.record_request_failure(latency_ms=_elapsed_ms(started))
            raise HTTPException(status_code=413, detail="Request body too large.") from exc
        try:
            delivery = parse_alertmanager_delivery(
                raw_body,
                max_alerts=settings.max_alerts_per_delivery,
            )
            result = await service.ingest(source, delivery)
        except AlertPayloadError as exc:
            metrics.record_request_failure(latency_ms=_elapsed_ms(started))
            raise HTTPException(status_code=422, detail="Invalid Alertmanager payload.") from exc
        except AlertPersistenceError as exc:
            metrics.record_request_failure(latency_ms=_elapsed_ms(started))
            raise HTTPException(status_code=503, detail="Alert persistence unavailable.") from exc
        if result.disposition == "incident_created":
            try:
                await runtime.start()
            except Exception as exc:
                emit_event(
                    logger,
                    "alert.ingestion.worker_wakeup_failed",
                    sourceId=source.id,
                    errorCategory=exc.__class__.__name__,
                )
        return JSONResponse(status_code=202, content=_safe_response(result))

    _ = receive
    return router


class _BodyTooLarge(ValueError):
    pass


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > limit):
        raise _BodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _BodyTooLarge
    return bytes(body)


def _authenticate(header: str | None, expected_token: str) -> None:
    if header is None or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid webhook authorization.")
    candidate = header.removeprefix("Bearer ")
    if not candidate or any(character.isspace() for character in candidate):
        raise HTTPException(status_code=401, detail="Invalid webhook authorization.")
    if not hmac.compare_digest(candidate.encode("utf-8"), expected_token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid webhook authorization.")


def _safe_response(result: IngestionResult) -> dict[str, object]:
    return {
        "status": "accepted",
        "incidentId": result.incident_id,
        "diagnosticTaskId": result.diagnostic_task_id,
        "duplicate": result.duplicate,
        "filtered": result.filtered,
        "redisMode": result.redis_mode,
    }


def _elapsed_ms(started: float) -> float:
    return (monotonic() - started) * 1000
