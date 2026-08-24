"""Trusted alert-triggered dispatch into the governed recovery control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from super_ai.memory.repositories import DiagnosticMemoryRepository
from super_ai.recovery.contracts import RecoveryStatus
from super_ai.recovery.intent_service import (
    RecoveryIntentNotEligible,
    RecoveryIntentService,
)

DispatchOutcome = Literal["created", "reused", "skipped"]
DispatchReason = Literal[
    "task_unavailable",
    "not_alert_triggered",
    "diagnostic_not_succeeded",
    "proposal_not_eligible",
]


@dataclass(frozen=True, slots=True)
class AutoRecoveryDispatchResult:
    outcome: DispatchOutcome
    reason_code: DispatchReason | None = None
    intent_id: str | None = None
    status: RecoveryStatus | None = None

    def public_event(self) -> dict[str, object]:
        """Project only the stable, non-sensitive Job event fields."""

        return {
            "type": "recovery.intent.dispatch",
            "outcome": self.outcome,
            "reasonCode": self.reason_code,
            "intentId": self.intent_id,
            "status": self.status,
        }


class AutoRecoveryIntentDispatcher:
    """Gate automatic dispatch by trusted origin and persisted task state."""

    def __init__(
        self,
        *,
        diagnostics: DiagnosticMemoryRepository,
        recovery_intents: RecoveryIntentService,
    ) -> None:
        self._diagnostics = diagnostics
        self._recovery_intents = recovery_intents

    async def dispatch(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
    ) -> AutoRecoveryDispatchResult:
        task = await self._diagnostics.get_task(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        if task is None:
            return AutoRecoveryDispatchResult("skipped", "task_unavailable")
        if task.input_payload.get("triggerSource") != "alertmanager":
            return AutoRecoveryDispatchResult("skipped", "not_alert_triggered")
        if task.status != "succeeded":
            return AutoRecoveryDispatchResult("skipped", "diagnostic_not_succeeded")
        try:
            result = await self._recovery_intents.create_result(
                owner_user_id=owner_user_id,
                diagnostic_task_id=diagnostic_task_id,
                note=None,
            )
        except RecoveryIntentNotEligible:
            return AutoRecoveryDispatchResult("skipped", "proposal_not_eligible")
        return AutoRecoveryDispatchResult(
            "reused" if result.reused else "created",
            intent_id=result.intent.id,
            status=result.intent.status,
        )
