from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)
from super_ai.evaluation.live.runner import LiveBenchmarkError, LiveBenchmarkRunner

LIVE_ROOT = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "live"


class RecordingDriver:
    def __init__(
        self,
        *,
        confirmed: bool = True,
        fail_at: str | None = None,
        cleanup_passed: bool = True,
    ) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.cleanup_passed = cleanup_passed
        self.observation = LiveFaultObservation(
            scenario_id="APY-LIVE-PG-LOCK-001",
            checks=(
                LiveCheck("waiter_has_lock_event", confirmed),
                LiveCheck("blocker_edge_confirmed", confirmed),
            ),
        )

    async def _step(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"secret-{name}")

    async def preflight(self, identity: object) -> None:
        del identity
        await self._step("preflight")

    async def baseline(self, identity: object) -> None:
        del identity
        await self._step("baseline")

    async def inject(self, identity: object) -> LiveFaultObservation:
        del identity
        await self._step("inject")
        return self.observation

    async def verify(self, identity: object) -> LiveVerification:
        del identity
        await self._step("verify")
        return LiveVerification(
            checks=(
                LiveCheck("blocker_gone", True),
                LiveCheck("waiter_unblocked", True),
                LiveCheck("lock_graph_clear", True),
                LiveCheck("probe_succeeded", True),
                LiveCheck("postgres_healthy", True),
                LiveCheck("unrelated_sessions_untouched", True),
            )
        )

    async def cleanup(self, identity: object) -> LiveCleanupResult:
        del identity
        await self._step("cleanup")
        return LiveCleanupResult(
            checks=(LiveCheck("scoped_fixture_removed", self.cleanup_passed),)
        )


class RecordingDiagnostic:
    def __init__(self, events: list[str], *, cancelled: bool = False) -> None:
        self.events = events
        self.cancelled = cancelled

    async def diagnose(self, **values: object) -> object:
        del values
        self.events.append("diagnose")
        if self.cancelled:
            raise asyncio.CancelledError
        return {"decision": "row_lock_blocking"}


class RecordingEvidencePreparer:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def prepare(self, **values: object) -> LiveEvidenceContext:
        del values
        self.events.append("prepare")
        if self.fail:
            raise RuntimeError("secret-evidence-readiness")
        return LiveEvidenceContext.local(incident_id="APY-LIVE-PG-LOCK-001-run-1")


class RecordingRecovery:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def recover(self, **values: object) -> LiveRecoveryRecord:
        del values
        self.events.append("recover")
        return LiveRecoveryRecord(
            action="terminate_postgres_backend",
            target_ref="synthetic_blocker",
            expectation="executed_recovery",
            authorized=True,
            executed=True,
            authorization_code="authorized",
        )


class RecordingEvaluator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def evaluate(self, **values: object) -> str:
        del values
        self.events.append("evaluate")
        return "passed"


@pytest.mark.asyncio
async def test_runner_executes_live_phases_and_always_cleans_up() -> None:
    driver = RecordingDriver()
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    result = await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert result == "passed"
    assert driver.events == [
        "preflight",
        "baseline",
        "inject",
        "prepare",
        "diagnose",
        "recover",
        "verify",
        "evaluate",
        "cleanup",
    ]


@pytest.mark.asyncio
async def test_runner_stops_before_diagnosis_when_fault_is_not_confirmed() -> None:
    driver = RecordingDriver(confirmed=False)
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert captured.value.category == "fault_injection_failed"
    assert driver.events == ["preflight", "baseline", "inject", "cleanup"]


@pytest.mark.asyncio
async def test_runner_cleans_up_and_redacts_driver_failure() -> None:
    driver = RecordingDriver(fail_at="inject")
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert captured.value.category == "fault_injection_failed"
    assert "secret" not in str(captured.value)
    assert driver.events[-1] == "cleanup"


@pytest.mark.asyncio
async def test_runner_re_raises_cancellation_after_cleanup() -> None:
    driver = RecordingDriver()
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events, cancelled=True),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert driver.events[-1] == "cleanup"


@pytest.mark.asyncio
async def test_cleanup_failure_is_a_hard_failure() -> None:
    driver = RecordingDriver(fail_at="cleanup")
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert captured.value.category == "cleanup_failed"


@pytest.mark.asyncio
async def test_failed_cleanup_result_is_a_hard_failure() -> None:
    driver = RecordingDriver(cleanup_passed=False)
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert captured.value.category == "cleanup_failed"


@pytest.mark.asyncio
async def test_failed_post_recovery_verification_does_not_reach_evaluator() -> None:
    driver = RecordingDriver()

    async def failed_verify(identity: object) -> LiveVerification:
        del identity
        driver.events.append("verify")
        return LiveVerification(
            checks=(
                LiveCheck("blocker_gone", True),
                LiveCheck("waiter_unblocked", True),
                LiveCheck("lock_graph_clear", True),
                LiveCheck("probe_succeeded", False),
                LiveCheck("postgres_healthy", True),
                LiveCheck("unrelated_sessions_untouched", True),
            )
        )

    driver.verify = failed_verify  # type: ignore[method-assign]
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert captured.value.category == "recovery_verification_failed"
    assert "evaluate" not in driver.events
    assert driver.events[-1] == "cleanup"


@pytest.mark.asyncio
async def test_evidence_preparation_failure_is_classified_and_cleanup_still_runs() -> None:
    driver = RecordingDriver()
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events, fail=True),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )

    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")

    assert captured.value.category == "evidence_preparation_failed"
    assert "secret" not in str(captured.value)
    assert driver.events == ["preflight", "baseline", "inject", "prepare", "cleanup"]
