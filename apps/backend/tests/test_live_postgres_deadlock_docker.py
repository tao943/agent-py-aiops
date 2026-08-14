from __future__ import annotations

import os

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.postgres import PostgresConnectionConfig
from super_ai.evaluation.live.postgres_deadlock import (
    PostgresDeadlockRecoveryService,
    PostgresDeadlockScenarioDriver,
)
from super_ai.evaluation.live.scenarios import validate_run_id

pytestmark = pytest.mark.live_docker


@pytest.mark.asyncio
async def test_real_postgres_deadlock_retries_only_the_aborted_victim() -> None:
    identity = validate_run_id("docker-pg-deadlock-contract")
    driver = PostgresDeadlockScenarioDriver(
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
        assert observation.safe_fact("sqlstate") == "40P01"
        assert observation.safe_fact("victimRef") in {
            "transaction-a",
            "transaction-b",
        }
        artifact = RunArtifact(
            scenario_id="APY-LIVE-PG-DEADLOCK-001",
            mode="live",
            completed=True,
            report_produced=True,
            decision=RootCauseDecision(
                component="postgresql",
                mechanism="opposite_order_transaction_deadlock",
                trigger="concurrent_transactions_update_two_rows_in_reverse_order",
                causal_chain=("reverse update order", "wait cycle", "40P01 abort"),
                evidence_ids=("postgres-deadlock-40p01", "postgres-deadlock-cycle"),
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
        recovery = await PostgresDeadlockRecoveryService(driver).recover(
            identity=identity,
            diagnostic_artifact=artifact,
            observation=observation,
        )

        assert recovery.authorized is True
        assert recovery.executed is True
        assert recovery.target_ref == observation.safe_fact("victimRef")
        assert (await driver.verify(identity)).passed is True
    finally:
        assert (await driver.cleanup(identity)).passed is True
        assert (await driver.cleanup(identity)).passed is True
