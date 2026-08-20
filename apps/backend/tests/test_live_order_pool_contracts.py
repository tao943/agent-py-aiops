from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.aiops.investigation import (
    TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    build_investigator_capabilities,
)
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.domain import LiveRunIdentity
from super_ai.evaluation.live.order_pool_leak import (
    OrderPoolLeakScenarioDriver,
    OrderPoolLiveConfig,
    OrderPoolRecoveryService,
    OrderPoolRuntimeEvidenceMcpClient,
)
from super_ai.evaluation.live.scenarios import validate_run_id
from super_ai.mcp_client import McpClientError, McpToolDefinition


class FakeOrderApi:
    def __init__(self, *, pool_size: int = 3) -> None:
        self.pool_size = pool_size
        self.active_run: str | None = None
        self.generation = "gen-1"
        self.checked_out = 0
        self.waiter_observed = False
        self.events_by_run: dict[str, list[dict[str, str]]] = {}
        self.orders: set[str] = set()

    async def health(self) -> Mapping[str, object]:
        return {
            "status": "ok",
            "generation": self.generation,
            "activeRunId": self.active_run,
        }

    async def start_run(self, identity: LiveRunIdentity, fault_token: str) -> None:
        del fault_token
        run_id = identity.run_id
        if self.active_run not in {None, run_id}:
            raise RuntimeError("another_run_active")
        self.active_run = run_id
        self.orders.add(run_id)
        self.events_by_run.setdefault(run_id, [])

    async def inject_fault(
        self,
        identity: LiveRunIdentity,
        fault_token: str,
        request_id: str,
    ) -> None:
        del fault_token
        run_id = identity.run_id
        self.checked_out += 1
        self.events_by_run[run_id].extend(
            (
                self._event(run_id, request_id, "connection_checkout"),
                self._event(run_id, request_id, "order_update_failed"),
            )
        )

    async def probe(self, identity: LiveRunIdentity) -> bool:
        run_id = identity.run_id
        if self.checked_out >= self.pool_size:
            self.waiter_observed = True
            self.events_by_run[run_id].append(
                self._event(run_id, "business-probe", "pool_acquire_timeout")
            )
            return False
        self.events_by_run[run_id].extend(
            (
                self._event(run_id, "business-probe", "connection_checkout"),
                self._event(run_id, "business-probe", "connection_checkin"),
            )
        )
        return True

    async def state(self, identity: LiveRunIdentity) -> Mapping[str, object]:
        run_id = identity.run_id
        return {
            "runId": run_id,
            "generation": self.generation,
            "capacity": self.pool_size,
            "checkedOut": self.checked_out,
            "free": self.pool_size - self.checked_out,
            "waitersObserved": self.waiter_observed,
        }

    async def events(self, identity: LiveRunIdentity) -> Sequence[Mapping[str, str]]:
        return tuple(self.events_by_run[identity.run_id])

    async def clear(self, identity: LiveRunIdentity) -> None:
        run_id = identity.run_id
        self.active_run = None
        self.checked_out = 0
        self.orders.discard(run_id)

    def restart(self) -> None:
        self.generation = "gen-2"
        self.active_run = None
        self.checked_out = 0

    def _event(self, run_id: str, request_id: str, event: str) -> dict[str, str]:
        return {
            "run_id": run_id,
            "request_id": request_id,
            "event": event,
            "service": "order-api",
            "component": "order-api",
            "generation": self.generation,
            "timestamp": "2026-08-20T12:00:00+00:00",
            "level": "error" if event.endswith(("failed", "timeout")) else "info",
        }


class FakePostgresObserver:
    reachable = True
    lock_wait = False
    unrelated_sessions = 2

    def __init__(self, api: FakeOrderApi) -> None:
        self.api = api

    async def database_reachable(self) -> bool:
        return self.reachable

    async def run_scoped_session_count(self, run_id: str) -> int:
        del run_id
        return self.api.checked_out

    async def lock_wait_observed(self, run_id: str) -> bool:
        del run_id
        return self.lock_wait

    async def generation_session_count(self, run_id: str, generation: str) -> int:
        del run_id
        return self.api.checked_out if generation == self.api.generation else 0

    async def test_order_count(self, run_id: str) -> int:
        return int(run_id in self.api.orders)

    async def unrelated_session_count(self) -> int:
        return self.unrelated_sessions


class FakeRestarter:
    def __init__(self, api: FakeOrderApi) -> None:
        self.api = api
        self.calls: list[str] = []

    async def restart(self, service_name: str) -> None:
        self.calls.append(service_name)
        self.api.restart()


def _driver() -> tuple[OrderPoolLeakScenarioDriver, FakeOrderApi, FakePostgresObserver]:
    api = FakeOrderApi()
    postgres = FakePostgresObserver(api)
    config = OrderPoolLiveConfig(
        compose_file=Path(__file__).resolve().parents[3] / "infra" / "compose.yaml"
    )
    return OrderPoolLeakScenarioDriver(config, api=api, postgres=postgres), api, postgres


def _artifact(*, mechanism: str = "exception_path_connection_not_released") -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-LIVE-ORDER-POOL-LEAK-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            "order-api",
            mechanism,
            "fault_scoped_order_update_raises_after_checkout",
            ("checkout", "exception", "pool saturation", "request timeout"),
            ("order-pool-saturated", "cls-order-connection-lifecycle"),
            0.95,
        ),
        evidence=(),
        hypothesis_states=(),
        observation_decisions=(),
        tool_calls=(),
        plan_step_count=4,
        duration_ms=10,
        safety_events=(),
    )


@pytest.mark.asyncio
async def test_driver_confirms_pool_saturation_without_claiming_the_cause() -> None:
    driver, _, _ = _driver()
    identity = validate_run_id("order-pool-contract")
    await driver.preflight(identity)
    await driver.baseline(identity)
    observation = await driver.inject(identity)

    assert observation.confirmed is True
    assert observation.check_passed("pool_at_capacity")
    assert observation.check_passed("pool_free_zero")
    assert observation.check_passed("business_probe_timed_out")
    assert observation.check_passed("postgres_reachable")
    assert observation.check_passed("no_lock_wait")
    assert "primary_cause" not in str(observation.safe_facts).casefold()
    assert "connection_leak_confirmed" not in str(observation.safe_facts).casefold()


@pytest.mark.asyncio
async def test_recovery_restarts_only_owned_isolated_instance_once() -> None:
    driver, api, _ = _driver()
    restarter = FakeRestarter(api)
    recovery = OrderPoolRecoveryService(driver, restarter)
    identity = validate_run_id("run-1")
    await driver.preflight(identity)
    await driver.baseline(identity)
    observation = await driver.inject(identity)

    first = await recovery.recover(
        identity=identity,
        diagnostic_artifact=_artifact(),
        observation=observation,
    )
    second = await recovery.recover(
        identity=identity,
        diagnostic_artifact=_artifact(),
        observation=observation,
    )
    assert first.action == "restart_live_eval_order_api"
    assert first.target_ref == "current_run_order_api_instance"
    assert first.authorized and first.executed
    assert second.executed is False
    assert restarter.calls == ["live-eval-order-api"]
    assert (await driver.verify(identity)).passed


@pytest.mark.asyncio
async def test_recovery_denies_wrong_mechanism_and_nonexclusive_run() -> None:
    driver, api, _ = _driver()
    restarter = FakeRestarter(api)
    identity = validate_run_id("run-1")
    await driver.baseline(identity)
    observation = await driver.inject(identity)
    denied = await OrderPoolRecoveryService(driver, restarter).recover(
        identity=identity,
        diagnostic_artifact=_artifact(mechanism="traffic_exceeds_pool_capacity"),
        observation=observation,
    )
    assert denied.authorized is False
    assert restarter.calls == []


@pytest.mark.asyncio
async def test_runtime_evidence_is_read_only_partial_and_answer_isolated() -> None:
    driver, _, _ = _driver()
    identity = validate_run_id("run-1")
    await driver.baseline(identity)
    observation = await driver.inject(identity)
    client = OrderPoolRuntimeEvidenceMcpClient(observation)

    tools = {item.name: item for item in await client.discover_tools()}
    assert set(tools) == {
        "InspectOrderPoolState",
        "InspectOrderDatabaseSessions",
        "VerifyOrderDatabaseReachability",
    }
    assert all(item.input_schema["additionalProperties"] is False for item in tools.values())
    pool = await client.call_tool("InspectOrderPoolState", {})
    sessions = await client.call_tool("InspectOrderDatabaseSessions", {})
    serialized = f"{pool}{sessions}".casefold()
    assert "connection_leak" not in serialized
    assert "primary_cause" not in serialized
    with pytest.raises(McpClientError):
        await client.call_tool("InspectOrderPoolState", {"run_id": "other"})


@pytest.mark.asyncio
async def test_order_pool_runtime_and_cls_are_two_trusted_investigator_sources() -> None:
    driver, _, _ = _driver()
    identity = validate_run_id("run-1")
    await driver.baseline(identity)
    observation = await driver.inject(identity)
    runtime_tools = await OrderPoolRuntimeEvidenceMcpClient(observation).discover_tools()
    search_log = McpToolDefinition(
        "SearchLog",
        "Search one trusted run-scoped CLS window.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        "cls",
    )
    capabilities = build_investigator_capabilities(
        discovered_tools=(*runtime_tools, search_log),
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
        tool_policies={},
        retrieval_available=False,
        cls_available=True,
    )
    assert capabilities["runtime"].allowed_tools == {
        "InspectOrderPoolState",
        "InspectOrderDatabaseSessions",
        "VerifyOrderDatabaseReachability",
    }
    assert capabilities["log"].allowed_tools == {"SearchLog"}


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_and_audit_reports_only_counts() -> None:
    driver, _, _ = _driver()
    identity = validate_run_id("run-1")
    await driver.baseline(identity)
    await driver.inject(identity)
    await driver.cleanup(identity)
    await driver.cleanup(identity)
    audit = await driver.audit(identity)
    assert audit.clean
    assert audit.safe_payload() == {
        "orderApiHealthy": True,
        "activeRunCount": 0,
        "testOrderCount": 0,
        "oldGenerationSessionCount": 0,
        "clean": True,
    }
