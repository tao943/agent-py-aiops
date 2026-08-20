"""Persistable model-call budgets and diagnostic execution deadlines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

ModelRole = Literal[
    "planner",
    "replanner",
    "adjudicator",
    "validator",
    "investigator",
    "recovery_planner",
    "report",
]

ROLE_TIMEOUT_SECONDS: dict[ModelRole, int] = {
    "planner": 60,
    "replanner": 60,
    "adjudicator": 60,
    "validator": 60,
    "investigator": 45,
    "recovery_planner": 90,
    "report": 90,
}


class ModelCallBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class ModelCallBudget:
    hard_limit: int = 8
    used: int = 0

    def __post_init__(self) -> None:
        if self.hard_limit <= 0 or self.used < 0 or self.used > self.hard_limit:
            raise ValueError("Model call budget is invalid.")

    def reserve(self, role: ModelRole) -> int:
        del role
        if self.used >= self.hard_limit:
            raise ModelCallBudgetExceeded("model_call_budget_exhausted")
        self.used += 1
        return self.used


@dataclass(frozen=True, slots=True)
class ExecutionDeadlines:
    started_at: datetime
    soft_deadline_at: datetime
    hard_deadline_at: datetime

    def __post_init__(self) -> None:
        values = (self.started_at, self.soft_deadline_at, self.hard_deadline_at)
        if any(value.tzinfo is None for value in values):
            raise ValueError("Execution deadlines must be timezone aware.")
        if not self.started_at < self.soft_deadline_at < self.hard_deadline_at:
            raise ValueError("Execution deadlines are out of order.")

    @classmethod
    def start(cls, started_at: datetime | None = None) -> ExecutionDeadlines:
        started = started_at or datetime.now(timezone.utc)
        return cls(
            started_at=started,
            soft_deadline_at=started + timedelta(minutes=5),
            hard_deadline_at=started + timedelta(minutes=8),
        )

    @classmethod
    def from_iso(
        cls,
        *,
        started_at: str,
        soft_deadline_at: str,
        hard_deadline_at: str,
    ) -> ExecutionDeadlines:
        return cls(
            started_at=datetime.fromisoformat(started_at),
            soft_deadline_at=datetime.fromisoformat(soft_deadline_at),
            hard_deadline_at=datetime.fromisoformat(hard_deadline_at),
        )

    def soft_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.soft_deadline_at

    def hard_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.hard_deadline_at
