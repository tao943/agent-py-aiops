from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from super_ai.api.app import create_app
from super_ai.memory.models import (
    AiopsExecutionModel,
    AlertIncidentModel,
    ProductionRecoveryAuditEventModel,
    ProductionRecoveryIntentModel,
)
from super_ai.redis_runtime.rate_limit import RateLimitDecision
from super_ai.vector_store import MilvusHealthCheckResult

ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = ROOT / "infra" / "compose.yaml"
ORDER_API = "http://127.0.0.1:18082"
ORDER_SERVICE = "live-eval-order-api"
CONTROL_TOKEN = "agentpy-live-eval-control"


class FakeVectorStore:
    def health_check(self) -> MilvusHealthCheckResult:
        return MilvusHealthCheckResult(True, "http://milvus.test", "chunks", 1.0)


class AllowAllRateLimits:
    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision:
        del owner_id, action
        return RateLimitDecision(True, 1, 0, "local_fallback")


def _project_config(tmp_path: Path) -> Path:
    configuration = json.loads(
        (ROOT / "config" / "project.test.json").read_text(encoding="utf-8")
    )
    configuration["productionRecovery"] = {
        "enabled": True,
        "approvalTtlSeconds": 600,
        "composeTargets": [
            {
                "targetKey": ORDER_SERVICE,
                "composeFile": "infra/compose.yaml",
                "service": ORDER_SERVICE,
                "automaticRecoveryEnabled": True,
                "healthUrl": f"{ORDER_API}/health",
                "businessProbeUrl": f"{ORDER_API}/probe",
                "diagnosticSelector": {
                    "component": "order-api",
                    "mechanisms": ["exception_path_connection_not_released"],
                    "requiredEvidenceFacts": [
                        "InspectOrderPoolState.poolAtCapacity"
                    ],
                },
            }
        ],
        "postgresTargets": [],
    }
    path = tmp_path / "project.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    return path


async def _register(client: httpx.AsyncClient) -> Mapping[str, Any]:
    response = await client.post(
        "/auth/register",
        json={
            "email": f"recovery-{uuid4().hex}@example.test",
            "displayName": "Recovery Acceptance",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    return cast(Mapping[str, Any], response.json()["data"])


def _auth(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _order_request(
    method: str,
    path: str,
    *,
    json_body: Mapping[str, object] | None = None,
    authorized: bool = True,
) -> httpx.Response:
    headers = {"x-live-control-token": CONTROL_TOKEN} if authorized else None
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        return await client.request(
            method,
            f"{ORDER_API}{path}",
            headers=headers,
            json=json_body,
        )


def _container_identity() -> tuple[str, str]:
    container_id = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "ps",
            "-q",
            ORDER_SERVICE,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    assert container_id
    started_at = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", container_id],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    assert started_at
    return container_id, started_at


async def _seed_grounded_diagnosis(
    app: Any,
    *,
    owner_user_id: str,
    task_id: str,
    incident_id: str,
) -> None:
    now = datetime.now(timezone.utc)
    diagnostics = app.state.memory_repositories.diagnostics
    await diagnostics.create_task(
        owner_user_id=owner_user_id,
        task_id=task_id,
        status="succeeded",
        query="Investigate the observed order-api pool saturation.",
        input_payload={"alert": {"service": "order-api"}},
        result_payload={"status": "succeeded"},
        created_at=now,
        completed_at=now,
    )
    await diagnostics.add_report(
        owner_user_id=owner_user_id,
        report_id=f"report-{task_id}",
        task_id=task_id,
        title="Grounded order-api diagnosis",
        content="The observed exception path retained every checked-out connection.",
        payload={
            "status": "succeeded",
            "rootCauseDecision": {
                "component": "order-api",
                "mechanism": "exception_path_connection_not_released",
                "evidenceIds": [f"evidence-{task_id}"],
            },
            "decisionValidation": {
                "status": "valid",
                "validationOrigin": "deterministic",
                "deterministicChecks": [{"code": "grounded", "passed": True}],
            },
            "evidenceSufficiency": {"status": "sufficient"},
        },
        created_at=now,
    )
    await diagnostics.create_evidence(
        owner_user_id=owner_user_id,
        evidence_id=f"evidence-{task_id}",
        task_id=task_id,
        kind="metric",
        source="InspectOrderPoolState",
        summary="The isolated order-api pool is at capacity.",
        payload={"output": {"poolAtCapacity": True}},
        created_at=now,
    )
    await diagnostics.link_report_evidence(
        owner_user_id=owner_user_id,
        link_id=f"link-{task_id}",
        task_id=task_id,
        report_id=f"report-{task_id}",
        evidence_id=f"evidence-{task_id}",
        created_at=now,
    )
    async with app.state.memory_session_factory() as session, session.begin():
        session.add(
            AlertIncidentModel(
                id=incident_id,
                owner_user_id=owner_user_id,
                source_id="production-recovery-live",
                group_key_hash=uuid4().hex + uuid4().hex,
                run_id=task_id,
                scenario_id="PRODUCTION-RECOVERY-COMPOSE-001",
                status="active",
                alert_name="OrderPoolExhausted",
                service="order-api",
                severity="critical",
                starts_at=now,
                last_seen_at=now,
                resolved_at=None,
                verification_status="pending",
                verified_at=None,
                verification_summary=None,
                delivery_count=1,
                diagnostic_task_id=task_id,
                created_at=now,
                updated_at=now,
            )
        )


async def _resolve_after_independent_recovery(
    app: Any,
    *,
    incident_id: str,
    before: tuple[str, str],
) -> None:
    for _ in range(300):
        try:
            after = await asyncio.to_thread(_container_identity)
            probe = await _order_request("GET", "/probe", authorized=False)
        except (httpx.HTTPError, subprocess.SubprocessError):
            await asyncio.sleep(0.05)
            continue
        if after != before and probe.status_code == 200:
            now = datetime.now(timezone.utc)
            async with app.state.memory_session_factory() as session, session.begin():
                await session.execute(
                    update(AlertIncidentModel)
                    .where(AlertIncidentModel.id == incident_id)
                    .values(status="resolved", resolved_at=now, updated_at=now)
                )
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Independent recovery signals did not converge.")


@pytest.mark.asyncio
async def test_compose_recovery_closes_once_through_production_http_api(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    health = await _order_request("GET", "/health", authorized=False)
    assert health.status_code == 200
    run_id = f"recovery-{uuid4().hex[:12]}"
    fault_token = uuid4().hex
    started = await _order_request(
        "POST",
        "/internal/runs/start",
        json_body={"run_id": run_id, "fault_token": fault_token},
    )
    assert started.status_code == 200
    for index in range(3):
        injected = await _order_request(
            "POST",
            f"/internal/runs/{run_id}/fault",
            json_body={"fault_token": fault_token, "request_id": f"fault-{index}"},
        )
        assert injected.status_code == 200
    failed_probe = await _order_request("GET", "/probe", authorized=False)
    assert failed_probe.status_code == 503

    app = create_app(
        database_url=migrated_database_url,
        project_config_path=_project_config(tmp_path),
        vector_store=cast(Any, FakeVectorStore()),
        rate_limit_service=AllowAllRateLimits(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    before = await asyncio.to_thread(_container_identity)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            registered = await _register(client)
            owner_user_id = cast(str, cast(Mapping[str, object], registered["user"])["id"])
            token = registered["accessToken"]
            task_id = f"diagnostic-{uuid4().hex}"
            incident_id = f"incident-{uuid4().hex}"
            await _seed_grounded_diagnosis(
                app,
                owner_user_id=owner_user_id,
                task_id=task_id,
                incident_id=incident_id,
            )
            resolver = asyncio.create_task(
                _resolve_after_independent_recovery(
                    app,
                    incident_id=incident_id,
                    before=before,
                )
            )
            first, duplicate = await asyncio.gather(
                client.post(
                    f"/aiops/diagnostics/{task_id}/recovery-intents",
                    headers=_auth(token),
                    json={"note": "Run governed recovery."},
                ),
                client.post(
                    f"/aiops/diagnostics/{task_id}/recovery-intents",
                    headers=_auth(token),
                    json={"note": "Duplicate client delivery."},
                ),
            )
            assert {first.status_code, duplicate.status_code} == {200, 201}
            intent_ids = {
                first.json()["data"]["id"],
                duplicate.json()["data"]["id"],
            }
            assert len(intent_ids) == 1
            intent_id = intent_ids.pop()
            terminal: Mapping[str, object] | None = None
            for _ in range(300):
                response = await client.get(
                    f"/aiops/recovery-intents/{intent_id}",
                    headers=_auth(token),
                )
                assert response.status_code == 200
                candidate = cast(Mapping[str, object], response.json()["data"])
                if candidate["status"] in {
                    "recovered",
                    "verification_failed",
                    "manual_intervention",
                    "denied",
                }:
                    terminal = candidate
                    break
                await asyncio.sleep(0.1)
            await resolver
            assert terminal is not None
            assert terminal["status"] == "recovered", terminal
            assert all(
                check["status"] == "passed"
                for check in cast(list[Mapping[str, object]], terminal["verification"])
            )
            events = await client.get(
                f"/aiops/recovery-intents/{intent_id}/events?afterSequence=0",
                headers=_auth(token),
            )
            assert events.status_code == 200
            assert [
                item["toStatus"] for item in events.json()["data"]["items"]
            ] == [
                "queued",
                "revalidating",
                "executing",
                "verifying",
                "recovered",
            ]
    finally:
        await app.state.background_job_runtime.stop()

    after = await asyncio.to_thread(_container_identity)
    assert after != before
    assert (await _order_request("GET", "/health", authorized=False)).status_code == 200
    assert (await _order_request("GET", "/probe", authorized=False)).status_code == 200
    async with app.state.memory_session_factory() as session:
        intent_count = await session.scalar(
            select(func.count()).select_from(ProductionRecoveryIntentModel)
        )
        execution_count = await session.scalar(
            select(func.count())
            .select_from(AiopsExecutionModel)
            .where(AiopsExecutionModel.execution_kind == "recovery")
        )
        audit_count = await session.scalar(
            select(func.count()).select_from(ProductionRecoveryAuditEventModel)
        )
    assert intent_count == 1
    assert execution_count == 1
    assert audit_count == 5
