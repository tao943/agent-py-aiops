# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnusedFunction=false

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

_ISOLATED_DATABASE = "agent_py_live_eval"
_MAX_EVENTS = 256
_ORDER_DDL = """
CREATE TABLE IF NOT EXISTS live_eval_orders (
    run_id text NOT NULL,
    order_id text NOT NULL,
    status text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, order_id)
)
"""
_UPSERT_ORDER = """
INSERT INTO live_eval_orders (run_id, order_id, status)
VALUES ($1, 'order-1', 'ready')
ON CONFLICT (run_id, order_id)
DO UPDATE SET status = EXCLUDED.status, updated_at = now()
"""
_UPDATE_ORDER = """
UPDATE live_eval_orders
SET status = 'updated', updated_at = now()
WHERE run_id = $1
"""
_DELETE_ORDER = "DELETE FROM live_eval_orders WHERE run_id = $1"


class OrderApiConfigurationError(RuntimeError):
    pass


class OrderApiAccessError(RuntimeError):
    pass


class OrderApiConflictError(RuntimeError):
    pass


class ConnectionBoundary(Protocol):
    async def execute(self, query: str, *arguments: object) -> str: ...


class PoolBoundary(Protocol):
    async def acquire(self, *, timeout: float) -> ConnectionBoundary: ...

    async def release(self, connection: ConnectionBoundary) -> None: ...

    def get_size(self) -> int: ...

    def get_idle_size(self) -> int: ...


class AsyncpgPoolAdapter:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def acquire(self, *, timeout: float) -> ConnectionBoundary:
        connection: ConnectionBoundary = await self._pool.acquire(timeout=timeout)
        return connection

    async def release(self, connection: ConnectionBoundary) -> None:
        await self._pool.release(connection)

    def get_size(self) -> int:
        return int(self._pool.get_size())

    def get_idle_size(self) -> int:
        return int(self._pool.get_idle_size())

    async def close(self) -> None:
        await self._pool.close()


@dataclass(frozen=True, slots=True)
class OrderApiSettings:
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str = field(repr=False)
    control_token: str = field(repr=False)
    postgres_database: str = _ISOLATED_DATABASE
    pool_size: int = 3


def load_settings_from_environment() -> OrderApiSettings:
    required = {
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST"),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT"),
        "POSTGRES_USER": os.getenv("POSTGRES_USER"),
        "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD"),
        "POSTGRES_DB": os.getenv("POSTGRES_DB"),
        "LIVE_ORDER_API_CONTROL_TOKEN": os.getenv("LIVE_ORDER_API_CONTROL_TOKEN"),
    }
    if any(not value for value in required.values()):
        raise OrderApiConfigurationError("missing_required_environment")
    if required["POSTGRES_DB"] != _ISOLATED_DATABASE:
        raise OrderApiConfigurationError("database_not_isolated")
    try:
        port = int(required["POSTGRES_PORT"] or "")
        pool_size = int(os.getenv("LIVE_ORDER_API_POOL_SIZE", "3"))
    except ValueError as exc:
        raise OrderApiConfigurationError("numeric_environment_invalid") from exc
    if not 1 <= pool_size <= 16:
        raise OrderApiConfigurationError("pool_size_out_of_range")
    return OrderApiSettings(
        postgres_host=required["POSTGRES_HOST"] or "",
        postgres_port=port,
        postgres_user=required["POSTGRES_USER"] or "",
        postgres_password=required["POSTGRES_PASSWORD"] or "",
        postgres_database=_ISOLATED_DATABASE,
        control_token=required["LIVE_ORDER_API_CONTROL_TOKEN"] or "",
        pool_size=pool_size,
    )


class OrderApiRuntime:
    def __init__(
        self,
        *,
        pool: PoolBoundary,
        generation: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._generation = generation
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._active_run_id: str | None = None
        self._fault_token: str | None = None
        self._held_connections: list[ConnectionBoundary] = []
        self._events: list[dict[str, str]] = []
        self._waiters_observed = False

    async def initialize(self) -> None:
        connection = await self._pool.acquire(timeout=5.0)
        try:
            await connection.execute(_ORDER_DDL)
        finally:
            await self._pool.release(connection)

    async def start_run(self, run_id: str, fault_token: str) -> None:
        _validate_identifier(run_id, "run_id_invalid")
        if not fault_token:
            raise OrderApiAccessError("fault_token_invalid")
        if self._active_run_id is not None and self._active_run_id != run_id:
            raise OrderApiConflictError("another_run_active")
        if self._active_run_id == run_id:
            if not secrets.compare_digest(self._fault_token or "", fault_token):
                raise OrderApiAccessError("fault_token_invalid")
            return
        connection = await self._pool.acquire(timeout=5.0)
        try:
            await connection.execute(_UPSERT_ORDER, run_id)
        finally:
            await self._pool.release(connection)
        self._active_run_id = run_id
        self._fault_token = fault_token
        self._waiters_observed = False
        self._events.clear()

    async def baseline_update(self, run_id: str, request_id: str) -> None:
        if not await self.probe(run_id, timeout_seconds=1.0, request_id=request_id):
            raise OrderApiConflictError("baseline_probe_timed_out")

    async def execute_fault(self, run_id: str, fault_token: str, request_id: str) -> None:
        self._require_active_run(run_id)
        _validate_identifier(request_id, "request_id_invalid")
        if not secrets.compare_digest(self._fault_token or "", fault_token):
            raise OrderApiAccessError("fault_token_invalid")
        connection = await self._pool.acquire(timeout=1.0)
        await connection.execute(
            "SELECT set_config('application_name', $1, false)", _app_name(run_id)
        )
        self._record(run_id, request_id, "connection_checkout", "info")
        await connection.execute(_UPDATE_ORDER, run_id)
        self._record(run_id, request_id, "order_update_failed", "error")
        self._held_connections.append(connection)

    async def probe(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
        request_id: str = "business-probe",
    ) -> bool:
        self._require_active_run(run_id)
        _validate_identifier(request_id, "request_id_invalid")
        try:
            connection = await self._pool.acquire(timeout=timeout_seconds)
        except TimeoutError:
            self._waiters_observed = True
            self._record(run_id, request_id, "pool_acquire_timeout", "error")
            return False
        self._record(run_id, request_id, "connection_checkout", "info")
        try:
            await connection.execute(_UPDATE_ORDER, run_id)
            return True
        finally:
            await self._pool.release(connection)
            self._record(run_id, request_id, "connection_checkin", "info")

    async def state(self, run_id: str) -> dict[str, object]:
        self._require_active_run(run_id)
        return {
            "runId": run_id,
            "generation": self._generation,
            "capacity": self._pool.get_size(),
            "checkedOut": len(self._held_connections),
            "free": self._pool.get_idle_size(),
            "waitersObserved": self._waiters_observed,
        }

    async def events(self, run_id: str) -> tuple[dict[str, str], ...]:
        self._require_active_run(run_id)
        return tuple(dict(item) for item in self._events)

    @property
    def generation(self) -> str:
        return self._generation

    async def clear_run(self, run_id: str) -> None:
        if self._active_run_id is None:
            return
        self._require_active_run(run_id)
        held, self._held_connections = self._held_connections, []
        for connection in held:
            await self._pool.release(connection)
        connection = await self._pool.acquire(timeout=5.0)
        try:
            await connection.execute(_DELETE_ORDER, run_id)
        finally:
            await self._pool.release(connection)
        self._active_run_id = None
        self._fault_token = None
        self._waiters_observed = False
        self._events.clear()

    def _require_active_run(self, run_id: str) -> None:
        if self._active_run_id != run_id:
            raise OrderApiAccessError("run_not_active")

    def _record(self, run_id: str, request_id: str, event: str, level: str) -> None:
        self._events.append(
            {
                "run_id": run_id,
                "request_id": request_id,
                "event": event,
                "service": "order-api",
                "component": "order-api",
                "generation": self._generation,
                "timestamp": self._now().isoformat(),
                "level": level,
            }
        )
        if len(self._events) > _MAX_EVENTS:
            del self._events[: len(self._events) - _MAX_EVENTS]


class StartRunRequest(BaseModel):
    run_id: str
    fault_token: str


class FaultRequest(BaseModel):
    fault_token: str
    request_id: str


class ProbeRequest(BaseModel):
    request_id: str = "business-probe"
    timeout_seconds: float = 0.5


def create_app() -> FastAPI:
    settings = load_settings_from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        raw_pool = await asyncpg.create_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            database=settings.postgres_database,
            min_size=0,
            max_size=settings.pool_size,
        )
        pool = AsyncpgPoolAdapter(raw_pool)
        runtime = OrderApiRuntime(pool=pool, generation=uuid4().hex)
        await runtime.initialize()
        app.state.runtime = runtime
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="AgentPy Live Eval Order API", lifespan=lifespan)
    app.state.settings = settings

    def authorize(control_token: str | None) -> None:
        if control_token is None or not secrets.compare_digest(
            control_token, settings.control_token
        ):
            raise HTTPException(status_code=403, detail="control_token_invalid")

    @app.get("/health")
    async def health() -> dict[str, object]:
        runtime: OrderApiRuntime = app.state.runtime
        return {"status": "ok", "generation": runtime.generation}

    @app.post("/internal/runs/start")
    async def start_run(
        request: StartRunRequest,
        x_live_control_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize(x_live_control_token)
        await app.state.runtime.start_run(request.run_id, request.fault_token)
        return {"status": "started"}

    @app.post("/internal/runs/{run_id}/fault")
    async def inject_fault(
        run_id: str,
        request: FaultRequest,
        x_live_control_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize(x_live_control_token)
        await app.state.runtime.execute_fault(run_id, request.fault_token, request.request_id)
        return {"status": "injected"}

    @app.post("/internal/runs/{run_id}/probe")
    async def probe(
        run_id: str,
        request: ProbeRequest,
        x_live_control_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_live_control_token)
        passed = await app.state.runtime.probe(
            run_id,
            timeout_seconds=request.timeout_seconds,
            request_id=request.request_id,
        )
        return {"passed": passed}

    @app.get("/internal/runs/{run_id}/state")
    async def state(
        run_id: str,
        x_live_control_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_live_control_token)
        return await app.state.runtime.state(run_id)

    @app.get("/internal/runs/{run_id}/events")
    async def events(
        run_id: str,
        x_live_control_token: str | None = Header(default=None),
    ) -> tuple[dict[str, str], ...]:
        authorize(x_live_control_token)
        return await app.state.runtime.events(run_id)

    @app.delete("/internal/runs/{run_id}")
    async def clear_run(
        run_id: str,
        x_live_control_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize(x_live_control_token)
        await app.state.runtime.clear_run(run_id)
        return {"status": "cleared"}

    return app


def _validate_identifier(value: str, error: str) -> None:
    safe_characters = all(character.isalnum() or character in "-_" for character in value)
    if not value or len(value) > 96 or not safe_characters:
        raise OrderApiAccessError(error)


def _app_name(run_id: str) -> str:
    return f"agentpy-order-api-{run_id}"
