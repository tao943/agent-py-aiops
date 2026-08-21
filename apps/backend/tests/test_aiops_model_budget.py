from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from super_ai.aiops.model_budget import (
    ROLE_TIMEOUT_SECONDS,
    ExecutionDeadlines,
    ModelCallBudget,
    ModelCallBudgetExceeded,
)


def test_model_budget_resumes_from_persisted_count_and_never_exceeds_eight() -> None:
    budget = ModelCallBudget(used=7)

    assert budget.reserve("validator") == 8
    with pytest.raises(ModelCallBudgetExceeded, match="model_call_budget_exhausted"):
        budget.reserve("report")


def test_role_timeouts_are_bounded() -> None:
    assert ROLE_TIMEOUT_SECONDS == {
        "planner": 60,
        "replanner": 60,
        "adjudicator": 60,
        "validator": 60,
        "investigator": 45,
        "recovery_planner": 90,
        "report": 90,
    }


def test_deadlines_are_derived_once_and_reused_after_resume() -> None:
    started = datetime(2026, 8, 19, 1, 2, tzinfo=timezone.utc)
    deadlines = ExecutionDeadlines.start(started)
    resumed = ExecutionDeadlines.from_iso(
        started_at=deadlines.started_at.isoformat(),
        soft_deadline_at=deadlines.soft_deadline_at.isoformat(),
        hard_deadline_at=deadlines.hard_deadline_at.isoformat(),
    )

    assert resumed == deadlines
    assert deadlines.soft_deadline_at == started + timedelta(minutes=4)
    assert deadlines.hard_deadline_at == started + timedelta(minutes=6)
    assert deadlines.soft_expired(started + timedelta(minutes=4)) is True
    assert deadlines.hard_expired(started + timedelta(minutes=6)) is True
