"""Isolated order-api pool exhaustion Live driver and read-only evidence."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

import httpx

from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveScenario,
    LiveVerification,
)
from super_ai.evaluation.live.postgres import PostgresConnectionConfig
from super_ai.mcp_client import McpClientError, McpToolDefinition

SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"
_SERVICE_NAME = "live-eval-order-api"
_MECHANISM = "exception_path_connection_not_released"
_SESSION_PREFIX = "agentpy-order-api"
_SESSION_TOKEN_LENGTH = 16


def _session_run_scope(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:_SESSION_TOKEN_LENGTH]


def _session_run_pattern(run_id: str) -> str:
    return f"{_SESSION_PREFIX}:{_session_run_scope(run_id)}:%"


def _session_application_name(run_id: str, generation: str) -> str:
    return (
        f"{_SESSION_PREFIX}:{_session_run_scope(run_id)}:"
        f"{generation[:_SESSION_TOKEN_LENGTH]}"
    )


def _default_compose_file() -> Path:
    return Path(__file__).resolve().parents[6] / "infra" / "compose.yaml"


@dataclass(frozen=True, slots=True)
class OrderPoolLiveConfig:
    base_url: str = "http://127.0.0.1:18082"
    control_token: str = field(default="agentpy-live-eval-control", repr=False)
    pool_size: int = 3
    probe_timeout_seconds: float = 0.5
    compose_file: Path = field(default_factory=_default_compose_file)
    service_name: str = _SERVICE_NAME

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 18082
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Order pool Live Eval must use loopback port 18082.")
        if self.service_name != _SERVICE_NAME:
            raise ValueError("Order pool Live Eval service name is fixed.")
        if not self.compose_file.is_file() or self.compose_file.name != "compose.yaml":
            raise ValueError("Order pool Live Eval compose file is unavailable.")
        if not 1 <= self.pool_size <= 16:
            raise ValueError("Order pool Live Eval pool size is invalid.")


class OrderApiControlBoundary(Protocol):
    async def health(self) -> Mapping[str, object]: ...

    async def start_run(self, identity: LiveRunIdentity, fault_token: str) -> None: ...

    async def inject_fault(
        self,
        identity: LiveRunIdentity,
        fault_token: str,
        request_id: str,
    ) -> None: ...

    async def probe(self, identity: LiveRunIdentity) -> bool: ...

    async def state(self, identity: LiveRunIdentity) -> Mapping[str, object]: ...

    async def events(self, identity: LiveRunIdentity) -> Sequence[Mapping[str, str]]: ...

    async def clear(self, identity: LiveRunIdentity) -> None: ...


class OrderPoolPostgresBoundary(Protocol):
    async def database_reachable(self) -> bool: ...

    async def run_scoped_session_count(self, run_id: str) -> int: ...

    async def lock_wait_observed(self, run_id: str) -> bool: ...

    async def generation_session_count(self, run_id: str, generation: str) -> int: ...

    async def test_order_count(self, run_id: str) -> int: ...

    async def unrelated_sessions(self) -> frozenset[str]: ...


class ComposeRestartBoundary(Protocol):
    async def restart(self, service_name: str) -> None: ...


class HttpOrderApiControl:
    def __init__(self, config: OrderPoolLiveConfig) -> None:
        self._config = config

    async def health(self) -> Mapping[str, object]:
        return await self._request("GET", "/health", authorized=False)

    async def start_run(self, identity: LiveRunIdentity, fault_token: str) -> None:
        await self._request(
            "POST",
            "/internal/runs/start",
            json={"run_id": identity.run_id, "fault_token": fault_token},
        )

    async def inject_fault(
        self,
        identity: LiveRunIdentity,
        fault_token: str,
        request_id: str,
    ) -> None:
        await self._request(
            "POST",
            f"/internal/runs/{identity.run_id}/fault",
            json={"fault_token": fault_token, "request_id": request_id},
        )

    async def probe(self, identity: LiveRunIdentity) -> bool:
        result = await self._request(
            "POST",
            f"/internal/runs/{identity.run_id}/probe",
            json={
                "request_id": "business-probe",
                "timeout_seconds": self._config.probe_timeout_seconds,
            },
        )
        return result.get("passed") is True

    async def state(self, identity: LiveRunIdentity) -> Mapping[str, object]:
        return await self._request("GET", f"/internal/runs/{identity.run_id}/state")

    async def events(self, identity: LiveRunIdentity) -> Sequence[Mapping[str, str]]:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            response = await client.get(
                f"{self._config.base_url}/internal/runs/{identity.run_id}/events",
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("order_api_events_invalid")
        return cast(Sequence[Mapping[str, str]], payload)

    async def clear(self, identity: LiveRunIdentity) -> None:
        await self._request("DELETE", f"/internal/runs/{identity.run_id}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        authorized: bool = True,
    ) -> Mapping[str, object]:
        headers = self._headers() if authorized else None
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            response = await client.request(
                method,
                f"{self._config.base_url}{path}",
                headers=headers,
                json=json,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("order_api_response_invalid")
        return cast(Mapping[str, object], payload)

    def _headers(self) -> dict[str, str]:
        return {"x-live-control-token": self._config.control_token}


class PostgresOrderPoolObserver:
    def __init__(self, config: PostgresConnectionConfig) -> None:
        self._config = config

    async def database_reachable(self) -> bool:
        return await self._fetch_value("SELECT 1", application_name="order-pool-observer") == 1

    async def run_scoped_session_count(self, run_id: str) -> int:
        return _count(
            await self._fetch_value(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE $1",
                _session_run_pattern(run_id),
            )
        )

    async def lock_wait_observed(self, run_id: str) -> bool:
        return bool(
            await self._fetch_value(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                "WHERE application_name LIKE $1 AND wait_event_type = 'Lock')",
                _session_run_pattern(run_id),
            )
        )

    async def generation_session_count(self, run_id: str, generation: str) -> int:
        return _count(
            await self._fetch_value(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1",
                _session_application_name(run_id, generation),
            )
        )

    async def test_order_count(self, run_id: str) -> int:
        return _count(
            await self._fetch_value(
                "SELECT count(*) FROM live_eval_orders WHERE run_id = $1",
                run_id,
            )
        )

    async def unrelated_sessions(self) -> frozenset[str]:
        connection = await self._config.connect(application_name="order-pool-observer")
        try:
            rows = await connection.fetch(
                "SELECT pid::text || ':' || backend_start::text AS session_identity "
                "FROM pg_stat_activity "
                "WHERE datname = 'agent_py_live_eval' "
                "AND backend_type = 'client backend' "
                "AND application_name NOT LIKE 'agentpy-order-api:%' "
                "AND application_name <> 'order-pool-observer'"
            )
        finally:
            await connection.close()
        typed_rows = cast(Sequence[Mapping[str, object]], rows)
        return frozenset(
            _required_text(
                row.get("session_identity"),
                "order_pool_session_identity_invalid",
            )
            for row in typed_rows
        )

    async def _fetch_value(
        self,
        query: str,
        *arguments: object,
        application_name: str = "order-pool-observer",
    ) -> object:
        connection = await self._config.connect(application_name=application_name)
        try:
            return await connection.fetchval(query, *arguments)
        finally:
            await connection.close()


class ComposeServiceRestarter:
    def __init__(
        self,
        config: OrderPoolLiveConfig,
        *,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("order_pool_restart_timeout_invalid")
        self._config = config
        self._command_timeout_seconds = command_timeout_seconds

    async def restart(self, service_name: str) -> None:
        if service_name != self._config.service_name:
            raise ValueError("order_pool_restart_target_invalid")
        process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(self._config.compose_file),
            "restart",
            self._config.service_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            _, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self._command_timeout_seconds,
            )
        except TimeoutError as exc:
            await _terminate_subprocess(process)
            raise RuntimeError("order_pool_restart_timeout") from exc
        if process.returncode != 0:
            raise RuntimeError("order_pool_restart_failed")
        for _ in range(60):
            try:
                async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
                    response = await client.get(f"{self._config.base_url}/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        raise RuntimeError("order_pool_restart_readiness_timeout")


async def _terminate_subprocess(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        try:
            tree_killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(tree_killer.wait(), timeout=5.0)
        except (OSError, TimeoutError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        pass


@dataclass(frozen=True, slots=True)
class OrderPoolLiveRunAudit:
    order_api_healthy: bool
    active_run_count: int
    test_order_count: int
    old_generation_session_count: int

    @property
    def clean(self) -> bool:
        return (
            self.order_api_healthy
            and self.active_run_count == 0
            and self.test_order_count == 0
            and self.old_generation_session_count == 0
        )

    def safe_payload(self) -> dict[str, object]:
        return {
            "orderApiHealthy": self.order_api_healthy,
            "activeRunCount": self.active_run_count,
            "testOrderCount": self.test_order_count,
            "oldGenerationSessionCount": self.old_generation_session_count,
            "clean": self.clean,
        }


@dataclass(slots=True)
class _OrderPoolRun:
    fault_token: str
    original_generation: str
    unrelated_sessions_before: frozenset[str]
    recovery_started: bool = False
    recovery_completed: bool = False


class OrderPoolLeakScenarioDriver:
    def __init__(
        self,
        config: OrderPoolLiveConfig,
        *,
        api: OrderApiControlBoundary,
        postgres: OrderPoolPostgresBoundary,
    ) -> None:
        self._config = config
        self._api = api
        self._postgres = postgres
        self._runs: dict[str, _OrderPoolRun] = {}

    async def preflight(self, identity: LiveRunIdentity) -> None:
        health = await self._api.health()
        if health.get("status") != "ok" or not await self._postgres.database_reachable():
            raise RuntimeError("order_pool_preflight_unhealthy")
        active = health.get("activeRunId")
        if active not in {None, identity.run_id}:
            raise RuntimeError("order_pool_another_run_active")
        if await self._postgres.run_scoped_session_count(identity.run_id):
            raise RuntimeError("order_pool_residual_sessions")

    async def baseline(self, identity: LiveRunIdentity) -> None:
        health = await self._api.health()
        generation = _required_text(health.get("generation"), "order_api_generation_invalid")
        fault_token = hashlib.sha256(
            f"{identity.run_token}:order-pool-fault".encode()
        ).hexdigest()
        await self._api.start_run(identity, fault_token)
        if not await self._api.probe(identity):
            raise RuntimeError("order_pool_baseline_probe_failed")
        self._runs[identity.run_id] = _OrderPoolRun(
            fault_token=fault_token,
            original_generation=generation,
            unrelated_sessions_before=await self._postgres.unrelated_sessions(),
        )

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation:
        run = self._runs[identity.run_id]
        for index in range(self._config.pool_size):
            await self._api.inject_fault(
                identity,
                run.fault_token,
                f"fault-{index + 1}",
            )
        probe_succeeded = await self._api.probe(identity)
        state = await self._api.state(identity)
        checked_out = _count(state.get("checkedOut"))
        capacity = _count(state.get("capacity"))
        free = _count(state.get("free"))
        scoped_sessions = await self._postgres.run_scoped_session_count(identity.run_id)
        database_reachable = await self._postgres.database_reachable()
        lock_wait = await self._postgres.lock_wait_observed(identity.run_id)
        return LiveFaultObservation(
            scenario_id=SCENARIO_ID,
            checks=(
                LiveCheck("pool_at_capacity", checked_out == capacity == self._config.pool_size),
                LiveCheck("pool_free_zero", free == 0),
                LiveCheck("business_probe_timed_out", not probe_succeeded),
                LiveCheck("postgres_reachable", database_reachable),
                LiveCheck("no_lock_wait", not lock_wait),
                LiveCheck("run_scoped_sessions_present", scoped_sessions >= self._config.pool_size),
            ),
            safe_facts=(
                ("poolCapacity", capacity),
                ("checkedOutConnections", checked_out),
                ("freeConnections", free),
                ("waiterObserved", state.get("waitersObserved") is True),
                ("businessProbeTimedOut", not probe_succeeded),
                ("databaseReachable", database_reachable),
                ("lockWaitObserved", lock_wait),
                ("runScopedSessionCount", scoped_sessions),
                ("generation", run.original_generation),
            ),
        )

    async def events(self, identity: LiveRunIdentity) -> Sequence[Mapping[str, str]]:
        return await self._api.events(identity)

    def recovery_eligible(self, identity: LiveRunIdentity) -> bool:
        return identity.run_id in self._runs

    def mark_recovery_started(self, identity: LiveRunIdentity) -> bool:
        run = self._runs[identity.run_id]
        if run.recovery_started:
            return False
        run.recovery_started = True
        return True

    def mark_recovery_completed(self, identity: LiveRunIdentity) -> None:
        self._runs[identity.run_id].recovery_completed = True

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification:
        run = self._runs[identity.run_id]
        health = await self._api.health()
        new_generation = health.get("generation")
        generation_changed = isinstance(new_generation, str) and (
            new_generation != run.original_generation
        )
        old_sessions = await self._postgres.generation_session_count(
            identity.run_id, run.original_generation
        )
        await self._api.start_run(identity, run.fault_token)
        probe_succeeded = await self._api.probe(identity)
        unrelated_after = await self._postgres.unrelated_sessions()
        return LiveVerification(
            (
                LiveCheck("old_generation_released", old_sessions == 0),
                LiveCheck("new_generation_ready", generation_changed),
                LiveCheck("business_probe_recovered", probe_succeeded),
                LiveCheck("postgres_healthy", await self._postgres.database_reachable()),
                LiveCheck(
                    "unrelated_sessions_preserved",
                    run.unrelated_sessions_before.issubset(unrelated_after),
                ),
                LiveCheck("scoped_recovery_recorded", run.recovery_completed),
            )
        )

    async def cleanup(self, identity: LiveRunIdentity) -> LiveCleanupResult:
        run = self._runs.get(identity.run_id)
        try:
            await self._api.clear(identity)
        except (httpx.HTTPStatusError, RuntimeError):
            health = await self._api.health()
            if health.get("activeRunId") == identity.run_id:
                raise
        old_sessions = 0
        if run is not None:
            old_sessions = await self._postgres.generation_session_count(
                identity.run_id, run.original_generation
            )
        self._runs.pop(identity.run_id, None)
        return LiveCleanupResult(
            (
                LiveCheck(
                    "test_order_removed",
                    await self._postgres.test_order_count(identity.run_id) == 0,
                ),
                LiveCheck("old_generation_sessions_removed", old_sessions == 0),
                LiveCheck("postgres_cleanup_health", await self._postgres.database_reachable()),
            )
        )

    async def audit(self, identity: LiveRunIdentity) -> OrderPoolLiveRunAudit:
        health = await self._api.health()
        run = self._runs.get(identity.run_id)
        old_sessions = (
            await self._postgres.generation_session_count(
                identity.run_id, run.original_generation
            )
            if run is not None
            else await self._postgres.run_scoped_session_count(identity.run_id)
        )
        return OrderPoolLiveRunAudit(
            order_api_healthy=health.get("status") == "ok",
            active_run_count=int(health.get("activeRunId") is not None),
            test_order_count=await self._postgres.test_order_count(identity.run_id),
            old_generation_session_count=old_sessions,
        )


class OrderPoolRecoveryService:
    def __init__(
        self,
        driver: OrderPoolLeakScenarioDriver,
        restarter: ComposeRestartBoundary,
    ) -> None:
        self._driver = driver
        self._restarter = restarter

    async def recover(
        self,
        *,
        identity: LiveRunIdentity,
        diagnostic_artifact: object,
        observation: LiveFaultObservation,
    ) -> LiveRecoveryRecord:
        decision = (
            diagnostic_artifact.decision
            if isinstance(diagnostic_artifact, RunArtifact)
            else None
        )
        authorized = bool(
            decision is not None
            and decision.component == "order-api"
            and decision.mechanism == _MECHANISM
            and observation.scenario_id == SCENARIO_ID
            and observation.confirmed
            and self._driver.recovery_eligible(identity)
        )
        if not authorized:
            return LiveRecoveryRecord(
                "none", "none", "executed_recovery", False, False, "order_pool_decision_required"
            )
        if not self._driver.mark_recovery_started(identity):
            return LiveRecoveryRecord(
                "restart_live_eval_order_api",
                "current_run_order_api_instance",
                "executed_recovery",
                True,
                False,
                "recovery_intent_already_started",
            )
        await self._restarter.restart(_SERVICE_NAME)
        self._driver.mark_recovery_completed(identity)
        return LiveRecoveryRecord(
            "restart_live_eval_order_api",
            "current_run_order_api_instance",
            "executed_recovery",
            True,
            True,
            "authorized",
        )


class OrderPoolRuntimeEvidenceMcpClient:
    def __init__(self, observation: LiveFaultObservation) -> None:
        if observation.scenario_id != SCENARIO_ID:
            raise ValueError("Order pool evidence requires the matching scenario.")
        self._observation = observation

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        schema: dict[str, object] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return tuple(
            McpToolDefinition(name, description, schema, "order-pool-live")
            for name, description in (
                ("InspectOrderPoolState", "Read sanitized order-api pool capacity state."),
                (
                    "InspectOrderDatabaseSessions",
                    "Read sanitized current-run PostgreSQL session and lock-wait state.",
                ),
                (
                    "VerifyOrderDatabaseReachability",
                    "Read database reachability and business acquisition outcome.",
                ),
            )
        )

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        if arguments:
            raise McpClientError("Order pool Live evidence arguments are invalid.")
        if name == "InspectOrderPoolState":
            return {
                "benchmarkEvidenceId": "order-pool-saturated",
                "poolAtCapacity": self._observation.check_passed("pool_at_capacity"),
                "freeConnections": self._observation.safe_fact("freeConnections"),
                "waiterObserved": self._observation.safe_fact("waiterObserved"),
            }
        if name == "InspectOrderDatabaseSessions":
            return {
                "benchmarkEvidenceId": "order-db-sessions",
                "databaseReachable": self._observation.safe_fact("databaseReachable"),
                "runScopedSessionsPresent": self._observation.check_passed(
                    "run_scoped_sessions_present"
                ),
                "lockWaitObserved": self._observation.safe_fact("lockWaitObserved"),
            }
        if name == "VerifyOrderDatabaseReachability":
            return {
                "benchmarkEvidenceId": "order-pool-acquire-timeout",
                "databaseReachable": self._observation.safe_fact("databaseReachable"),
                "businessProbeTimedOut": self._observation.safe_fact(
                    "businessProbeTimedOut"
                ),
            }
        raise McpClientError("Order pool Live evidence tool is not allowed.")


class OrderPoolClsRecordProvider:
    def __init__(self, driver: OrderPoolLeakScenarioDriver) -> None:
        self._driver = driver

    async def records(
        self,
        *,
        identity: LiveRunIdentity,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        now: datetime,
    ) -> Sequence[Mapping[str, str]]:
        del now
        if scenario.id != SCENARIO_ID or observation.scenario_id != SCENARIO_ID:
            raise RuntimeError("order_pool_cls_scope_invalid")
        incident_id = f"{scenario.id}-{identity.run_id}"
        projected: list[dict[str, str]] = []
        for event in await self._driver.events(identity):
            projected.append(
                {
                    **dict(event),
                    "scenario_id": scenario.id,
                    "incident_id": incident_id,
                    "environment": "live-eval",
                    "trace": f"{identity.run_id}-{event.get('request_id', 'unknown')}",
                }
            )
        return tuple(projected)


def _required_text(value: object, error: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(error)
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("order_pool_count_invalid")
    return value
