from __future__ import annotations

import os

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.postgres import (
    PostgresConnectionConfig,
    PostgresLiveRecoveryService,
    PostgresLockScenarioDriver,
)
from super_ai.evaluation.live.scenarios import validate_run_id

pytestmark = pytest.mark.live_docker


@pytest.mark.asyncio
async def test_real_postgres_lock_injection_recovery_and_idempotent_cleanup() -> None:
    identity = validate_run_id("docker-pg-lock-contract")
    driver = PostgresLockScenarioDriver(
        PostgresConnectionConfig(
            host=os.getenv("LIVE_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("LIVE_POSTGRES_PORT", "5432")),
            user=os.getenv("LIVE_POSTGRES_USER", "agent_py"),
            password=os.getenv("LIVE_POSTGRES_PASSWORD", "agent_py_dev"),
            database="agent_py_live_eval",
        )
    )
    try:
        await driver.preflight(identity)
        await driver.baseline(identity)
        observation = await driver.inject(identity)

        assert observation.confirmed is True
        artifact = RunArtifact(
            scenario_id="APY-LIVE-PG-LOCK-001",
            mode="live",
            completed=True,
            report_produced=True,
            decision=RootCauseDecision(
                component="postgresql",
                mechanism="row_lock_blocking",
                trigger="synthetic_transaction_holds_order_row_lock",
                causal_chain=("lock acquired", "order update waits"),
                evidence_ids=("ev-wait", "ev-edge"),
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
        recovery = await PostgresLiveRecoveryService(driver).recover(
            identity=identity,
            diagnostic_artifact=artifact,
            observation=observation,
        )

        assert recovery.authorized is True
        assert recovery.executed is True
        assert (await driver.verify(identity)).passed is True
    finally:
        await driver.cleanup(identity)
        await driver.cleanup(identity)
