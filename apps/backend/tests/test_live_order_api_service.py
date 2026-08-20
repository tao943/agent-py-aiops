from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
ORDER_API_PATH = ROOT / "infra" / "live-eval" / "order_api.py"


def test_order_api_idle_pool_connections_have_owned_application_name() -> None:
    source = ORDER_API_PATH.read_text(encoding="utf-8")

    assert 'server_settings={"application_name": "agentpy-order-api:idle"}' in source


def _load_order_api() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_eval_order_api", ORDER_API_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("order_api_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fault_session_application_name_is_bounded_for_maximum_run_id() -> None:
    module = _load_order_api()
    run_id = "r" * 64
    generation = "0123456789abcdef" * 2

    application_name = module._session_application_name(run_id, generation)

    assert application_name == "agentpy-order-api:c9ea6f42c8efcb14:0123456789abcdef"
    assert len(application_name.encode("ascii")) == 51
    assert len(application_name.encode("ascii")) <= 63


def test_fault_session_application_name_separates_runs_and_generations() -> None:
    module = _load_order_api()

    assert module._session_application_name("run-1", "generation-a") != (
        module._session_application_name("run-2", "generation-a")
    )
    assert module._session_application_name("run-1", "generation-a") != (
        module._session_application_name("run-1", "generation-b")
    )


class FakeConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *arguments: object) -> str:
        self.executions.append((query, arguments))
        return "UPDATE 1"


class FakePool:
    def __init__(self, max_size: int = 3) -> None:
        self.max_size = max_size
        self.checked_out: list[FakeConnection] = []
        self.released: list[FakeConnection] = []

    async def acquire(self, *, timeout: float) -> FakeConnection:
        del timeout
        if len(self.checked_out) >= self.max_size:
            raise TimeoutError
        connection = FakeConnection()
        self.checked_out.append(connection)
        return connection

    async def release(self, connection: FakeConnection) -> None:
        self.checked_out.remove(connection)
        self.released.append(connection)

    def get_size(self) -> int:
        return self.max_size

    def get_idle_size(self) -> int:
        return self.max_size - len(self.checked_out)


def _fixed_now() -> datetime:
    return datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _runtime(module: ModuleType, pool: FakePool) -> Any:
    runtime_type: Callable[..., Any] = module.OrderApiRuntime
    return runtime_type(pool=pool, generation="gen-1", now=_fixed_now)


@pytest.mark.asyncio
async def test_fault_path_keeps_checked_out_connection_and_records_real_lifecycle() -> None:
    module = _load_order_api()
    pool = FakePool(max_size=3)
    runtime = _runtime(module, pool)

    await runtime.start_run("run-1", "fault-token")
    await runtime.execute_fault("run-1", "fault-token", "request-1")

    state = await runtime.state("run-1")
    events = await runtime.events("run-1")
    assert state["checkedOut"] == 1
    assert [item["event"] for item in events[-2:]] == [
        "connection_checkout",
        "order_update_failed",
    ]
    assert "connection_checkin" not in {item["event"] for item in events[-2:]}
    assert "fault-token" not in str(events)
    assert pool.checked_out[0].executions[-1][1] == ("run-1",)


@pytest.mark.asyncio
async def test_normal_probe_returns_connection_and_updates_run_scoped_order() -> None:
    module = _load_order_api()
    pool = FakePool(max_size=3)
    runtime = _runtime(module, pool)
    await runtime.start_run("run-1", "fault-token")

    assert await runtime.probe("run-1", timeout_seconds=0.1, request_id="probe-1") is True
    assert pool.checked_out == []
    assert pool.released[-1].executions[-1][1] == ("run-1",)
    assert [item["event"] for item in await runtime.events("run-1")][-2:] == [
        "connection_checkout",
        "connection_checkin",
    ]


@pytest.mark.asyncio
async def test_saturated_pool_records_timeout_without_leaking_sensitive_state() -> None:
    module = _load_order_api()
    pool = FakePool(max_size=1)
    runtime = _runtime(module, pool)
    await runtime.start_run("run-1", "fault-token")
    await runtime.execute_fault("run-1", "fault-token", "request-1")

    assert await runtime.probe("run-1", timeout_seconds=0.1, request_id="probe-1") is False
    state = await runtime.state("run-1")
    assert state == {
        "runId": "run-1",
        "generation": "gen-1",
        "capacity": 1,
        "checkedOut": 1,
        "free": 0,
        "waitersObserved": True,
    }
    assert (await runtime.events("run-1"))[-1]["event"] == "pool_acquire_timeout"
    assert "fault-token" not in str(state)


@pytest.mark.asyncio
async def test_run_scope_token_and_cleanup_are_fail_closed_and_idempotent() -> None:
    module = _load_order_api()
    pool = FakePool(max_size=2)
    runtime = _runtime(module, pool)
    await runtime.start_run("run-1", "fault-token")
    with pytest.raises(module.OrderApiAccessError, match="fault_token_invalid"):
        await runtime.execute_fault("run-1", "wrong-token", "request-1")
    with pytest.raises(module.OrderApiConflictError, match="another_run_active"):
        await runtime.start_run("run-2", "other-token")

    await runtime.execute_fault("run-1", "fault-token", "request-1")
    await runtime.clear_run("run-1")
    await runtime.clear_run("run-1")
    assert pool.checked_out == []


def test_factory_loads_environment_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_order_api()
    values = {
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "agent_py",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "agent_py_live_eval",
        "LIVE_ORDER_API_CONTROL_TOKEN": "control-token",
        "LIVE_ORDER_API_POOL_SIZE": "3",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    app = module.create_app()
    assert app.state.settings.postgres_database == "agent_py_live_eval"
    assert "secret" not in repr(app.state.settings)
    monkeypatch.delenv("LIVE_ORDER_API_CONTROL_TOKEN")
    with pytest.raises(module.OrderApiConfigurationError, match="missing_required_environment"):
        module.create_app()


def test_factory_rejects_non_isolated_database(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_order_api()
    for key, value in {
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "agent_py",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "production",
        "LIVE_ORDER_API_CONTROL_TOKEN": "control-token",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(module.OrderApiConfigurationError, match="database_not_isolated"):
        module.create_app()
