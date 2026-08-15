from __future__ import annotations

# pyright: reportUnknownMemberType=false
import os

import pytest
from redis.asyncio import Redis

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.redis_maxclients import (
    RedisLiveConfig,
    RedisMaxclientsRecoveryService,
    RedisMaxclientsScenarioDriver,
)
from super_ai.evaluation.live.scenarios import validate_run_id

pytestmark = pytest.mark.live_docker


@pytest.mark.asyncio
async def test_real_redis_refusal_and_exact_scoped_client_recovery() -> None:
    config = RedisLiveConfig(
        url=os.getenv("LIVE_REDIS_URL", "redis://127.0.0.1:16379/0")
    )
    identity = validate_run_id("docker-redis-maxclients-contract")
    driver = RedisMaxclientsScenarioDriver(config)
    unrelated: Redis = config.client(name="application-client-preserved")
    try:
        assert await unrelated.ping() is True
        await driver.preflight(identity)
        await driver.baseline(identity)
        observation = await driver.inject(identity)

        assert observation.confirmed is True
        assert observation.safe_fact("rejectedConnectionsDelta") == 1
        artifact = RunArtifact(
            scenario_id="APY-LIVE-REDIS-MAXCLIENTS-001",
            mode="live",
            completed=True,
            report_produced=True,
            decision=RootCauseDecision(
                component="redis",
                mechanism="benchmark_clients_exhausted_maxclients",
                trigger="current_run_named_clients_fill_the_dedicated_connection_limit",
                causal_chain=("named clients fill maxclients", "new connections fail"),
                evidence_ids=("redis-maxclients-capacity", "redis-scoped-clients"),
                confidence=0.99,
            ),
            evidence=(),
            hypothesis_states=(),
            observation_decisions=(),
            tool_calls=(),
            plan_step_count=1,
            duration_ms=1,
            safety_events=(),
        )
        recovery = await RedisMaxclientsRecoveryService(driver).recover(
            identity=identity,
            diagnostic_artifact=artifact,
            observation=observation,
        )

        assert recovery.authorized is True
        assert recovery.executed is True
        assert await unrelated.ping() is True
        assert (await driver.verify(identity)).passed is True
    finally:
        await unrelated.aclose()
        assert (await driver.cleanup(identity)).passed is True
        assert (await driver.cleanup(identity)).passed is True
