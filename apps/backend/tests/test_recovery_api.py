from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI, Request

from super_ai.api.responses import ApiErrorException, error_response
from super_ai.auth.repositories import UserRecord
from super_ai.recovery.api import create_recovery_router
from super_ai.recovery.config import ProductionRecoverySettings
from super_ai.recovery.contracts import (
    RecoveryAuditEventRecord,
    RecoveryIntentRecord,
    RecoveryStatus,
)
from super_ai.recovery.repository import RecoveryIntentCreateResult

NOW = datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc)


def make_intent(status: RecoveryStatus = "queued") -> RecoveryIntentRecord:
    return RecoveryIntentRecord(
        "intent-1", "owner-1", "incident-1", "diagnostic-1", "report-1",
        "restart_compose_service", "live-eval-order-api", "low", True, False,
        status, "a" * 64, ("evidence-1",), {}, {"private": "not-public"}, NOW,
        None, None, None, None, None, (),
    )  # type: ignore[arg-type]


class Service:
    def __init__(self, repository: Intents) -> None:
        self.repository = repository

    async def create_result(self, **_: object) -> RecoveryIntentCreateResult:
        self.repository.intent = make_intent()
        return RecoveryIntentCreateResult(self.repository.intent, False)


class Intents:
    def __init__(self, intent: RecoveryIntentRecord | None = None) -> None:
        self.intent = intent

    async def get_owned(self, *, owner_user_id: str, intent_id: str):  # type: ignore[no-untyped-def]
        if (
            self.intent
            and owner_user_id == self.intent.owner_user_id
            and intent_id == self.intent.id
        ):
            return self.intent
        return None

    async def approve_with_job_and_event(self, **_: object):  # type: ignore[no-untyped-def]
        assert self.intent is not None
        self.intent = replace(self.intent, status="queued")
        return self.intent

    async def reject(self, **_: object):  # type: ignore[no-untyped-def]
        assert self.intent is not None
        self.intent = replace(self.intent, status="rejected")
        return self.intent

    async def cancel_before_claim(self, **_: object):  # type: ignore[no-untyped-def]
        assert self.intent is not None
        self.intent = replace(self.intent, status="cancelled")
        return self.intent

    async def list_events(self, **_: object) -> list[RecoveryAuditEventRecord]:
        return [
            RecoveryAuditEventRecord(
                1, "intent.created", None, "queued", None,
                "Recovery intent created.", None, NOW,
            )
        ]


class Runtime:
    def __init__(self) -> None:
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1


def build_app(repository: Intents, *, owner_id: str = "owner-1") -> tuple[FastAPI, Runtime]:
    app = FastAPI()
    runtime = Runtime()

    async def user() -> UserRecord:
        return UserRecord(owner_id, "u@example.test", "User", "hidden", NOW, NOW)

    async def rate_limit(owner: str) -> None:
        assert owner == owner_id

    app.include_router(
        create_recovery_router(
            current_user_dependency=user,
            service=Service(repository),  # type: ignore[arg-type]
            intents=repository,  # type: ignore[arg-type]
            settings=ProductionRecoverySettings(True, 600, {}, {}, {}),
            runtime=runtime,
            rate_limit_guard=rate_limit,
            now=lambda: NOW,
        )
    )

    @app.exception_handler(ApiErrorException)
    async def api_error(request: Request, exc: ApiErrorException):  # type: ignore[no-untyped-def]
        return error_response(request, exc.code, message=exc.message)

    return app, runtime


@pytest.mark.asyncio
async def test_create_get_and_events_return_only_public_projection() -> None:
    repository = Intents()
    app, runtime = build_app(repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/aiops/diagnostics/diagnostic-1/recovery-intents", json={"note": "review"}
        )
        fetched = await client.get("/aiops/recovery-intents/intent-1")
        events = await client.get(
            "/aiops/recovery-intents/intent-1/events?afterSequence=0"
        )

    assert created.status_code == 201, created.text
    assert fetched.status_code == 200
    assert events.status_code == 200
    assert runtime.starts == 1
    serialized = str((created.json(), fetched.json(), events.json()))
    assert "trusted_snapshot" not in serialized
    assert "canonical_arguments" not in serialized
    assert "not-public" not in serialized


@pytest.mark.asyncio
async def test_owner_can_approve_exact_incident_confirmation() -> None:
    repository = Intents(make_intent("awaiting_approval"))
    app, runtime = build_app(repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/aiops/recovery-intents/intent-1:approve",
            json={"incidentIdConfirmation": "incident-1"},
        )

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "queued"
    assert runtime.starts == 1
