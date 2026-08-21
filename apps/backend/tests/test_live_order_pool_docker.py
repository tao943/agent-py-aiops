from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.cli import build_live_scenario_registry
from super_ai.evaluation.live.order_pool_leak import (
    OrderPoolClsRecordProvider,
    OrderPoolLeakScenarioDriver,
)
from super_ai.evaluation.live.scenarios import load_live_scenario, validate_run_id

pytestmark = pytest.mark.live_docker


def _artifact() -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-LIVE-ORDER-POOL-LEAK-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            "order-api",
            "exception_path_connection_not_released",
            "The exception path checks out a connection and omits release.",
            (
                "Checked-out connections accumulate.",
                "The pool saturates and new order updates time out.",
            ),
            ("order-pool-saturated", "cls-order-connection-lifecycle"),
            0.95,
        ),
        evidence=(),
        hypothesis_states=(),
        observation_decisions=(),
        tool_calls=(),
        plan_step_count=4,
        duration_ms=1,
        safety_events=(),
    )


@pytest.mark.asyncio
async def test_real_order_pool_leak_recovery_and_idempotent_cleanup() -> None:
    components = build_live_scenario_registry().resolve(
        "APY-LIVE-ORDER-POOL-LEAK-001"
    )
    assert isinstance(components.driver, OrderPoolLeakScenarioDriver)
    driver = components.driver
    identity = validate_run_id("docker-order-pool-contract")
    scenario = load_live_scenario(
        Path(__file__).resolve().parents[3]
        / "benchmarks"
        / "agentpy"
        / "live"
        / "APY-LIVE-ORDER-POOL-LEAK-001"
    )
    try:
        await driver.preflight(identity)
        await driver.baseline(identity)
        observation = await driver.inject(identity)
        assert observation.confirmed

        provider = components.cls_record_provider
        assert isinstance(provider, OrderPoolClsRecordProvider)
        records = await provider.records(
            identity=identity,
            scenario=scenario,
            observation=observation,
            now=datetime.now(timezone.utc),
        )
        _assert_actual_lifecycle(records, identity.run_id)

        recovery = await components.recovery.recover(
            identity=identity,
            diagnostic_artifact=_artifact(),
            observation=observation,
        )
        assert recovery.authorized and recovery.executed
        assert (await driver.verify(identity)).passed
    finally:
        await driver.cleanup(identity)
        await driver.cleanup(identity)
    assert (await driver.audit(identity)).clean


def _assert_actual_lifecycle(
    records: Sequence[Mapping[str, str]],
    run_id: str,
) -> None:
    typed = tuple(records)
    assert all(record["run_id"] == run_id for record in typed)
    fault_requests = {
        record["request_id"]
        for record in typed
        if record["event"] == "order_update_failed"
    }
    assert len(fault_requests) == 3
    assert all(
        any(
            record["request_id"] == request_id
            and record["event"] == "connection_checkout"
            for record in typed
        )
        for request_id in fault_requests
    )
    assert all(
        not any(
            record["request_id"] == request_id
            and record["event"] == "connection_checkin"
            for record in typed
        )
        for request_id in fault_requests
    )
    assert any(record["event"] == "pool_acquire_timeout" for record in typed)
    serialized = str(typed).casefold()
    for forbidden in ("oracle", "ground_truth", "primary_cause", "password", "token"):
        assert forbidden not in serialized
