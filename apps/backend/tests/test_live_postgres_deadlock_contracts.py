from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import RunArtifact
from super_ai.evaluation.live.domain import LiveCheck, LiveFaultObservation
from super_ai.evaluation.live.postgres_deadlock import (
    PostgresDeadlockEvidenceMcpClient,
    PostgresDeadlockRecoveryService,
)
from super_ai.evaluation.live.scenarios import validate_run_id
from super_ai.mcp_client import McpClientError


def _observation() -> LiveFaultObservation:
    return LiveFaultObservation(
        scenario_id="APY-LIVE-PG-DEADLOCK-001",
        checks=(
            LiveCheck("postgres_40p01", True, "InspectPostgresDeadlockAudit"),
            LiveCheck("deadlock_cycle_audited", True, "InspectPostgresDeadlockAudit"),
        ),
        safe_facts=(
            ("sqlstate", "40P01"),
            ("cycleLength", 2),
            ("victimRef", "transaction-b"),
            ("postgresHealthy", True),
            ("transactionAFirstResource", "order_row_1"),
            ("transactionASecondResource", "order_row_2"),
            ("transactionBFirstResource", "order_row_2"),
            ("transactionBSecondResource", "order_row_1"),
        ),
    )


def _artifact(mechanism: str = "opposite_order_transaction_deadlock") -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-LIVE-PG-DEADLOCK-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            "postgresql",
            mechanism,
            "concurrent_transactions_update_two_rows_in_reverse_order",
            ("transactions update in reverse order", "wait cycle forms", "victim aborts"),
            ("postgres-deadlock-40p01", "postgres-deadlock-cycle"),
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
        self.targets: list[str] = []

    async def retry_aborted_transaction(self, *, run_id: str, target_ref: str) -> bool:
        self.targets.append(f"{run_id}:{target_ref}")
        return True


@pytest.mark.asyncio
async def test_deadlock_recovery_retries_only_the_recorded_current_run_victim() -> None:
    driver = RecordingDriver()
    record = await PostgresDeadlockRecoveryService(driver).recover(
        identity=validate_run_id("run-1"),
        diagnostic_artifact=_artifact(),
        observation=_observation(),
    )

    assert record.authorized is True
    assert record.executed is True
    assert record.target_ref == "transaction-b"
    assert driver.targets == ["run-1:transaction-b"]


@pytest.mark.asyncio
async def test_deadlock_recovery_denies_a_non_deadlock_decision() -> None:
    driver = RecordingDriver()
    record = await PostgresDeadlockRecoveryService(driver).recover(
        identity=validate_run_id("run-1"),
        diagnostic_artifact=_artifact("long_lock_wait"),
        observation=_observation(),
    )

    assert record.authorized is False
    assert record.executed is False
    assert driver.targets == []


@pytest.mark.asyncio
async def test_deadlock_evidence_tools_are_read_only_and_pid_free() -> None:
    client = PostgresDeadlockEvidenceMcpClient(_observation())
    definitions = await client.discover_tools()

    assert {item.name for item in definitions} == {
        "InspectPostgresDeadlockAudit",
        "InspectPostgresTransactionResult",
        "VerifyPostgresHealth",
    }
    output = await client.call_tool("InspectPostgresDeadlockAudit", {})
    assert isinstance(output, Mapping)
    serialized = json.dumps(output)
    assert "40P01" in serialized
    assert output["transactionAFirstResource"] == "order_row_1"
    assert output["transactionBFirstResource"] == "order_row_2"
    assert "pid" not in serialized.casefold()
    with pytest.raises(McpClientError):
        await client.call_tool("RetryAbortedBenchmarkTransaction", {})


@pytest.mark.asyncio
async def test_deadlock_evidence_rejects_arguments() -> None:
    client = PostgresDeadlockEvidenceMcpClient(_observation())

    with pytest.raises(McpClientError):
        await client.call_tool(
            "InspectPostgresTransactionResult",
            cast_mapping({"run_id": "other"}),
        )


def cast_mapping(value: dict[str, object]) -> Mapping[str, object]:
    return value
