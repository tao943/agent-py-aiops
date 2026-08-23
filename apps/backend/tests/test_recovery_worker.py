from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from super_ai.aiops.execution import ExecutionResult
from super_ai.memory.repositories import JsonDict
from super_ai.recovery.contracts import (
    RecoveryCheck,
    RecoveryExecutionResult,
    RecoveryIntentRecord,
    RecoveryStatus,
    RecoveryVerificationResult,
)
from super_ai.recovery.worker import (
    ProductionRecoveryWorker,
    RecoveryAuthorization,
    RecoveryDriverPreflight,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def make_intent(status: RecoveryStatus = "queued") -> RecoveryIntentRecord:
    return RecoveryIntentRecord(
        "intent-1",
        "owner-1",
        "incident-1",
        "diagnostic-1",
        "report-1",
        "restart_compose_service",
        "live-eval-order-api",
        "low",
        True,
        False,
        status,
        "a" * 64,
        ("evidence-1",),
        {},
        {},
        NOW,
        None,
        None,
        None,
        None,
        None,
        (),
    )  # type: ignore[arg-type]


class Context:
    class Job:
        owner_user_id = "owner-1"
        resource_id = "intent-1"

    job = Job()

    def __init__(self) -> None:
        self.cancel_checks = 0
        self.events: list[dict[str, object]] = []

    async def raise_if_cancelled(self) -> None:
        self.cancel_checks += 1

    async def append_event(self, payload: dict[str, object]) -> None:
        self.events.append(payload)


class Intents:
    def __init__(self, intent: RecoveryIntentRecord) -> None:
        self.intent = intent
        self.statuses = [intent.status]

    async def get_owned(self, **_: str) -> RecoveryIntentRecord:
        return self.intent

    async def get_current_approval(self, **_: str) -> None:
        return None

    async def transition(
        self, *, expected_statuses: tuple[str, ...], to_status: str, **values: object
    ) -> RecoveryIntentRecord:
        assert self.intent.status in expected_statuses
        updates: dict[str, object] = {"status": to_status}
        if values.get("execution_summary") is not None:
            updates["execution_summary"] = values["execution_summary"]
        if values.get("verification_checks") is not None:
            updates["verification"] = values["verification_checks"]
        self.intent = replace(self.intent, **updates)  # type: ignore[arg-type]
        self.statuses.append(to_status)
        return self.intent


class Authorizer:
    async def authorize(
        self, intent: RecoveryIntentRecord, approval: object
    ) -> RecoveryAuthorization:
        del approval
        return RecoveryAuthorization(True, intent.proposal_fingerprint, None)


class Driver:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.verify_calls = 0

    async def preflight(
        self, intent: RecoveryIntentRecord, approval: object
    ) -> RecoveryDriverPreflight:
        del intent, approval
        return RecoveryDriverPreflight(True, {"containerId": "private"}, None)

    async def execute(self, context: dict[str, object]) -> RecoveryExecutionResult:
        del context
        self.execute_calls += 1
        return RecoveryExecutionResult(True, True, "recovery_action_completed", 25)

    async def verify(self, context: dict[str, object]) -> RecoveryVerificationResult:
        del context
        self.verify_calls += 1
        return RecoveryVerificationResult(
            True,
            (RecoveryCheck("incident_resolved", "passed", "Incident resolved.", NOW),),
            "recovery_verified",
        )


class Coordinator:
    def __init__(self) -> None:
        self.calls = 0
        self.output: dict[str, object] | None = None

    async def run_once(
        self,
        identity: object,
        operation: Callable[[], Awaitable[JsonDict]],
        *,
        outcome_known_on_error: bool,
    ) -> ExecutionResult:
        del identity
        assert outcome_known_on_error is False
        self.calls += 1
        if self.output is None:
            self.output = await operation()
            return ExecutionResult(self.output, False, 1)
        return ExecutionResult(self.output, True, 1)


@pytest.mark.asyncio
async def test_worker_persists_exact_success_progression_and_checks_cancellation_before_claim() -> (
    None
):
    intents = Intents(make_intent())
    driver = Driver()
    coordinator = Coordinator()
    worker = ProductionRecoveryWorker(
        intents=intents,  # type: ignore[arg-type]
        authorizer=Authorizer(),  # type: ignore[arg-type]
        driver_factory=lambda intent: driver,
        coordinator_factory=lambda intent: coordinator,  # type: ignore[arg-type]
        now=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}-fixed-{len(intents.statuses)}",
    )
    context = Context()

    await worker.handle(context)  # type: ignore[arg-type]

    assert intents.statuses == ["queued", "revalidating", "executing", "verifying", "recovered"]
    assert context.cancel_checks == 1
    assert driver.execute_calls == 1
    assert driver.verify_calls == 1
    assert coordinator.calls == 1
    assert intents.intent.execution_summary == "recovery_action_completed"
    assert [check.key for check in intents.intent.verification] == ["incident_resolved"]
    assert [event["status"] for event in context.events] == [
        "revalidating",
        "executing",
        "verifying",
        "recovered",
    ]


class DeniedAuthorizer:
    async def authorize(
        self, intent: RecoveryIntentRecord, approval: object
    ) -> RecoveryAuthorization:
        del approval
        return RecoveryAuthorization(False, intent.proposal_fingerprint, "policy_drift")


class DeniedPreflightDriver(Driver):
    async def preflight(
        self, intent: RecoveryIntentRecord, approval: object
    ) -> RecoveryDriverPreflight:
        del intent, approval
        return RecoveryDriverPreflight(False, {}, "preflight_drift")


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_stage", ["authorization", "preflight"])
async def test_failed_fresh_gate_never_invokes_executor(deny_stage: str) -> None:
    intents = Intents(make_intent())
    driver = DeniedPreflightDriver() if deny_stage == "preflight" else Driver()
    authorizer = DeniedAuthorizer() if deny_stage == "authorization" else Authorizer()
    worker = ProductionRecoveryWorker(
        intents=intents,  # type: ignore[arg-type]
        authorizer=authorizer,  # type: ignore[arg-type]
        driver_factory=lambda intent: driver,
        coordinator_factory=lambda intent: Coordinator(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    await worker.handle(Context())  # type: ignore[arg-type]

    assert intents.intent.status == "manual_intervention"
    assert driver.execute_calls == 0
