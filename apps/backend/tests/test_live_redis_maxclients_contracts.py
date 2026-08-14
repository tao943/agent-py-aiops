from __future__ import annotations

import json

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.domain import LiveCheck, LiveFaultObservation
from super_ai.evaluation.live.redis_maxclients import (
    RedisLiveConfig,
    RedisMaxclientsEvidenceMcpClient,
    RedisMaxclientsRecoveryService,
    RedisMaxclientsScenarioDriver,
)
from super_ai.evaluation.live.scenarios import validate_run_id


def _observation() -> LiveFaultObservation:
    return LiveFaultObservation(
        "APY-LIVE-REDIS-MAXCLIENTS-001",
        (
            LiveCheck("new_connection_rejected", True),
            LiveCheck("established_control_ping_succeeded", True),
        ),
        safe_facts=(
            ("maxclients", 16),
            ("connectedClients", 16),
            ("rejectedConnectionsDelta", 1),
            ("currentRunClientCount", 15),
            ("controlPing", True),
        ),
    )


def _artifact() -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-LIVE-REDIS-MAXCLIENTS-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            "redis",
            "benchmark_clients_exhausted_maxclients",
            "current_run_named_clients_fill_the_dedicated_connection_limit",
            ("named clients fill maxclients", "new connections are rejected"),
            ("redis-maxclients-capacity", "redis-scoped-clients"),
            0.95,
        ),
        evidence=(),
        hypothesis_states=(),
        observation_decisions=(),
        tool_calls=(),
        plan_step_count=2,
        duration_ms=10,
        safety_events=(),
    )


class RecordingDriver:
    def __init__(self) -> None:
        self.names = (
            "agentpy-live:run-1:load:1",
            "agentpy-live:run-2:load:1",
            "application-client",
        )
        self.closed_names: list[str] = []

    async def current_run_client_names(self, *, run_id: str) -> tuple[str, ...]:
        del run_id
        return self.names

    async def close_clients(self, *, run_id: str, names: tuple[str, ...]) -> bool:
        del run_id
        self.closed_names.extend(names)
        return True


@pytest.mark.asyncio
async def test_cleanup_closes_only_exact_current_run_client_names() -> None:
    driver = RecordingDriver()
    record = await RedisMaxclientsRecoveryService(driver).recover(
        identity=validate_run_id("run-1"),
        diagnostic_artifact=_artifact(),
        observation=_observation(),
    )

    assert record.executed is True
    assert driver.closed_names == ["agentpy-live:run-1:load:1"]


@pytest.mark.asyncio
async def test_broad_or_unknown_redis_kill_is_denied_before_network_access() -> None:
    driver = RedisMaxclientsScenarioDriver(RedisLiveConfig())

    with pytest.raises(ValueError, match="current-run"):
        await driver.close_clients(run_id="run-1", names=("application-client",))


def test_redis_live_config_refuses_the_application_redis_port() -> None:
    with pytest.raises(ValueError, match="16379"):
        RedisLiveConfig(url="redis://127.0.0.1:6379/0")


@pytest.mark.asyncio
async def test_redis_component_client_is_read_only_and_answer_free() -> None:
    client = RedisMaxclientsEvidenceMcpClient(_observation())
    definitions = await client.discover_tools()

    assert {item.name for item in definitions} == {
        "InspectRedisServerInfo",
        "ListBenchmarkRedisClients",
        "VerifyRedisPing",
    }
    output = await client.call_tool("InspectRedisServerInfo", {})
    serialized = json.dumps(output)
    assert "maxclients" in serialized
    assert "run-1" not in serialized
    assert "CloseBenchmarkRedisClients" not in {item.name for item in definitions}
