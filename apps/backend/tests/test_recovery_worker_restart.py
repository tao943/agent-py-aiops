from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest

from super_ai.aiops.execution import ExecutionResult, UnsafeExecutionReplay
from super_ai.memory.repositories import JsonDict
from super_ai.recovery.contracts import RecoveryExecutionResult
from super_ai.recovery.worker import ProductionRecoveryWorker
from test_recovery_worker import (
    NOW,
    Authorizer,
    Context,
    Coordinator,
    Driver,
    Intents,
    make_intent,
)


class CrashAfterExecutionIntents(Intents):
    def __init__(self) -> None:
        super().__init__(make_intent())
        self.crash_once = True

    async def transition(
        self, *, expected_statuses: tuple[str, ...], to_status: str, **kwargs: object
    ):
        if self.intent.status == "executing" and to_status == "verifying" and self.crash_once:
            self.crash_once = False
            raise RuntimeError("simulated_worker_crash")
        return await super().transition(
            expected_statuses=expected_statuses, to_status=to_status, **kwargs
        )


class UnsafeCoordinator:
    async def run_once(
        self,
        identity: object,
        operation: Callable[[], Awaitable[JsonDict]],
        *,
        outcome_known_on_error: bool,
    ) -> ExecutionResult:
        del identity, operation, outcome_known_on_error
        raise UnsafeExecutionReplay("uncertain_side_effect_requires_manual_review")


class UnknownOutcomeDriver(Driver):
    async def execute(self, context: dict[str, object]) -> RecoveryExecutionResult:
        del context
        self.execute_calls += 1
        return RecoveryExecutionResult(False, False, "outcome_unknown", 30_000)


@pytest.mark.asyncio
async def test_completed_execution_is_reused_after_crash_and_only_verification_repeats() -> None:
    intents = CrashAfterExecutionIntents()
    driver = Driver()
    coordinator = Coordinator()
    worker = ProductionRecoveryWorker(
        intents=intents,  # type: ignore[arg-type]
        authorizer=Authorizer(),  # type: ignore[arg-type]
        driver_factory=lambda intent: driver,
        coordinator_factory=lambda intent: coordinator,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="simulated_worker_crash"):
        await worker.handle(Context())  # type: ignore[arg-type]
    assert intents.intent.status == "executing"
    assert driver.execute_calls == 1

    await worker.handle(Context())  # type: ignore[arg-type]

    assert intents.intent.status == "recovered"
    assert driver.execute_calls == 1
    assert driver.verify_calls == 1


@pytest.mark.asyncio
async def test_expired_unknown_side_effect_claim_moves_to_manual_without_executor() -> None:
    intents = Intents(replace(make_intent(), status="executing"))
    driver = Driver()
    worker = ProductionRecoveryWorker(
        intents=intents,  # type: ignore[arg-type]
        authorizer=Authorizer(),  # type: ignore[arg-type]
        driver_factory=lambda intent: driver,
        coordinator_factory=lambda intent: UnsafeCoordinator(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    await worker.handle(Context())  # type: ignore[arg-type]

    assert intents.intent.status == "manual_intervention"
    assert driver.execute_calls == 0
    assert driver.verify_calls == 0


@pytest.mark.asyncio
async def test_terminal_intent_is_a_noop() -> None:
    intents = Intents(replace(make_intent(), status="recovered"))
    driver = Driver()
    worker = ProductionRecoveryWorker(
        intents=intents,  # type: ignore[arg-type]
        authorizer=Authorizer(),  # type: ignore[arg-type]
        driver_factory=lambda intent: driver,
        coordinator_factory=lambda intent: UnsafeCoordinator(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    await worker.handle(Context())  # type: ignore[arg-type]

    assert intents.statuses == ["recovered"]
    assert driver.execute_calls == 0
    assert driver.verify_calls == 0


@pytest.mark.asyncio
async def test_operation_unknown_outcome_is_not_retried() -> None:
    intents = Intents(make_intent())
    driver = UnknownOutcomeDriver()
    coordinator = Coordinator()
    worker = ProductionRecoveryWorker(
        intents=intents,  # type: ignore[arg-type]
        authorizer=Authorizer(),  # type: ignore[arg-type]
        driver_factory=lambda intent: driver,
        coordinator_factory=lambda intent: coordinator,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    await worker.handle(Context())  # type: ignore[arg-type]
    await worker.handle(Context())  # type: ignore[arg-type]

    assert intents.intent.status == "manual_intervention"
    assert driver.execute_calls == 1
