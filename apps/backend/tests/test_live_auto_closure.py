from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.aiops.execution import ExecutionResult, UnsafeExecutionReplay
from super_ai.alert_ingestion.repositories import LiveAlertLifecycle
from super_ai.evaluation.artifacts import RunArtifact, ValidationAudit
from super_ai.evaluation.live.auto_closure import (
    AutoClosureBudgets,
    OrderPoolAutoClosureOrchestrator,
    PersistedDiagnosticOutcome,
    PersistedDiagnosticOutcomeLoader,
    authorize_order_pool_recovery,
)
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveVerification,
)

SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"


def _observation() -> LiveFaultObservation:
    return LiveFaultObservation(
        scenario_id=SCENARIO_ID,
        checks=tuple(
            LiveCheck(name, True)
            for name in (
                "pool_at_capacity",
                "pool_free_zero",
                "business_probe_timed_out",
                "postgres_reachable",
                "no_lock_wait",
                "run_scoped_sessions_present",
            )
        ),
        safe_facts=(("poolCapacity", 3), ("checkedOutConnections", 3)),
    )


def _artifact() -> RunArtifact:
    return RunArtifact(
        scenario_id=SCENARIO_ID,
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            component="order-api",
            mechanism="exception_path_connection_not_released",
            trigger="order update exception path retains a checked-out connection",
            causal_chain=("connection retained", "pool saturated", "probe timeout"),
            evidence_ids=("pool", "sessions", "probe"),
            confidence=0.95,
        ),
        evidence=(),
        hypothesis_states=(),
        observation_decisions=(),
        tool_calls=(),
        plan_step_count=3,
        duration_ms=100,
        safety_events=(),
        diagnostic_task_id="diagnostic-1",
        validation_audit=ValidationAudit(
            model=None,
            origin="deterministic_grounded_fallback",
            error_category=None,
            error_codes=(),
            error_phase=None,
            attempts=0,
        ),
        workflow_version="evidence-driven-v4",
        graph_version="aiops-diagnostic-v3",
    )


def _lifecycle(*, status: str = "active", verification: str = "pending") -> LiveAlertLifecycle:
    return LiveAlertLifecycle(
        incident_id="incident-1",
        diagnostic_task_id="diagnostic-1",
        background_job_id="job-1",
        report_id="report-1",
        status=status,  # type: ignore[arg-type]
        verification_status=verification,  # type: ignore[arg-type]
        verified_at=None,
        verification_summary=None,
    )


class Driver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def preflight(self, identity) -> None:
        self.calls.append("preflight")

    async def baseline(self, identity) -> None:
        self.calls.append("baseline")

    async def inject(self, identity) -> LiveFaultObservation:
        self.calls.append("inject")
        return _observation()

    def recovery_eligible(self, identity) -> bool:
        return True

    async def verify(self, identity) -> LiveVerification:
        self.calls.append("verify")
        return LiveVerification(
            tuple(
                LiveCheck(name, True)
                for name in (
                    "old_generation_released",
                    "new_generation_ready",
                    "business_probe_recovered",
                    "postgres_healthy",
                    "unrelated_sessions_preserved",
                    "scoped_recovery_recorded",
                )
            )
        )

    async def cleanup(self, identity) -> LiveCleanupResult:
        self.calls.append("cleanup")
        return LiveCleanupResult((LiveCheck("cleanup", True),))


class LifecycleRepository:
    def __init__(self) -> None:
        self.verification_status = "pending"
        self.verification_calls = 0

    async def get_live_lifecycle(self, **kwargs) -> LiveAlertLifecycle:
        del kwargs
        status = "resolved" if self.verification_calls else "active"
        return _lifecycle(status=status, verification=self.verification_status)

    async def record_verification(self, **kwargs) -> LiveAlertLifecycle:
        assert kwargs["status"] == "passed"
        self.verification_calls += 1
        self.verification_status = "passed"
        return _lifecycle(verification="passed")


class Loader:
    async def load(self, **kwargs) -> PersistedDiagnosticOutcome:
        del kwargs
        return PersistedDiagnosticOutcome(_artifact(), "sufficient")


class Recovery:
    def __init__(self) -> None:
        self.calls = 0

    async def recover(self, **kwargs) -> LiveRecoveryRecord:
        del kwargs
        self.calls += 1
        return LiveRecoveryRecord(
            "restart_live_eval_order_api",
            "current_run_order_api_instance",
            "executed_recovery",
            True,
            True,
            "authorized",
        )


class Coordinator:
    async def run_once(self, identity, operation, *, outcome_known_on_error):
        assert identity.side_effecting
        assert identity.execution_kind == "recovery"
        assert not outcome_known_on_error
        return ExecutionResult(await operation(), False, 1)


@pytest.mark.asyncio
async def test_automatic_closure_waits_for_report_recovers_once_and_closes_verified() -> None:
    driver = Driver()
    recovery = Recovery()
    repository = LifecycleRepository()
    orchestrator = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=driver,
        lifecycles=repository,
        diagnostic_loader=Loader(),
        recovery=recovery,
        recovery_coordinator=Coordinator(),
        budgets=AutoClosureBudgets(poll_seconds=0),
    )

    result = await orchestrator.run(SCENARIO_ID, run_id="auto-001")

    assert result.validity == "VALID_PASS"
    assert result.strategy == "single_agent"
    assert result.closed_verified
    assert result.correlation.incident_id == "incident-1"
    assert result.correlation.report_id == "report-1"
    assert result.recovery_intent_id
    assert recovery.calls == 1
    assert repository.verification_calls == 1
    assert driver.calls == ["preflight", "baseline", "inject", "verify", "cleanup"]


@pytest.mark.asyncio
async def test_wrong_root_cause_is_valid_fail_without_recovery() -> None:
    class WrongLoader:
        async def load(self, **kwargs) -> PersistedDiagnosticOutcome:
            del kwargs
            artifact = replace(
                _artifact(),
                decision=replace(_artifact().decision, mechanism="connectivity_failure"),
            )
            return PersistedDiagnosticOutcome(artifact, "sufficient")

    recovery = Recovery()
    orchestrator = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=Driver(),
        lifecycles=LifecycleRepository(),
        diagnostic_loader=WrongLoader(),
        recovery=recovery,
        recovery_coordinator=Coordinator(),
        budgets=AutoClosureBudgets(poll_seconds=0),
    )

    result = await orchestrator.run(SCENARIO_ID, run_id="auto-wrong")

    assert result.validity == "VALID_FAIL"
    assert result.authorization_code == "mechanism_mismatch"
    assert recovery.calls == 0


def test_authorization_requires_all_deterministic_predicates() -> None:
    authorization = authorize_order_pool_recovery(
        PersistedDiagnosticOutcome(_artifact(), "sufficient"),
        _observation(),
        driver_owns_identity=True,
    )
    assert authorization.execution_permitted
    assert authorization.target == "live-eval-order-api"

    denied = authorize_order_pool_recovery(
        PersistedDiagnosticOutcome(_artifact(), "insufficient"),
        _observation(),
        driver_owns_identity=True,
    )
    assert not denied.execution_permitted
    assert denied.code == "evidence_insufficient"


@pytest.mark.asyncio
async def test_uncertain_recovery_is_manual_review_and_never_verified() -> None:
    class UncertainCoordinator:
        async def run_once(self, identity, operation, *, outcome_known_on_error):
            del identity, operation, outcome_known_on_error
            raise UnsafeExecutionReplay("uncertain_side_effect_requires_manual_review")

    driver = Driver()
    orchestrator = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=driver,
        lifecycles=LifecycleRepository(),
        diagnostic_loader=Loader(),
        recovery=Recovery(),
        recovery_coordinator=UncertainCoordinator(),
        budgets=AutoClosureBudgets(poll_seconds=0),
    )

    result = await orchestrator.run(SCENARIO_ID, run_id="auto-uncertain")

    assert result.validity == "MANUAL_REVIEW"
    assert "verify" not in driver.calls
    assert driver.calls[-1] == "cleanup"


@pytest.mark.asyncio
async def test_persisted_loader_rebuilds_artifact_without_reading_report_prose() -> None:
    task = SimpleNamespace(
        id="diagnostic-1",
        result_payload={"evidenceSufficiency": "sufficient"},
    )
    report = SimpleNamespace(payload={"evidenceSufficiency": "sufficient"})

    class Diagnostics:
        async def get_task(self, **kwargs):
            del kwargs
            return task

        async def list_steps(self, **kwargs):
            del kwargs
            return ["step"]

        async def list_evidence(self, **kwargs):
            del kwargs
            return ["evidence"]

        async def list_reports(self, **kwargs):
            del kwargs
            return [report]

    class Audits:
        async def list_for_diagnostic_task(self, **kwargs):
            del kwargs
            return ["audit"]

    repositories = SimpleNamespace(diagnostics=Diagnostics(), tool_call_audits=Audits())
    captured = {}

    def builder(actual_task, steps, evidence, audits, reports):
        captured.update(
            task=actual_task,
            steps=steps,
            evidence=evidence,
            audits=audits,
            reports=reports,
        )
        return _artifact()

    loader = PersistedDiagnosticOutcomeLoader(repositories, artifact_builder=builder)

    outcome = await loader.load(
        owner_user_id="owner",
        diagnostic_task_id="diagnostic-1",
    )

    assert outcome.artifact == _artifact()
    assert outcome.evidence_sufficiency == "sufficient"
    assert captured == {
        "task": task,
        "steps": ["step"],
        "evidence": ["evidence"],
        "audits": ["audit"],
        "reports": [report],
    }
