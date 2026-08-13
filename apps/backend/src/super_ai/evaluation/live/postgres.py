"""PostgreSQL boundaries for the deterministic Docker Live lock experiment."""

# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, TypedDict

import asyncpg

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation.artifacts import RunArtifact
from super_ai.evaluation.live.domain import (
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveVerification,
)
from super_ai.evaluation.live.recovery import (
    PostgresRecoveryPlanner,
    PostgresRecoveryPolicy,
    PostgresSessionState,
)


class SafeSessionEvidence(TypedDict):
    blockerPid: int
    waiterPid: int
    waitEventType: str | None


class SafeLockGraphEvidence(TypedDict):
    blockerEdgeConfirmed: bool


class SafeProbeEvidence(TypedDict):
    succeeded: bool
    errorCategory: str | None


class SafeDockerLogEvidence(TypedDict):
    categories: list[str]


class SafePostgresEvidence(TypedDict):
    sessions: SafeSessionEvidence
    lockGraph: SafeLockGraphEvidence
    probe: SafeProbeEvidence
    dockerLog: SafeDockerLogEvidence


@dataclass(frozen=True, slots=True)
class PostgresConnectionConfig:
    """Explicit Live database connection values with a redacted password."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str = "agent_py_live_eval"

    def __post_init__(self) -> None:
        if self.database != "agent_py_live_eval":
            raise ValueError("Docker Live PostgreSQL config must target agent_py_live_eval.")

    async def connect(self, *, application_name: str) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            server_settings={"application_name": application_name},
        )


async def rollback_transaction_if_connection_open(
    connection: Any,
    transaction: Any,
) -> None:
    if connection.is_closed():
        return
    try:
        await transaction.rollback()
    except (asyncpg.PostgresError, ConnectionError):
        pass


@dataclass(slots=True)
class _RunConnections:
    blocker: asyncpg.Connection
    waiter: asyncpg.Connection
    blocker_transaction: Any
    waiter_task: asyncio.Task[str]
    blocker_pid: int
    waiter_pid: int
    unrelated_pids: frozenset[int]


class PostgresLockScenarioDriver:
    """Create, observe, verify and clean one deterministic row-lock wait."""

    def __init__(self, config: PostgresConnectionConfig, *, timeout_seconds: float = 1.0) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._runs: dict[str, _RunConnections] = {}

    async def preflight(self, identity: LiveRunIdentity) -> None:
        connection = await self._config.connect(application_name="agentpy-live:preflight")
        try:
            database = await connection.fetchval("SELECT current_database()")
            if database != "agent_py_live_eval":
                raise RuntimeError("Live database scope mismatch.")
            residual = await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE $1",
                f"agentpy-live:{identity.run_id}:%",
            )
            if residual:
                raise RuntimeError("Live run has residual sessions.")
        finally:
            await connection.close()

    async def baseline(self, identity: LiveRunIdentity) -> None:
        connection = await self._config.connect(application_name="agentpy-live:baseline")
        try:
            await connection.execute("CREATE SCHEMA IF NOT EXISTS live_eval")
            await connection.execute(
                f"CREATE TABLE IF NOT EXISTS live_eval.{identity.table_name} "
                "(id integer PRIMARY KEY, status text NOT NULL)"
            )
            await connection.execute(
                f"INSERT INTO live_eval.{identity.table_name} (id, status) VALUES (1, $1) "
                "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status",
                "ready",
            )
            await connection.execute(
                f"UPDATE live_eval.{identity.table_name} SET status = $1 WHERE id = 1",
                "baseline-ok",
                timeout=self._timeout_seconds,
            )
        finally:
            await connection.close()

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation:
        blocker = await self._config.connect(application_name=identity.blocker_application_name)
        waiter = await self._config.connect(application_name=identity.waiter_application_name)
        transaction = blocker.transaction()
        await transaction.start()
        await blocker.fetchval(
            f"SELECT id FROM live_eval.{identity.table_name} WHERE id = 1 FOR UPDATE"
        )
        blocker_pid = _required_pid(await blocker.fetchval("SELECT pg_backend_pid()"))
        waiter_pid = _required_pid(await waiter.fetchval("SELECT pg_backend_pid()"))
        observer = await self._config.connect(application_name="agentpy-live:observer")
        try:
            unrelated = frozenset(
                int(row["pid"])
                for row in await observer.fetch(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name NOT LIKE 'agentpy-live:%'"
                )
            )
        finally:
            await observer.close()
        waiter_task = asyncio.create_task(
            waiter.execute(
                f"UPDATE live_eval.{identity.table_name} SET status = $1 WHERE id = 1",
                "waiting",
            )
        )
        state = _RunConnections(
            blocker,
            waiter,
            transaction,
            waiter_task,
            blocker_pid,
            waiter_pid,
            unrelated,
        )
        self._runs[identity.run_id] = state
        for _ in range(100):
            observation = await self.observe(identity)
            if observation.confirmed:
                return observation
            await asyncio.sleep(0.02)
        return await self.observe(identity)

    async def observe(self, identity: LiveRunIdentity) -> LiveFaultObservation:
        state = self._runs[identity.run_id]
        connection = await self._config.connect(application_name="agentpy-live:observer")
        try:
            row = await connection.fetchrow(
                "SELECT wait_event_type, $1 = ANY(pg_blocking_pids($2)) AS edge "
                "FROM pg_stat_activity WHERE pid = $2",
                state.blocker_pid,
                state.waiter_pid,
            )
            return LiveFaultObservation(
                state.blocker_pid,
                state.waiter_pid,
                row is not None and row["wait_event_type"] == "Lock",
                row is not None and bool(row["edge"]),
            )
        finally:
            await connection.close()

    async def session_state(
        self, identity: LiveRunIdentity, target_pid: int
    ) -> tuple[PostgresSessionState | None, int]:
        state = self._runs[identity.run_id]
        connection = await self._config.connect(application_name="agentpy-live:executor")
        try:
            executor_pid = _required_pid(await connection.fetchval("SELECT pg_backend_pid()"))
            row = await connection.fetchrow(
                "SELECT pid, datname, application_name, backend_type, "
                "pg_blocking_pids($1) AS blocker_check "
                "FROM pg_stat_activity WHERE pid = $2",
                state.waiter_pid,
                target_pid,
            )
            if row is None:
                return None, executor_pid
            blockers = tuple(int(pid) for pid in row["blocker_check"])
            blocked_waiters = (state.waiter_pid,) if target_pid in blockers else ()
            return (
                PostgresSessionState(
                    pid=int(row["pid"]),
                    database=str(row["datname"]),
                    application_name=str(row["application_name"]),
                    backend_type=str(row["backend_type"]),
                    blocked_waiter_pids=blocked_waiters,
                ),
                executor_pid,
            )
        finally:
            await connection.close()

    async def terminate(self, identity: LiveRunIdentity, target_pid: int) -> bool:
        connection = await self._config.connect(application_name="agentpy-live:executor")
        try:
            return bool(
                await connection.fetchval(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE pid = $1 AND datname = 'agent_py_live_eval' "
                    "AND application_name = $2",
                    target_pid,
                    identity.blocker_application_name,
                )
            )
        finally:
            await connection.close()

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification:
        state = self._runs[identity.run_id]
        try:
            await asyncio.wait_for(asyncio.shield(state.waiter_task), timeout=2.0)
        except (asyncio.TimeoutError, asyncpg.PostgresError):
            pass
        connection = await self._config.connect(application_name="agentpy-live:verify")
        try:
            blocker_exists = bool(
                await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid = $1)",
                    state.blocker_pid,
                )
            )
            waiter_locking = bool(
                await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE pid = $1 AND wait_event_type = 'Lock')",
                    state.waiter_pid,
                )
            )
            await connection.execute(
                f"UPDATE live_eval.{identity.table_name} SET status = $1 WHERE id = 1",
                "recovered",
                timeout=self._timeout_seconds,
            )
            current_unrelated = frozenset(
                int(row["pid"])
                for row in await connection.fetch(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name NOT LIKE 'agentpy-live:%'"
                )
            )
            return LiveVerification(
                not blocker_exists,
                not waiter_locking,
                not waiter_locking,
                True,
                True,
                state.unrelated_pids <= current_unrelated,
            )
        finally:
            await connection.close()

    async def cleanup(self, identity: LiveRunIdentity) -> None:
        state = self._runs.pop(identity.run_id, None)
        if state is not None:
            if not state.waiter_task.done():
                state.waiter_task.cancel()
            await rollback_transaction_if_connection_open(
                state.blocker,
                state.blocker_transaction,
            )
            for connection in (state.blocker, state.waiter):
                if not connection.is_closed():
                    await connection.close()
        connection = await self._config.connect(application_name="agentpy-live:cleanup")
        try:
            rows = await connection.fetch(
                "SELECT pid FROM pg_stat_activity WHERE datname = 'agent_py_live_eval' "
                "AND application_name LIKE $1 AND pid <> pg_backend_pid()",
                f"agentpy-live:{identity.run_id}:%",
            )
            for row in rows:
                await connection.fetchval("SELECT pg_terminate_backend($1)", int(row["pid"]))
            await connection.execute(f"DROP TABLE IF EXISTS live_eval.{identity.table_name}")
        finally:
            await connection.close()


class PostgresLiveRecoveryService:
    """Plan, revalidate and execute one synthetic backend termination."""

    def __init__(self, driver: PostgresLockScenarioDriver) -> None:
        self._driver = driver
        self._planner = PostgresRecoveryPlanner()
        self._policy = PostgresRecoveryPolicy()

    async def recover(
        self,
        *,
        identity: LiveRunIdentity,
        diagnostic_artifact: object,
        observation: LiveFaultObservation,
    ) -> LiveRecoveryRecord:
        decision: RootCauseDecision | None = (
            diagnostic_artifact.decision
            if isinstance(diagnostic_artifact, RunArtifact)
            else None
        )
        intent = self._planner.plan(
            decision=decision,
            blocker_pids=(observation.blocker_pid,) if observation.blocker_edge_confirmed else (),
        )
        if intent is None:
            return LiveRecoveryRecord("none", 0, False, False, "intent_missing")
        state, executor_pid = await self._driver.session_state(identity, intent.target_pid)
        authorization = self._policy.authorize(
            identity=identity,
            intent=intent,
            state=state,
            injected_blocker_pid=observation.blocker_pid,
            waiter_pid=observation.waiter_pid,
            executor_pid=executor_pid,
        )
        executed = (
            await self._driver.terminate(identity, intent.target_pid)
            if authorization.allowed
            else False
        )
        return LiveRecoveryRecord(
            intent.action,
            intent.target_pid,
            authorization.allowed,
            executed,
            authorization.code,
        )


def safe_postgres_evidence(
    *,
    blocker_pid: int,
    waiter_pid: int,
    waiter_has_lock_event: bool,
    blocker_edge_confirmed: bool,
    probe_error: str | None,
    docker_log: str,
) -> SafePostgresEvidence:
    """Return content-free PostgreSQL evidence suitable for Agent and reports."""
    categories: list[str] = []
    lowered_log = docker_log.lower()
    for marker, category in (
        ("deadlock detected", "deadlock_detected"),
        ("canceling statement due to lock timeout", "lock_timeout"),
        ("canceling statement due to statement timeout", "statement_timeout"),
    ):
        if marker in lowered_log:
            categories.append(category)
    error_category = None
    if probe_error is not None:
        lowered_error = probe_error.lower()
        error_category = (
            "query_timeout"
            if "querycancel" in lowered_error or "timeout" in lowered_error
            else "query_failed"
        )
    return {
        "sessions": {
            "blockerPid": blocker_pid,
            "waiterPid": waiter_pid,
            "waitEventType": "Lock" if waiter_has_lock_event else None,
        },
        "lockGraph": {"blockerEdgeConfirmed": blocker_edge_confirmed},
        "probe": {"succeeded": probe_error is None, "errorCategory": error_category},
        "dockerLog": {"categories": categories},
    }


def _required_pid(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("PostgreSQL did not return a valid backend PID.")
    return value
