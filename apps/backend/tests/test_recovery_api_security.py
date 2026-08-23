from __future__ import annotations

import httpx
import pytest

from test_recovery_api import Intents, build_app, make_intent


@pytest.mark.asyncio
async def test_non_owner_and_path_traversal_cannot_enumerate_intents() -> None:
    repository = Intents(make_intent())
    app, _ = build_app(repository, owner_id="other-owner")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        non_owner = await client.get("/aiops/recovery-intents/intent-1")
        traversal = await client.get("/aiops/recovery-intents/..%2Fintent-1")

    assert non_owner.status_code == 403
    assert traversal.status_code in {400, 404}


@pytest.mark.asyncio
async def test_extra_action_pid_and_path_fields_are_rejected() -> None:
    app, _ = build_app(Intents())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/aiops/diagnostics/diagnostic-1/recovery-intents",
            json={"action": "restart", "pid": 42, "path": "../../host"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_wrong_incident_confirmation_cannot_approve() -> None:
    repository = Intents(make_intent("awaiting_approval"))
    app, runtime = build_app(repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/aiops/recovery-intents/intent-1:approve",
            json={"incidentIdConfirmation": "incident-other"},
        )

    assert response.status_code == 400
    assert repository.intent is not None
    assert repository.intent.status == "awaiting_approval"
    assert runtime.starts == 0
