"""Deterministic PostgreSQL deadlock Live driver and scoped retry boundary."""

# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveVerification,
)
from super_ai.evaluation.live.postgres import PostgresConnectionConfig
from super_ai.mcp_client import McpClientError, McpToolDefinition

SCENARIO_ID = "APY-LIVE-PG-DEADLOCK-001"


@dataclass(slots=True)
class _DeadlockRun:
    victim_ref: str
    victim_order: tuple[int, int]
    cycle_confirmed: bool
    sqlstate: str
    retried: bool = False


class PostgresDeadlockScenarioDriver:
    """Create a real two-transaction deadlock and retain only safe audit facts."""

    def __init__(self, config: PostgresConnectionConfig) -> None:
        self._config = config
        self._runs: dict[str, _DeadlockRun] = {}

    async def preflight(self, identity: LiveRunIdentity) -> None:
        connection = await self._config.connect(application_name="agentpy-live:preflight")
        try:
            if await connection.fetchval("SELECT current_database()") != "agent_py_live_eval":
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
                f"CREATE TABLE IF NOT EXISTS live_eval.{self._table(identity)} "
                "(id integer PRIMARY KEY, status text NOT NULL)"
            )
            await connection.execute(
                f"INSERT INTO live_eval.{self._table(identity)} (id, status) "
                "VALUES (1, 'ready'), (2, 'ready') "
                "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status"
            )
        finally:
            await connection.close()

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation:
        connection_a = await self._config.connect(
            application_name=f"agentpy-live:{identity.run_id}:deadlock-a"
        )
        connection_b = await self._config.connect(
            application_name=f"agentpy-live:{identity.run_id}:deadlock-b"
        )
        transaction_a = connection_a.transaction()
        transaction_b = connection_b.transaction()
        task_a: asyncio.Task[str] | None = None
        task_b: asyncio.Task[str] | None = None
        try:
            await transaction_a.start()
            await transaction_b.start()
            table = self._table(identity)
            await connection_a.execute(
                f"UPDATE live_eval.{table} SET status = 'a-holds' WHERE id = 1"
            )
            await connection_b.execute(
                f"UPDATE live_eval.{table} SET status = 'b-holds' WHERE id = 2"
            )
            pid_a = _required_pid(await connection_a.fetchval("SELECT pg_backend_pid()"))
            pid_b = _required_pid(await connection_b.fetchval("SELECT pg_backend_pid()"))
            task_a = asyncio.create_task(
                connection_a.execute(
                    f"UPDATE live_eval.{table} SET status = 'a-second' WHERE id = 2"
                )
            )
            await self._wait_for_edge(waiter_pid=pid_a, blocker_pid=pid_b)
            task_b = asyncio.create_task(
                connection_b.execute(
                    f"UPDATE live_eval.{table} SET status = 'b-second' WHERE id = 1"
                )
            )
            cycle_confirmed = await self._wait_for_cycle(pid_a, pid_b)
            done, pending = await asyncio.wait(
                {task_a, task_b},
                timeout=4.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            victim_task, victim_ref, victim_order, victim_transaction = self._victim(
                done,
                task_a=task_a,
                task_b=task_b,
                transaction_a=transaction_a,
                transaction_b=transaction_b,
            )
            sqlstate = _postgres_sqlstate(victim_task.exception())
            await victim_transaction.rollback()
            for survivor_task in pending:
                await asyncio.wait_for(survivor_task, timeout=2.0)
            if victim_ref == "transaction-a":
                await transaction_b.commit()
            else:
                await transaction_a.commit()
            self._runs[identity.run_id] = _DeadlockRun(
                victim_ref=victim_ref,
                victim_order=victim_order,
                cycle_confirmed=cycle_confirmed,
                sqlstate=sqlstate,
            )
            return self._observation(self._runs[identity.run_id])
        finally:
            for task in (task_a, task_b):
                if task is not None and not task.done():
                    task.cancel()
            for connection in (connection_a, connection_b):
                if not connection.is_closed():
                    await connection.close()

    async def retry_aborted_transaction(self, *, run_id: str, target_ref: str) -> bool:
        state = self._runs.get(run_id)
        if state is None or state.victim_ref != target_ref or state.sqlstate != "40P01":
            return False
        if state.retried:
            return True
        identity = _identity_for_existing_run(run_id)
        connection = await self._config.connect(
            application_name=f"agentpy-live:{run_id}:deadlock-retry"
        )
        transaction = connection.transaction()
        try:
            await transaction.start()
            for row_id in state.victim_order:
                await connection.execute(
                    f"UPDATE live_eval.{self._table(identity)} SET status = $1 WHERE id = $2",
                    f"retried:{target_ref}",
                    row_id,
                )
            await transaction.commit()
            state.retried = True
            return True
        except BaseException:
            if not connection.is_closed():
                await transaction.rollback()
            raise
        finally:
            await connection.close()

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification:
        state = self._runs[identity.run_id]
        connection = await self._config.connect(application_name="agentpy-live:verify")
        try:
            healthy = await connection.fetchval("SELECT 1") == 1
            residual = _required_count(
                await connection.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE $1",
                    f"agentpy-live:{identity.run_id}:%",
                )
            )
            retried_rows = _required_count(
                await connection.fetchval(
                    f"SELECT count(*) FROM live_eval.{self._table(identity)} "
                    "WHERE status = $1",
                    f"retried:{state.victim_ref}",
                )
            )
            return LiveVerification(
                (
                    LiveCheck("victim_retry_succeeded", state.retried),
                    LiveCheck("both_business_rows_updated", retried_rows == 2),
                    LiveCheck("no_open_current_run_transaction", residual == 0),
                    LiveCheck("postgres_healthy", healthy),
                )
            )
        finally:
            await connection.close()

    async def cleanup(self, identity: LiveRunIdentity) -> LiveCleanupResult:
        self._runs.pop(identity.run_id, None)
        connection = await self._config.connect(application_name="agentpy-live:cleanup")
        try:
            rows = await connection.fetch(
                "SELECT pid FROM pg_stat_activity WHERE application_name LIKE $1 "
                "AND pid <> pg_backend_pid()",
                f"agentpy-live:{identity.run_id}:%",
            )
            for row in rows:
                await connection.fetchval("SELECT pg_terminate_backend($1)", int(row["pid"]))
            await connection.execute(
                f"DROP TABLE IF EXISTS live_eval.{self._table(identity)}"
            )
            residual_sessions = _required_count(
                await connection.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE $1",
                    f"agentpy-live:{identity.run_id}:%",
                )
            )
            residual_tables = _required_count(
                await connection.fetchval(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'live_eval' "
                    "AND tablename = $1",
                    self._table(identity),
                )
            )
            return LiveCleanupResult(
                (
                    LiveCheck("scoped_sessions_removed", residual_sessions == 0, "cleanup_audit"),
                    LiveCheck("scoped_fixture_removed", residual_tables == 0, "cleanup_audit"),
                )
            )
        finally:
            await connection.close()

    async def _wait_for_edge(self, *, waiter_pid: int, blocker_pid: int) -> None:
        for _ in range(100):
            connection = await self._config.connect(application_name="agentpy-live:observer")
            try:
                confirmed = bool(
                    await connection.fetchval(
                        "SELECT $1 = ANY(pg_blocking_pids($2))",
                        blocker_pid,
                        waiter_pid,
                    )
                )
            finally:
                await connection.close()
            if confirmed:
                return
            await asyncio.sleep(0.01)
        raise RuntimeError("Deadlock first blocking edge was not observed.")

    async def _wait_for_cycle(self, pid_a: int, pid_b: int) -> bool:
        for _ in range(100):
            connection = await self._config.connect(application_name="agentpy-live:observer")
            try:
                confirmed = bool(
                    await connection.fetchval(
                        "SELECT $1 = ANY(pg_blocking_pids($2)) "
                        "AND $2 = ANY(pg_blocking_pids($1))",
                        pid_a,
                        pid_b,
                    )
                )
            finally:
                await connection.close()
            if confirmed:
                return True
            await asyncio.sleep(0.01)
        return False

    @staticmethod
    def _victim(
        done: set[asyncio.Task[str]],
        *,
        task_a: asyncio.Task[str],
        task_b: asyncio.Task[str],
        transaction_a: Any,
        transaction_b: Any,
    ) -> tuple[asyncio.Task[str], str, tuple[int, int], Any]:
        for task, ref, order, transaction in (
            (task_a, "transaction-a", (1, 2), transaction_a),
            (task_b, "transaction-b", (2, 1), transaction_b),
        ):
            if task in done:
                if _postgres_sqlstate(task.exception()) == "40P01":
                    return task, ref, order, transaction
        raise RuntimeError("PostgreSQL did not abort exactly one deadlock victim.")

    @staticmethod
    def _observation(state: _DeadlockRun) -> LiveFaultObservation:
        return LiveFaultObservation(
            scenario_id=SCENARIO_ID,
            checks=(
                LiveCheck(
                    "postgres_40p01",
                    state.sqlstate == "40P01",
                    "InspectPostgresDeadlockAudit",
                ),
                LiveCheck(
                    "deadlock_cycle_audited",
                    state.cycle_confirmed,
                    "InspectPostgresDeadlockAudit",
                ),
            ),
            safe_facts=(
                ("sqlstate", state.sqlstate),
                ("cycleLength", 2),
                ("victimRef", state.victim_ref),
                ("postgresHealthy", True),
                ("transactionAFirstResource", "order_row_1"),
                ("transactionASecondResource", "order_row_2"),
                ("transactionBFirstResource", "order_row_2"),
                ("transactionBSecondResource", "order_row_1"),
            ),
        )

    @staticmethod
    def _table(identity: LiveRunIdentity) -> str:
        return f"deadlock_target_{identity.run_token}"


class DeadlockRetryDriver(Protocol):
    async def retry_aborted_transaction(self, *, run_id: str, target_ref: str) -> bool: ...


class PostgresDeadlockRecoveryService:
    """Authorize only a replay of the recorded current-run 40P01 victim."""

    def __init__(self, driver: DeadlockRetryDriver) -> None:
        self._driver = driver

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
        target = observation.safe_fact("victimRef")
        authorized = bool(
            decision is not None
            and decision.mechanism == "opposite_order_transaction_deadlock"
            and observation.confirmed
            and observation.safe_fact("sqlstate") == "40P01"
            and isinstance(target, str)
            and target in {"transaction-a", "transaction-b"}
        )
        executed = (
            await self._driver.retry_aborted_transaction(
                run_id=identity.run_id,
                target_ref=target,
            )
            if authorized and isinstance(target, str)
            else False
        )
        return LiveRecoveryRecord(
            action="retry_aborted_benchmark_transaction" if authorized else "none",
            target_ref=target if isinstance(target, str) else "none",
            expectation="executed_recovery",
            authorized=authorized,
            executed=executed,
            authorization_code="authorized" if authorized else "deadlock_decision_required",
        )


class PostgresDeadlockEvidenceMcpClient:
    """Expose only sanitized historical deadlock facts to the Agent."""

    def __init__(self, observation: LiveFaultObservation) -> None:
        if observation.scenario_id != SCENARIO_ID:
            raise ValueError("PostgreSQL deadlock evidence requires the matching scenario.")
        self._observation = observation

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        schema: dict[str, object] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return tuple(
            McpToolDefinition(name, description, schema, "postgres-deadlock-live")
            for name, description in (
                ("InspectPostgresDeadlockAudit", "Read the sanitized deadlock cycle audit."),
                (
                    "InspectPostgresTransactionResult",
                    "Read the sanitized aborted transaction result.",
                ),
                ("VerifyPostgresHealth", "Read the independent PostgreSQL health result."),
            )
        )

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        if arguments:
            raise McpClientError("PostgreSQL deadlock evidence arguments are invalid.")
        if name == "InspectPostgresDeadlockAudit":
            return {
                "benchmarkEvidenceId": "postgres-deadlock-cycle",
                "sqlstate": self._observation.safe_fact("sqlstate"),
                "cycleDetected": self._observation.check_passed("deadlock_cycle_audited"),
                "cycleLength": self._observation.safe_fact("cycleLength"),
                "transactionAFirstResource": self._observation.safe_fact(
                    "transactionAFirstResource"
                ),
                "transactionASecondResource": self._observation.safe_fact(
                    "transactionASecondResource"
                ),
                "transactionBFirstResource": self._observation.safe_fact(
                    "transactionBFirstResource"
                ),
                "transactionBSecondResource": self._observation.safe_fact(
                    "transactionBSecondResource"
                ),
            }
        if name == "InspectPostgresTransactionResult":
            return {
                "benchmarkEvidenceId": "postgres-deadlock-40p01",
                "victimRef": self._observation.safe_fact("victimRef"),
                "aborted": self._observation.check_passed("postgres_40p01"),
                "retryEligible": self._observation.confirmed,
            }
        if name == "VerifyPostgresHealth":
            return {
                "benchmarkEvidenceId": "postgres-deadlock-health",
                "postgresHealthy": self._observation.safe_fact("postgresHealthy"),
            }
        raise McpClientError("PostgreSQL deadlock evidence tool is not allowed.")


def _required_pid(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("PostgreSQL returned an invalid backend identity.")
    return value


def _required_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("PostgreSQL returned an invalid count.")
    return value


def _postgres_sqlstate(error: BaseException | None) -> str:
    if not isinstance(error, asyncpg.PostgresError):
        return ""
    value = getattr(error, "sqlstate", None)
    return value if isinstance(value, str) else ""


def _identity_for_existing_run(run_id: str) -> LiveRunIdentity:
    from super_ai.evaluation.live.scenarios import validate_run_id

    return validate_run_id(run_id)
