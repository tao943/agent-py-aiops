"""Isolated order-api pool exhaustion Live driver and read-only evidence."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import hashlib
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

import httpx
from prometheus_client.parser import text_string_to_metric_families

from super_ai.aiops.tool_routing import AutomaticLiveEvidenceScope
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


def _validate_order_pool_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
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


@dataclass(frozen=True, slots=True)
class OrderPoolLiveConfig:
    base_url: str = "http://127.0.0.1:18082"
    control_token: str = field(default="agentpy-live-eval-control", repr=False)
    pool_size: int = 3
    probe_timeout_seconds: float = 0.5
    compose_file: Path = field(default_factory=_default_compose_file)
    service_name: str = _SERVICE_NAME

    def __post_init__(self) -> None:
        _validate_order_pool_base_url(self.base_url)
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
    unrelated_session_fingerprints: frozenset[str]
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

    def export_resume_state(self, identity: LiveRunIdentity) -> dict[str, object]:
        run = self._runs[identity.run_id]
        return {
            "originalGeneration": run.original_generation,
            "unrelatedSessionFingerprints": sorted(
                run.unrelated_session_fingerprints
            ),
        }

    def restore(
        self,
        identity: LiveRunIdentity,
        state: Mapping[str, object],
    ) -> None:
        if set(state) != {
            "originalGeneration",
            "unrelatedSessionFingerprints",
        }:
            raise ValueError("order_pool_resume_state_invalid")
        generation = state.get("originalGeneration")
        fingerprints = state.get("unrelatedSessionFingerprints")
        if (
            not isinstance(generation, str)
            or not generation
            or len(generation) > 96
            or not isinstance(fingerprints, list)
            or len(cast(list[object], fingerprints)) > 128
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(character not in "0123456789abcdef" for character in item)
                for item in cast(list[object], fingerprints)
            )
        ):
            raise ValueError("order_pool_resume_state_invalid")
        fault_token = hashlib.sha256(
            f"{identity.run_token}:order-pool-fault".encode()
        ).hexdigest()
        self._runs[identity.run_id] = _OrderPoolRun(
            fault_token=fault_token,
            original_generation=generation,
            unrelated_session_fingerprints=frozenset(cast(Sequence[str], fingerprints)),
        )

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
            unrelated_session_fingerprints=_fingerprint_sessions(
                await self._postgres.unrelated_sessions()
            ),
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
                    run.unrelated_session_fingerprints.issubset(
                        _fingerprint_sessions(unrelated_after)
                    ),
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


@dataclass(frozen=True, slots=True)
class OrderPoolMetricSnapshot:
    capacity: int
    checked_out: int
    free: int
    waiter_observed: bool
    fault_active: bool
    business_probe_success: bool

    @property
    def pool_at_capacity(self) -> bool:
        return self.checked_out == self.capacity


class OrderPoolMetricsBoundary(Protocol):
    async def snapshot(self, run_id: str) -> OrderPoolMetricSnapshot: ...


class ResidentOrderPoolPostgresBoundary(Protocol):
    async def database_reachable(self) -> bool: ...

    async def run_scoped_session_count(self, run_id: str) -> int: ...

    async def lock_wait_observed(self, run_id: str) -> bool: ...


_ORDER_POOL_METRICS = frozenset(
    {
        "agentpy_order_pool_capacity",
        "agentpy_order_pool_checked_out",
        "agentpy_order_pool_free",
        "agentpy_order_pool_waiter_observed",
        "agentpy_order_pool_fault_active",
        "agentpy_order_business_probe_success",
    }
)
_ORDER_POOL_BOOLEAN_METRICS = frozenset(
    {
        "agentpy_order_pool_waiter_observed",
        "agentpy_order_pool_fault_active",
        "agentpy_order_business_probe_success",
    }
)
_ORDER_POOL_METRICS_MAX_BODY_BYTES = 256 * 1024


class HttpOrderPoolMetricsReader:
    """Read one exact, bounded public Order Pool Prometheus snapshot."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 3.0,
        max_body_bytes: int = _ORDER_POOL_METRICS_MAX_BODY_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _validate_order_pool_base_url(base_url)
        if timeout_seconds <= 0 or not 1 <= max_body_bytes <= 1024 * 1024:
            raise ValueError("Order pool metrics reader limits are invalid.")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_body_bytes = max_body_bytes
        self._transport = transport

    async def snapshot(self, run_id: str) -> OrderPoolMetricSnapshot:
        expected_labels = {
            "service": "order-api",
            "environment": "live-eval",
            "scenario_id": SCENARIO_ID,
            "run_id": run_id,
        }
        try:
            body = await self._read_body()
            values: dict[str, int] = {}
            for family in text_string_to_metric_families(body):
                for sample in family.samples:
                    if sample.name not in _ORDER_POOL_METRICS:
                        continue
                    if dict(sample.labels) != expected_labels:
                        continue
                    if sample.name in values:
                        raise ValueError("duplicate metric")
                    numeric = float(sample.value)
                    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
                        raise ValueError("invalid metric value")
                    values[sample.name] = int(numeric)
            if set(values) != set(_ORDER_POOL_METRICS):
                raise ValueError("incomplete metric snapshot")
            if any(values[name] not in {0, 1} for name in _ORDER_POOL_BOOLEAN_METRICS):
                raise ValueError("invalid boolean metric")
            capacity = values["agentpy_order_pool_capacity"]
            checked_out = values["agentpy_order_pool_checked_out"]
            free = values["agentpy_order_pool_free"]
            fault_active = values["agentpy_order_pool_fault_active"]
            business_probe_success = values["agentpy_order_business_probe_success"]
            if (
                not 1 <= capacity <= 16
                or checked_out > capacity
                or checked_out + free != capacity
                or fault_active != int(checked_out > 0)
                or business_probe_success != int(free > 0)
            ):
                raise ValueError("contradictory metric snapshot")
        except (httpx.HTTPError, UnicodeError, ValueError):
            raise McpClientError("Order pool metrics evidence is unavailable.") from None
        return OrderPoolMetricSnapshot(
            capacity=capacity,
            checked_out=checked_out,
            free=free,
            waiter_observed=bool(values["agentpy_order_pool_waiter_observed"]),
            fault_active=bool(fault_active),
            business_probe_success=bool(business_probe_success),
        )

    async def _read_body(self) -> str:
        content = bytearray()
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            async with client.stream("GET", f"{self._base_url}/metrics") as response:
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(
                        "Order pool metrics returned a non-success status.",
                        request=response.request,
                        response=response,
                    )
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > self._max_body_bytes:
                        raise ValueError("metrics response too large")
                    content.extend(chunk)
        return content.decode("utf-8", errors="strict")


class ResidentOrderPoolEvidenceMcpClient:
    """Reconstruct read-only evidence in the resident backend process."""

    def __init__(
        self,
        *,
        scope: AutomaticLiveEvidenceScope,
        metrics_reader: OrderPoolMetricsBoundary,
        postgres_observer: ResidentOrderPoolPostgresBoundary,
    ) -> None:
        if scope.scenario_id != SCENARIO_ID:
            raise ValueError("Order pool resident evidence requires the matching scenario.")
        self._scope = scope
        self._metrics_reader = metrics_reader
        self._postgres = postgres_observer

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        schema: dict[str, object] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return tuple(
            McpToolDefinition(name, description, schema, "order-pool-live")
            for name, description in (
                ("InspectOrderPoolState", "Read the current run's bounded pool metrics."),
                (
                    "InspectOrderDatabaseSessions",
                    "Read current-run PostgreSQL session and lock-wait state.",
                ),
                (
                    "VerifyOrderDatabaseReachability",
                    "Read database reachability and bounded business probe state.",
                ),
            )
        )

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        if arguments:
            raise McpClientError("Order pool resident evidence arguments are invalid.")
        if name == "InspectOrderPoolState":
            snapshot = await self._metrics_reader.snapshot(self._scope.run_id)
            return {
                "benchmarkEvidenceId": "order-pool-saturated",
                "poolAtCapacity": snapshot.pool_at_capacity,
                "freeConnections": snapshot.free,
                "waiterObserved": snapshot.waiter_observed,
            }
        if name == "InspectOrderDatabaseSessions":
            try:
                snapshot = await self._metrics_reader.snapshot(self._scope.run_id)
                reachable = await self._postgres.database_reachable()
                sessions = await self._postgres.run_scoped_session_count(
                    self._scope.run_id
                )
                lock_wait = await self._postgres.lock_wait_observed(self._scope.run_id)
            except Exception:
                raise McpClientError(
                    "Order pool PostgreSQL evidence is unavailable."
                ) from None
            return {
                "benchmarkEvidenceId": "order-db-sessions",
                "databaseReachable": reachable,
                "runScopedSessionsPresent": sessions >= snapshot.capacity,
                "lockWaitObserved": lock_wait,
            }
        if name == "VerifyOrderDatabaseReachability":
            try:
                reachable = await self._postgres.database_reachable()
            except Exception:
                raise McpClientError(
                    "Order pool PostgreSQL evidence is unavailable."
                ) from None
            snapshot = await self._metrics_reader.snapshot(self._scope.run_id)
            return {
                "benchmarkEvidenceId": "order-pool-acquire-timeout",
                "databaseReachable": reachable,
                "businessProbeTimedOut": not snapshot.business_probe_success,
            }
        raise McpClientError("Order pool resident evidence tool is not allowed.")


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


def _fingerprint_sessions(values: Sequence[str] | frozenset[str]) -> frozenset[str]:
    return frozenset(
        hashlib.sha256(f"order-pool-session:{value}".encode()).hexdigest()
        for value in values
    )
