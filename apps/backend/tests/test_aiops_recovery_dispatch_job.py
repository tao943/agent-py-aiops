from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from super_ai.api.app import _aiops_job_handler  # pyright: ignore[reportPrivateUsage]
from super_ai.jobs.runtime import BackgroundJobContext, JobCancelled
from super_ai.memory.repositories import BackgroundJobRecord, DiagnosticTaskRecord
from super_ai.recovery.auto_dispatch import AutoRecoveryDispatchResult

NOW = datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc)


def _task(status: str) -> DiagnosticTaskRecord:
    return DiagnosticTaskRecord(
        "diagnostic-1",
        "owner-1",
        status,
        "diagnose",
        {"query": "diagnose", "triggerSource": "alertmanager"},
        {},
        NOW,
        NOW,
        NOW if status in {"succeeded", "failed", "cancelled"} else None,
    )


def _job() -> BackgroundJobRecord:
    return BackgroundJobRecord(
        "job-1",
        "owner-1",
        "aiops_diagnosis",
        "aiops_diagnostic",
        "diagnostic-1",
        "running",
        {"diagnosticId": "diagnostic-1"},
        1,
        3,
        1800,
        NOW,
        "worker-1",
        NOW,
        None,
        None,
        None,
        NOW,
        NOW,
        NOW,
        None,
    )


class Diagnostics:
    def __init__(self, status: str) -> None:
        self.task = _task(status)

    async def get_task(self, **_: str) -> DiagnosticTaskRecord | None:
        return self.task

    async def update_task(self, **kwargs: object) -> DiagnosticTaskRecord:
        self.task = replace(
            self.task,
            status=str(kwargs["status"]),
            completed_at=kwargs.get("completed_at"),  # type: ignore[arg-type]
        )
        return self.task


class Runner:
    def __init__(self, diagnostics: Diagnostics) -> None:
        self.diagnostics = diagnostics
        self.stream_calls = 0

    async def stream(self, **_: object):  # type: ignore[no-untyped-def]
        self.stream_calls += 1
        self.diagnostics.task = _task("succeeded")
        yield {"type": "task.status", "status": "succeeded"}


class Dispatcher:
    def __init__(self, result: AutoRecoveryDispatchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def dispatch(
        self, *, owner_user_id: str, diagnostic_task_id: str
    ) -> AutoRecoveryDispatchResult:
        self.calls.append((owner_user_id, diagnostic_task_id))
        return self.result


class JobRepository:
    def __init__(self, *, cancel_after_checks: int | None = None) -> None:
        self.job = _job()
        self.cancel_after_checks = cancel_after_checks
        self.cancel_checks = 0
        self.events: list[dict[str, object]] = []

    async def get(self, **_: str) -> BackgroundJobRecord:
        self.cancel_checks += 1
        if (
            self.cancel_after_checks is not None
            and self.cancel_checks >= self.cancel_after_checks
        ):
            return replace(self.job, cancel_requested_at=NOW)
        return self.job

    async def append_event(self, *, payload: dict[str, object], **_: str) -> object:
        self.events.append(payload)
        return object()


def _fixture(
    status: str,
    *,
    result: AutoRecoveryDispatchResult | None = None,
    cancel_after_checks: int | None = None,
) -> tuple[Awaitable[None], Runner, Dispatcher, JobRepository]:
    diagnostics = Diagnostics(status)
    runner = Runner(diagnostics)
    dispatcher = Dispatcher(
        result or AutoRecoveryDispatchResult("created", intent_id="intent-1", status="queued")
    )
    jobs = JobRepository(cancel_after_checks=cancel_after_checks)
    app = FastAPI()
    app.state.memory_repositories = SimpleNamespace(diagnostics=diagnostics)
    app.state.aiops_diagnostic_runner = runner
    app.state.auto_recovery_intent_dispatcher = dispatcher
    return _aiops_job_handler(app)(BackgroundJobContext(jobs.job, jobs)), runner, dispatcher, jobs  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_successful_diagnosis_dispatches_and_appends_safe_event() -> None:
    pending, runner, dispatcher, jobs = _fixture("accepted")

    await pending

    assert runner.stream_calls == 1
    assert dispatcher.calls == [("owner-1", "diagnostic-1")]
    assert jobs.events[-1] == {
        "type": "recovery.intent.dispatch",
        "outcome": "created",
        "reasonCode": None,
        "intentId": "intent-1",
        "status": "queued",
    }


@pytest.mark.asyncio
async def test_succeeded_retry_skips_runner_and_reuses_dispatch() -> None:
    pending, runner, dispatcher, jobs = _fixture(
        "succeeded",
        result=AutoRecoveryDispatchResult(
            "reused", intent_id="intent-1", status="queued"
        ),
    )

    await pending

    assert runner.stream_calls == 0
    assert dispatcher.calls == [("owner-1", "diagnostic-1")]
    assert jobs.events[-1]["outcome"] == "reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "unknown"])
async def test_terminal_task_never_reruns_or_dispatches(status: str) -> None:
    pending, runner, dispatcher, _ = _fixture(status)

    with pytest.raises((RuntimeError, JobCancelled)):
        await pending

    assert runner.stream_calls == 0
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_cancellation_after_diagnosis_prevents_dispatch() -> None:
    pending, runner, dispatcher, _ = _fixture("accepted", cancel_after_checks=2)

    with pytest.raises(JobCancelled):
        await pending

    assert runner.stream_calls == 1
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_dispatcher_task_reread_race_fails_job_instead_of_skipping() -> None:
    pending, _, _, jobs = _fixture(
        "succeeded",
        result=AutoRecoveryDispatchResult("skipped", "task_unavailable"),
    )

    with pytest.raises(RuntimeError, match="Diagnostic task is unavailable"):
        await pending

    assert not any(
        event.get("reasonCode") == "task_unavailable" for event in jobs.events
    )
