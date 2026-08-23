from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.aiops.execution import (
    ExecutionIdentity,
    ExecutionResult,
    UnsafeExecutionReplay,
)
from super_ai.alert_ingestion.repositories import LiveAlertLifecycle
from super_ai.evaluation.artifacts import (
    RecoveryPolicyAudit,
    RunArtifact,
    ValidationAudit,
)
from super_ai.evaluation.live.auto_closure import (
    AutoClosureBudgets,
    ClosureMetricStage,
    OrderPoolAutoClosureOrchestrator,
    PersistedDiagnosticOutcome,
    PersistedDiagnosticOutcomeLoader,
    authorize_order_pool_recovery,
)
from super_ai.evaluation.live.auto_closure_state import AutoClosureState
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveRunIdentity,
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
        recovery_policy_audit=RecoveryPolicyAudit(
            status="deferred",
            authorization_code="external_policy_required",
            execution_permitted=False,
            proposal_recorded=False,
            human_approval_required=False,
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

    async def preflight(self, identity: LiveRunIdentity) -> None:
        del identity
        self.calls.append("preflight")

    async def baseline(self, identity: LiveRunIdentity) -> None:
        del identity
        self.calls.append("baseline")

    def export_resume_state(self, identity: LiveRunIdentity) -> dict[str, object]:
        del identity
        return {
            "originalGeneration": "generation-1",
            "unrelatedSessionFingerprints": ["a" * 64],
        }

    def restore(self, identity: LiveRunIdentity, state: Mapping[str, object]) -> None:
        del identity, state
        self.calls.append("restore")

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation:
        del identity
        self.calls.append("inject")
        return _observation()

    def recovery_eligible(self, identity: LiveRunIdentity) -> bool:
        del identity
        return True

    def mark_recovery_completed(self, identity: LiveRunIdentity) -> None:
        del identity

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification:
        del identity
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

    async def cleanup(self, identity: LiveRunIdentity) -> LiveCleanupResult:
        del identity
        self.calls.append("cleanup")
        return LiveCleanupResult((LiveCheck("cleanup", True),))


class LifecycleRepository:
    def __init__(self) -> None:
        self.verification_status = "pending"
        self.verification_calls = 0

    async def get_live_lifecycle(
        self, **kwargs: str
    ) -> LiveAlertLifecycle:
        del kwargs
        status = "resolved" if self.verification_calls else "active"
        return _lifecycle(status=status, verification=self.verification_status)

    async def record_verification(self, **kwargs: object) -> LiveAlertLifecycle:
        assert kwargs["status"] == "passed"
        self.verification_calls += 1
        self.verification_status = "passed"
        return _lifecycle(verification="passed")


class Loader:
    async def load(self, **kwargs: str) -> PersistedDiagnosticOutcome:
        del kwargs
        return PersistedDiagnosticOutcome(_artifact(), "sufficient")


class Recovery:
    def __init__(self) -> None:
        self.calls = 0

    async def recover(self, **kwargs: object) -> LiveRecoveryRecord:
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
    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[dict[str, object]]],
        *,
        outcome_known_on_error: bool,
    ) -> ExecutionResult:
        assert identity.side_effecting
        assert identity.execution_kind == "recovery"
        assert not outcome_known_on_error
        return ExecutionResult(await operation(), False, 1)


class CachingCoordinator:
    def __init__(self) -> None:
        self.outputs: dict[str, dict[str, object]] = {}

    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[dict[str, object]]],
        *,
        outcome_known_on_error: bool,
    ) -> ExecutionResult:
        assert not outcome_known_on_error
        if identity.execution_key in self.outputs:
            return ExecutionResult(self.outputs[identity.execution_key], True, 1)
        output = await operation()
        self.outputs[identity.execution_key] = output
        return ExecutionResult(output, False, 1)


class StateRepository:
    def __init__(self) -> None:
        self.state: AutoClosureState | None = None

    async def create(self, **kwargs: object) -> AutoClosureState:
        driver_state = cast(dict[str, object], kwargs["driver_state"])
        if self.state is None:
            self.state = AutoClosureState("baseline_ready", driver_state)
        return self.state

    async def load(self, **kwargs: object) -> AutoClosureState | None:
        del kwargs
        return self.state

    async def save(self, **kwargs: object) -> AutoClosureState:
        state = cast(AutoClosureState, kwargs["state"])
        assert self.state is not None
        assert state.version == self.state.version
        self.state = replace(state, version=state.version + 1)
        return self.state


class ClosureMetrics:
    def __init__(self) -> None:
        self.latencies: list[tuple[str, float]] = []
        self.verifications: list[str] = []

    def record_stage_latency(
        self, stage: ClosureMetricStage, *, latency_ms: float
    ) -> None:
        self.latencies.append((stage, latency_ms))

    def record_verification(self, status: Literal["passed", "failed"]) -> None:
        self.verifications.append(status)


class EvidencePreparer:
    def __init__(self) -> None:
        self.calls = 0

    async def prepare(
        self, identity: LiveRunIdentity, observation: LiveFaultObservation
    ) -> LiveEvidenceContext:
        assert observation.confirmed
        self.calls += 1
        return LiveEvidenceContext.local(incident_id=f"{SCENARIO_ID}-{identity.run_id}")


@pytest.mark.asyncio
async def test_automatic_closure_waits_for_report_recovers_once_and_closes_verified() -> None:
    driver = Driver()
    recovery = Recovery()
    repository = LifecycleRepository()
    evidence_preparer = EvidencePreparer()
    orchestrator = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=driver,
        lifecycles=repository,
        diagnostic_loader=Loader(),
        recovery=recovery,
        recovery_coordinator=Coordinator(),
        evidence_preparer=evidence_preparer,
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
    assert evidence_preparer.calls == 1
    assert result.diagnostic_artifact is not None
    assert result.diagnostic_artifact.live_evidence is not None
    assert result.diagnostic_artifact.live_evidence.source == "local"
    assert repository.verification_calls == 1
    assert driver.calls == ["preflight", "baseline", "inject", "verify", "cleanup"]


@pytest.mark.asyncio
async def test_automatic_closure_waits_for_completed_persisted_artifact() -> None:
    class EventuallyCompleteLoader:
        def __init__(self) -> None:
            self.calls = 0

        async def load(self, **kwargs: str) -> PersistedDiagnosticOutcome:
            del kwargs
            self.calls += 1
            artifact = _artifact()
            if self.calls == 1:
                artifact = replace(
                    artifact,
                    completed=False,
                    report_produced=False,
                )
            return PersistedDiagnosticOutcome(artifact, "sufficient")

    loader = EventuallyCompleteLoader()
    recovery = Recovery()
    orchestrator = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=Driver(),
        lifecycles=LifecycleRepository(),
        diagnostic_loader=loader,
        recovery=recovery,
        recovery_coordinator=Coordinator(),
        budgets=AutoClosureBudgets(poll_seconds=0),
    )

    result = await orchestrator.run(SCENARIO_ID, run_id="auto-artifact-race")

    assert result.validity == "VALID_PASS"
    assert loader.calls == 2
    assert recovery.calls == 1


@pytest.mark.asyncio
async def test_resume_restores_state_and_reuses_completed_recovery() -> None:
    states = StateRepository()
    coordinator = CachingCoordinator()
    recovery = Recovery()
    lifecycle = LifecycleRepository()
    first_driver = Driver()
    first = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=first_driver,
        lifecycles=lifecycle,
        diagnostic_loader=Loader(),
        recovery=recovery,
        recovery_coordinator=coordinator,
        state_repository=states,
        budgets=AutoClosureBudgets(poll_seconds=0),
    )
    first_result = await first.run(SCENARIO_ID, run_id="auto-resume")

    resumed_driver = Driver()
    resumed = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=resumed_driver,
        lifecycles=lifecycle,
        diagnostic_loader=Loader(),
        recovery=recovery,
        recovery_coordinator=coordinator,
        state_repository=states,
        budgets=AutoClosureBudgets(poll_seconds=0),
    )
    resumed_result = await resumed.run(
        SCENARIO_ID,
        run_id="auto-resume",
        resume=True,
    )

    assert first_result.validity == resumed_result.validity == "VALID_PASS"
    assert first_result.recovery_intent_id == resumed_result.recovery_intent_id
    assert recovery.calls == 1
    assert resumed_driver.calls == ["restore", "verify", "cleanup"]
    assert states.state is not None and states.state.stage == "resolved"


@pytest.mark.asyncio
async def test_success_records_bounded_stage_metrics_and_progress() -> None:
    metrics = ClosureMetrics()
    progress: list[str] = []
    orchestrator = OrderPoolAutoClosureOrchestrator(
        owner_user_id="owner",
        source_id="local-alertmanager",
        driver=Driver(),
        lifecycles=LifecycleRepository(),
        diagnostic_loader=Loader(),
        recovery=Recovery(),
        recovery_coordinator=Coordinator(),
        metrics=metrics,
        progress=progress.append,
        budgets=AutoClosureBudgets(poll_seconds=0),
    )

    result = await orchestrator.run(SCENARIO_ID, run_id="auto-metrics")

    assert result.validity == "VALID_PASS"
    assert [stage for stage, _ in metrics.latencies] == [
        "detection",
        "diagnosis",
        "recovery",
        "verification",
        "resolved",
        "total",
    ]
    assert all(latency >= 0 for _, latency in metrics.latencies)
    assert metrics.verifications == ["passed"]
    assert progress == [
        "fixture_ready",
        "fault_injected",
        "alert_detected",
        "diagnosis_completed",
        "recovery_completed",
        "verification_passed",
        "alert_resolved",
        "cleanup_completed",
    ]


@pytest.mark.asyncio
async def test_wrong_root_cause_is_valid_fail_without_recovery() -> None:
    class WrongLoader:
        async def load(self, **kwargs: str) -> PersistedDiagnosticOutcome:
            del kwargs
            decision = _artifact().decision
            assert decision is not None
            artifact = replace(
                _artifact(),
                decision=replace(decision, mechanism="connectivity_failure"),
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
        expected_task_id="diagnostic-1",
    )
    assert authorization.execution_permitted
    assert authorization.target == "live-eval-order-api"

    denied = authorize_order_pool_recovery(
        PersistedDiagnosticOutcome(_artifact(), "insufficient"),
        _observation(),
        driver_owns_identity=True,
        expected_task_id="diagnostic-1",
    )
    assert not denied.execution_permitted
    assert denied.code == "evidence_insufficient"


def _policy_audit() -> RecoveryPolicyAudit:
    audit = _artifact().recovery_policy_audit
    assert audit is not None
    return audit


@pytest.mark.parametrize(
    ("artifact", "expected_code"),
    (
        (replace(_artifact(), recovery_policy_audit=None), "policy_gate_missing"),
        (
            replace(
                _artifact(),
                recovery_policy_audit=replace(
                    _policy_audit(),
                    authorization_code="manual_review_required",
                ),
            ),
            "policy_gate_handoff_invalid",
        ),
        (
            replace(
                _artifact(),
                recovery_policy_audit=replace(
                    _policy_audit(),
                    execution_permitted=True,
                ),
            ),
            "policy_gate_handoff_invalid",
        ),
    ),
)
def test_authorization_requires_persisted_external_policy_handoff(
    artifact: RunArtifact,
    expected_code: str,
) -> None:
    authorization = authorize_order_pool_recovery(
        PersistedDiagnosticOutcome(artifact, "sufficient"),
        _observation(),
        driver_owns_identity=True,
        expected_task_id="diagnostic-1",
    )

    assert not authorization.execution_permitted
    assert authorization.code == expected_code


def test_authorization_rejects_cross_task_artifact() -> None:
    authorization = authorize_order_pool_recovery(
        PersistedDiagnosticOutcome(_artifact(), "sufficient"),
        _observation(),
        driver_owns_identity=True,
        expected_task_id="different-task",
    )

    assert not authorization.execution_permitted
    assert authorization.code == "diagnostic_task_mismatch"


@pytest.mark.asyncio
async def test_uncertain_recovery_is_manual_review_and_never_verified() -> None:
    class UncertainCoordinator:
        async def run_once(
            self,
            identity: ExecutionIdentity,
            operation: Callable[[], Awaitable[dict[str, object]]],
            *,
            outcome_known_on_error: bool,
        ) -> ExecutionResult:
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
        async def get_task(self, **kwargs: str) -> object:
            del kwargs
            return task

        async def list_steps(self, **kwargs: str) -> list[object]:
            del kwargs
            return ["step"]

        async def list_evidence(self, **kwargs: str) -> list[object]:
            del kwargs
            return ["evidence"]

        async def list_reports(self, **kwargs: str) -> list[object]:
            del kwargs
            return [report]

    class Audits:
        async def list_for_diagnostic_task(self, **kwargs: str) -> list[object]:
            del kwargs
            return ["audit"]

    repositories = SimpleNamespace(diagnostics=Diagnostics(), tool_call_audits=Audits())
    captured: dict[str, object] = {}

    def builder(
        actual_task: object,
        steps: Sequence[object],
        evidence: Sequence[object],
        audits: Sequence[object],
        reports: Sequence[object],
    ) -> RunArtifact:
        captured.update(
            task=actual_task,
            steps=steps,
            evidence=evidence,
            audits=audits,
            reports=reports,
        )
        return _artifact()

    loader = PersistedDiagnosticOutcomeLoader(
        cast(Any, repositories),
        artifact_builder=cast(Any, builder),
    )

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


@pytest.mark.asyncio
async def test_persisted_loader_reads_structured_evidence_sufficiency_status() -> None:
    task = SimpleNamespace(
        id="diagnostic-structured",
        result_payload={"evidenceSufficiency": {"status": "sufficient"}},
    )

    class Diagnostics:
        async def get_task(self, **kwargs: str) -> object:
            del kwargs
            return task

        async def list_steps(self, **kwargs: str) -> list[object]:
            del kwargs
            return []

        async def list_evidence(self, **kwargs: str) -> list[object]:
            del kwargs
            return []

        async def list_reports(self, **kwargs: str) -> list[object]:
            del kwargs
            return []

    repositories = SimpleNamespace(diagnostics=Diagnostics(), tool_call_audits=None)

    def static_builder(*_args: object) -> RunArtifact:
        return _artifact()

    loader = PersistedDiagnosticOutcomeLoader(
        cast(Any, repositories),
        artifact_builder=cast(Any, static_builder),
    )

    outcome = await loader.load(
        owner_user_id="owner",
        diagnostic_task_id="diagnostic-structured",
    )

    assert outcome.evidence_sufficiency == "sufficient"
