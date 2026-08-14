"""Dedicated Redis maxclients Live driver with exact run-scoped recovery."""

# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveVerification,
)
from super_ai.mcp_client import McpClientError, McpToolDefinition

SCENARIO_ID = "APY-LIVE-REDIS-MAXCLIENTS-001"


@dataclass(frozen=True, slots=True)
class RedisLiveConfig:
    """Connection boundary restricted to the isolated Compose fixture."""

    url: str = "redis://127.0.0.1:16379/0"

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if (
            parsed.scheme != "redis"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 16379
            or parsed.path not in {"", "/0"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Redis Live Eval must use redis://127.0.0.1:16379/0.")

    def client(self, *, name: str) -> Redis:
        return Redis.from_url(
            self.url,
            decode_responses=True,
            client_name=name,
            socket_connect_timeout=2,
            socket_timeout=2,
        )


@dataclass(slots=True)
class _RedisRun:
    control: Redis
    load_clients: list[Redis]
    maxclients: int
    rejected_before: int
    recovered: bool = False


class RedisMaxclientsScenarioDriver:
    """Fill only a dedicated Redis instance with named current-run clients."""

    def __init__(self, config: RedisLiveConfig) -> None:
        self._config = config
        self._runs: dict[str, _RedisRun] = {}

    async def preflight(self, identity: LiveRunIdentity) -> None:
        client = self._config.client(name="agentpy-live:preflight")
        try:
            if not await client.ping():
                raise RuntimeError("Redis Live Eval is unavailable.")
            config = cast(Mapping[str, object], await client.config_get("maxclients"))
            if _required_nonnegative_int(config.get("maxclients")) < 4:
                raise RuntimeError("Redis Live Eval maxclients is too small.")
            if await self._matching_clients(client, self._prefix(identity.run_id)):
                raise RuntimeError("Redis Live run has residual clients.")
        finally:
            await client.aclose()

    async def baseline(self, identity: LiveRunIdentity) -> None:
        control = self._config.client(name=f"agentpy-live:{identity.run_id}:control")
        try:
            if not await control.ping():
                raise RuntimeError("Redis control connection is unavailable.")
            config = cast(Mapping[str, object], await control.config_get("maxclients"))
            stats = cast(Mapping[str, object], await control.info("stats"))
            self._runs[identity.run_id] = _RedisRun(
                control=control,
                load_clients=[],
                maxclients=_required_nonnegative_int(config.get("maxclients")),
                rejected_before=_required_nonnegative_int(
                    stats.get("rejected_connections", 0)
                ),
            )
        except BaseException:
            await control.aclose()
            raise

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation:
        state = self._runs[identity.run_id]
        refused = False
        for index in range(state.maxclients + 2):
            client = self._config.client(
                name=f"{self._prefix(identity.run_id)}{index}"
            )
            try:
                await client.ping()
            except RedisConnectionError as exc:
                await client.aclose()
                if "max number of clients reached" not in str(exc).casefold():
                    raise
                refused = True
                break
            state.load_clients.append(client)
        clients = cast(Mapping[str, object], await state.control.info("clients"))
        stats = cast(Mapping[str, object], await state.control.info("stats"))
        names = await self.current_run_client_names(run_id=identity.run_id)
        connected = _required_nonnegative_int(clients.get("connected_clients"))
        rejected_delta = (
            _required_nonnegative_int(stats.get("rejected_connections", 0))
            - state.rejected_before
        )
        control_ping = bool(await state.control.ping())
        return LiveFaultObservation(
            scenario_id=SCENARIO_ID,
            checks=(
                LiveCheck("new_connection_rejected", refused and rejected_delta >= 1),
                LiveCheck("established_control_ping_succeeded", control_ping),
            ),
            safe_facts=(
                ("maxclients", state.maxclients),
                ("connectedClients", connected),
                ("rejectedConnectionsDelta", rejected_delta),
                ("currentRunClientCount", len(names)),
                ("controlPing", control_ping),
            ),
        )

    async def current_run_client_names(self, *, run_id: str) -> tuple[str, ...]:
        state = self._runs[run_id]
        clients = await self._matching_clients(state.control, self._prefix(run_id))
        return tuple(
            name
            for _, name in clients
            if name.startswith(self._prefix(run_id)) and ":load:" in name
        )

    async def close_clients(self, *, run_id: str, names: tuple[str, ...]) -> bool:
        prefix = self._prefix(run_id)
        if any(not name.startswith(prefix) or ":load:" not in name for name in names):
            raise ValueError("Only exact current-run Redis load clients may be closed.")
        state = self._runs.get(run_id)
        if state is None:
            return not names
        for expected_name in names:
            current = await self._matching_clients(state.control, prefix)
            match = next(
                (
                    (client_id, name)
                    for client_id, name in current
                    if name == expected_name
                ),
                None,
            )
            if match is None:
                raise RuntimeError("Redis client identity changed before scoped close.")
            killed = await state.control.execute_command("CLIENT", "KILL", "ID", match[0])
            if _required_nonnegative_int(killed) != 1:
                raise RuntimeError("Redis did not close the exact scoped client.")
        for client in state.load_clients:
            await client.aclose()
        state.load_clients.clear()
        state.recovered = True
        return True

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification:
        state = self._runs[identity.run_id]
        ping = bool(await state.control.ping())
        info = cast(Mapping[str, object], await state.control.info("clients"))
        connected = _required_nonnegative_int(info.get("connected_clients"))
        names = await self.current_run_client_names(run_id=identity.run_id)
        control_names = await self._matching_clients(
            state.control, f"agentpy-live:{identity.run_id}:control"
        )
        probe = self._config.client(name="agentpy-live:verification-probe")
        try:
            new_connection = bool(await probe.ping())
        finally:
            await probe.aclose()
        return LiveVerification(
            (
                LiveCheck("scoped_clients_closed", not names),
                LiveCheck("connected_clients_below_limit", connected < state.maxclients),
                LiveCheck("established_control_preserved", bool(control_names) and ping),
                LiveCheck("new_connection_succeeds", new_connection),
                LiveCheck("scoped_recovery_recorded", state.recovered),
            )
        )

    async def cleanup(self, identity: LiveRunIdentity) -> LiveCleanupResult:
        state = self._runs.pop(identity.run_id, None)
        if state is not None:
            for client in state.load_clients:
                await client.aclose()
            await state.control.aclose()
        audit = self._config.client(name="agentpy-live:cleanup-audit")
        try:
            prefix = f"agentpy-live:{identity.run_id}:"
            for client_id, _ in await self._matching_clients(audit, prefix):
                await audit.execute_command("CLIENT", "KILL", "ID", client_id)
            residual = await self._matching_clients(audit, prefix)
            return LiveCleanupResult(
                (
                    LiveCheck("scoped_redis_clients_removed", not residual),
                    LiveCheck("redis_cleanup_health", bool(await audit.ping())),
                )
            )
        finally:
            await audit.aclose()

    @staticmethod
    async def _matching_clients(
        client: Redis, prefix: str
    ) -> tuple[tuple[int, str], ...]:
        raw_clients = cast(Sequence[Mapping[str, object]], await client.client_list())
        matches: list[tuple[int, str]] = []
        for item in raw_clients:
            name = item.get("name")
            if isinstance(name, str) and name.startswith(prefix):
                matches.append((_required_nonnegative_int(item.get("id")), name))
        return tuple(matches)

    @staticmethod
    def _prefix(run_id: str) -> str:
        return f"agentpy-live:{run_id}:load:"


class RedisScopedCloseDriver(Protocol):
    async def current_run_client_names(self, *, run_id: str) -> tuple[str, ...]: ...

    async def close_clients(self, *, run_id: str, names: tuple[str, ...]) -> bool: ...


class RedisMaxclientsRecoveryService:
    """Close only exact load-client names owned by the current benchmark run."""

    def __init__(self, driver: RedisScopedCloseDriver) -> None:
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
        authorized = bool(
            decision is not None
            and decision.mechanism == "benchmark_clients_exhausted_maxclients"
            and observation.confirmed
        )
        names: tuple[str, ...] = ()
        executed = False
        if authorized:
            candidates = await self._driver.current_run_client_names(
                run_id=identity.run_id
            )
            prefix = f"agentpy-live:{identity.run_id}:load:"
            names = tuple(name for name in candidates if name.startswith(prefix))
            executed = bool(names) and await self._driver.close_clients(
                run_id=identity.run_id,
                names=names,
            )
        return LiveRecoveryRecord(
            action="close_current_run_benchmark_clients" if authorized else "none",
            target_ref="current_run_named_clients" if names else "none",
            expectation="executed_recovery",
            authorized=authorized,
            executed=executed,
            authorization_code="authorized" if authorized else "redis_decision_required",
        )


class RedisMaxclientsEvidenceMcpClient:
    """Expose a sanitized immutable Redis capacity snapshot without write tools."""

    def __init__(self, observation: LiveFaultObservation) -> None:
        if observation.scenario_id != SCENARIO_ID:
            raise ValueError("Redis maxclients evidence requires the matching scenario.")
        self._observation = observation

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        schema: dict[str, object] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return (
            McpToolDefinition(
                "InspectRedisServerInfo",
                "Read sanitized Redis client-capacity counters.",
                schema,
                "redis-maxclients-live",
            ),
            McpToolDefinition(
                "ListBenchmarkRedisClients",
                "Read only the count of current-run named benchmark clients.",
                schema,
                "redis-maxclients-live",
            ),
            McpToolDefinition(
                "VerifyRedisPing",
                "Read the established Redis control-connection health result.",
                schema,
                "redis-maxclients-live",
            ),
        )

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        if arguments:
            raise McpClientError("Redis Live evidence arguments are invalid.")
        if name == "InspectRedisServerInfo":
            return {
                "benchmarkEvidenceId": "redis-maxclients-capacity",
                "maxclients": self._observation.safe_fact("maxclients"),
                "connectedClients": self._observation.safe_fact("connectedClients"),
                "rejectedConnectionsDelta": self._observation.safe_fact(
                    "rejectedConnectionsDelta"
                ),
            }
        if name == "ListBenchmarkRedisClients":
            return {
                "benchmarkEvidenceId": "redis-scoped-clients",
                "currentRunClientCount": self._observation.safe_fact(
                    "currentRunClientCount"
                ),
                "namesRedacted": True,
            }
        if name == "VerifyRedisPing":
            return {
                "benchmarkEvidenceId": "redis-established-ping",
                "establishedConnectionHealthy": self._observation.safe_fact(
                    "controlPing"
                ),
            }
        raise McpClientError("Redis Live evidence tool is not allowed.")


def _required_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        raise RuntimeError("Redis returned an invalid integer.")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise RuntimeError("Redis returned an invalid integer.")
