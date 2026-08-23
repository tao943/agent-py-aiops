"""Trusted orchestration for the isolated order-pool automatic-remediation slice."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic
from typing import Literal, Protocol, cast

from super_ai.aiops.execution import (
    ExecutionIdentity,
    ExecutionResult,
    UnsafeExecutionReplay,
)
from super_ai.alert_ingestion.repositories import LiveAlertLifecycle
from super_ai.evaluation.artifacts import RunArtifact, build_run_artifact
from super_ai.evaluation.live.auto_closure_state import (
    AutoClosureStage,
    AutoClosureState,
    stage_at_least,
)
from super_ai.evaluation.live.diagnostics import append_live_evidence_context
from super_ai.evaluation.live.domain import (
    LiveCleanupResult,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveVerification,
    RecoveryExpectation,
)
from super_ai.evaluation.live.scenarios import validate_run_id
from super_ai.memory.repositories import (
    AgentToolCallAuditRecord,
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticStepRecord,
    DiagnosticTaskRecord,
    MemoryRepositories,
)

SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"
RECOVERY_TARGET = "live-eval-order-api"
RECOVERY_ACTION = "restart_live_eval_order_api"
RECOVERY_GRAPH_VERSION = "order-pool-auto-closure-v1"
_MECHANISM = "exception_path_connection_not_released"
_REQUIRED_FAULT_CHECKS = frozenset(
    {
        "pool_at_capacity",
        "pool_free_zero",
        "business_probe_timed_out",
        "postgres_reachable",
        "no_lock_wait",
        "run_scoped_sessions_present",
    }
)
_VALIDATION_ORIGINS = frozenset(
    {"llm_confirmed", "deterministic_grounded_fallback"}
)

AutoClosureValidity = Literal[
    "VALID_PASS",
    "VALID_FAIL",
    "INFRA_INVALID",
    "MANUAL_REVIEW",
]
ClosureMetricStage = Literal[
    "detection",
    "diagnosis",
    "recovery",
    "verification",
    "resolved",
    "total",
]


@dataclass(frozen=True, slots=True)
class AutoClosureBudgets:
    detection_seconds: float = 45
    diagnosis_seconds: float = 360
    recovery_seconds: float = 30
    verification_seconds: float = 60
    resolved_seconds: float = 60
    poll_seconds: float = 2


@dataclass(frozen=True, slots=True)
class AutoClosureCorrelation:
    incident_id: str | None = None
    diagnostic_task_id: str | None = None
    background_job_id: str | None = None
    report_id: str | None = None


@dataclass(frozen=True, slots=True)
class PersistedDiagnosticOutcome:
    artifact: RunArtifact
    evidence_sufficiency: str


@dataclass(frozen=True, slots=True)
class RecoveryAuthorization:
    execution_permitted: bool
    code: str
    target: str = RECOVERY_TARGET


@dataclass(frozen=True, slots=True)
class LiveAutoClosureResult:
    validity: AutoClosureValidity
    strategy: Literal["single_agent"]
    authorization_code: str
    correlation: AutoClosureCorrelation
    recovery_intent_id: str | None
    closed_verified: bool
    observation: LiveFaultObservation | None = None
    diagnostic_artifact: RunArtifact | None = None
    recovery: LiveRecoveryRecord | None = None
    verification: LiveVerification | None = None
    cleanup: LiveCleanupResult | None = None


class AutoClosureDriver(Protocol):
    async def preflight(self, identity: LiveRunIdentity) -> None: ...

    async def baseline(self, identity: LiveRunIdentity) -> None: ...

    def export_resume_state(self, identity: LiveRunIdentity) -> dict[str, object]: ...

    def restore(
        self,
        identity: LiveRunIdentity,
        state: Mapping[str, object],
    ) -> None: ...

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation: ...

    def recovery_eligible(self, identity: LiveRunIdentity) -> bool: ...

    def mark_recovery_completed(self, identity: LiveRunIdentity) -> None: ...

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification: ...

    async def cleanup(self, identity: LiveRunIdentity) -> LiveCleanupResult: ...


class LifecycleRepository(Protocol):
    async def get_live_lifecycle(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
    ) -> LiveAlertLifecycle | None: ...

    async def record_verification(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
        status: Literal["passed", "failed"],
        summary: str,
        verified_at: datetime,
    ) -> LiveAlertLifecycle: ...


class DiagnosticOutcomeLoader(Protocol):
    async def load(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
    ) -> PersistedDiagnosticOutcome: ...


class EvidencePreparer(Protocol):
    async def prepare(
        self,
        identity: LiveRunIdentity,
        observation: LiveFaultObservation,
    ) -> LiveEvidenceContext: ...


class AutoClosureStateRepository(Protocol):
    async def create(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
        driver_state: dict[str, object],
    ) -> AutoClosureState: ...

    async def load(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
    ) -> AutoClosureState | None: ...

    async def save(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
        state: AutoClosureState,
    ) -> AutoClosureState: ...


class AutoClosureMetrics(Protocol):
    def record_stage_latency(
        self,
        stage: ClosureMetricStage,
        *,
        latency_ms: float,
    ) -> None: ...

    def record_verification(self, status: Literal["passed", "failed"]) -> None: ...


class PersistedDiagnosticOutcomeLoader:
    """Rebuild a trusted artifact from records, never from report prose."""

    def __init__(
        self,
        repositories: MemoryRepositories,
        *,
        artifact_builder: Callable[
            [
                DiagnosticTaskRecord,
                Sequence[DiagnosticStepRecord],
                Sequence[DiagnosticEvidenceRecord],
                Sequence[AgentToolCallAuditRecord],
                Sequence[DiagnosticReportRecord],
            ],
            RunArtifact,
        ] = build_run_artifact,
    ) -> None:
        self._repositories = repositories
        self._artifact_builder = artifact_builder

    async def load(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
    ) -> PersistedDiagnosticOutcome:
        diagnostics = self._repositories.diagnostics
        task = await diagnostics.get_task(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        if task is None:
            raise RuntimeError("Persisted diagnostic task is unavailable.")
        steps = await diagnostics.list_steps(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        evidence = await diagnostics.list_evidence(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        reports = await diagnostics.list_reports(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        tool_call_repository = self._repositories.tool_call_audits
        audits = (
            await tool_call_repository.list_for_diagnostic_task(
                owner_user_id=owner_user_id,
                diagnostic_task_id=diagnostic_task_id,
            )
            if tool_call_repository is not None
            else []
        )
        artifact = self._artifact_builder(task, steps, evidence, audits, reports)
        sufficiency = _evidence_sufficiency_status(task.result_payload)
        if sufficiency is None and reports:
            sufficiency = _evidence_sufficiency_status(reports[-1].payload)
        if sufficiency is None:
            sufficiency = "insufficient"
        return PersistedDiagnosticOutcome(artifact, sufficiency)


def _evidence_sufficiency_status(payload: Mapping[str, object]) -> str | None:
    raw = payload.get("evidenceSufficiency")
    if isinstance(raw, Mapping):
        raw = cast(Mapping[str, object], raw).get("status")
    return raw if isinstance(raw, str) and raw in {"sufficient", "insufficient"} else None


class RecoveryService(Protocol):
    async def recover(
        self,
        *,
        identity: LiveRunIdentity,
        diagnostic_artifact: object,
        observation: LiveFaultObservation,
    ) -> LiveRecoveryRecord: ...


class RecoveryCoordinator(Protocol):
    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[dict[str, object]]],
        *,
        outcome_known_on_error: bool,
    ) -> ExecutionResult: ...


def authorize_order_pool_recovery(
    outcome: PersistedDiagnosticOutcome,
    observation: LiveFaultObservation,
    *,
    driver_owns_identity: bool,
    expected_task_id: str,
) -> RecoveryAuthorization:
    artifact = outcome.artifact
    decision = artifact.decision
    checks = {check.name for check in observation.checks if check.passed}
    policy = artifact.recovery_policy_audit
    predicates = (
        (artifact.scenario_id == SCENARIO_ID, "scenario_mismatch"),
        (observation.scenario_id == SCENARIO_ID, "observation_scenario_mismatch"),
        (
            artifact.diagnostic_task_id == expected_task_id,
            "diagnostic_task_mismatch",
        ),
        (decision is not None, "decision_missing"),
        (decision is not None and decision.component == "order-api", "component_mismatch"),
        (decision is not None and decision.mechanism == _MECHANISM, "mechanism_mismatch"),
        (_REQUIRED_FAULT_CHECKS <= checks, "fault_observation_incomplete"),
        (outcome.evidence_sufficiency == "sufficient", "evidence_insufficient"),
        (
            artifact.validation_audit is not None
            and artifact.validation_audit.origin in _VALIDATION_ORIGINS,
            "deterministic_validation_failed",
        ),
        (policy is not None, "policy_gate_missing"),
        (
            policy is not None
            and policy.status == "deferred"
            and policy.authorization_code == "external_policy_required"
            and policy.execution_permitted is False
            and policy.proposal_recorded is False
            and policy.human_approval_required is False,
            "policy_gate_handoff_invalid",
        ),
        (artifact.completed and artifact.report_produced, "diagnostic_incomplete"),
        (driver_owns_identity, "driver_identity_missing"),
    )
    for passed, code in predicates:
        if not passed:
            return RecoveryAuthorization(False, code)
    return RecoveryAuthorization(True, "authorized")


class OrderPoolAutoClosureOrchestrator:
    def __init__(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        driver: AutoClosureDriver,
        lifecycles: LifecycleRepository,
        diagnostic_loader: DiagnosticOutcomeLoader,
        recovery: RecoveryService,
        recovery_coordinator: RecoveryCoordinator,
        evidence_preparer: EvidencePreparer | None = None,
        state_repository: AutoClosureStateRepository | None = None,
        metrics: AutoClosureMetrics | None = None,
        progress: Callable[[str], None] | None = None,
        budgets: AutoClosureBudgets | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._owner_user_id = owner_user_id
        self._source_id = source_id
        self._driver = driver
        self._lifecycles = lifecycles
        self._diagnostic_loader = diagnostic_loader
        self._recovery = recovery
        self._recovery_coordinator = recovery_coordinator
        self._evidence_preparer = evidence_preparer
        self._state_repository = state_repository
        self._metrics = metrics
        self._progress_callback = progress
        self._budgets = budgets or AutoClosureBudgets()
        self._sleep = sleep
        self._clock = clock

    async def run(
        self,
        scenario_id: str,
        *,
        run_id: str,
        resume: bool = False,
    ) -> LiveAutoClosureResult:
        if scenario_id != SCENARIO_ID:
            raise ValueError("Automatic closure supports only the order-pool scenario.")
        run_started = self._clock()
        identity = validate_run_id(run_id)
        observation: LiveFaultObservation | None = None
        evidence_context: LiveEvidenceContext | None = None
        outcome: PersistedDiagnosticOutcome | None = None
        lifecycle: LiveAlertLifecycle | None = None
        recovery: LiveRecoveryRecord | None = None
        verification: LiveVerification | None = None
        recovery_intent_id: str | None = None
        validity: AutoClosureValidity = "INFRA_INVALID"
        authorization_code = "preflight_failed"
        state = await self._load_state(identity)
        if resume:
            if self._state_repository is None or state is None:
                raise ValueError("Automatic closure resume state does not exist.")
            self._driver.restore(identity, state.driver_state)
            observation = state.observation
        elif state is not None:
            raise ValueError("Automatic closure run exists; use resume.")
        try:
            try:
                if state is None:
                    await self._driver.preflight(identity)
                    await self._driver.baseline(identity)
                    self._progress("fixture_ready")
                    state = await self._create_state(identity)
                if observation is None:
                    observation = await self._driver.inject(identity)
                    self._progress("fault_injected")
                    state = await self._advance_state(
                        identity,
                        state,
                        stage="fault_injected",
                        observation=observation,
                    )
            except Exception:
                return await self._finish(
                    identity,
                    validity="INFRA_INVALID",
                    authorization_code="fixture_infrastructure_failed",
                    observation=observation,
                    started_at=run_started,
                )
            if not observation.confirmed:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code="fault_observation_incomplete",
                    observation=observation,
                    started_at=run_started,
                )
            if self._evidence_preparer is not None:
                evidence_context = await self._evidence_preparer.prepare(
                    identity,
                    observation,
                )
            detection_started = self._clock()
            lifecycle = await self._wait_lifecycle(
                run_id,
                seconds=self._budgets.detection_seconds,
                require_report=False,
            )
            if lifecycle is None:
                return await self._finish(
                    identity,
                    validity="INFRA_INVALID",
                    authorization_code="prometheus_detection_timeout",
                    observation=observation,
                    started_at=run_started,
                )
            self._record_stage_latency("detection", detection_started)
            self._progress("alert_detected")
            state = await self._advance_state(
                identity,
                state,
                stage="alert_detected",
                correlation=_lifecycle_payload(lifecycle),
            )
            diagnosis_started = self._clock()
            diagnosis_deadline = diagnosis_started + max(
                0,
                self._budgets.diagnosis_seconds,
            )
            lifecycle = await self._wait_lifecycle(
                run_id,
                seconds=max(0, diagnosis_deadline - self._clock()),
                require_report=True,
            )
            if lifecycle is None:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code="diagnostic_report_timeout",
                    observation=observation,
                    started_at=run_started,
                )
            outcome = await self._wait_completed_diagnostic_outcome(
                diagnostic_task_id=lifecycle.diagnostic_task_id,
                deadline=diagnosis_deadline,
            )
            if outcome is None:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code="diagnostic_report_timeout",
                    lifecycle=lifecycle,
                    observation=observation,
                    started_at=run_started,
                )
            self._record_stage_latency("diagnosis", diagnosis_started)
            self._progress("diagnosis_completed")
            state = await self._advance_state(
                identity,
                state,
                stage="diagnosis_completed",
                correlation=_lifecycle_payload(lifecycle),
            )
            if evidence_context is not None:
                outcome = replace(
                    outcome,
                    artifact=append_live_evidence_context(
                        outcome.artifact,
                        context=evidence_context,
                    ),
                )
            authorization = authorize_order_pool_recovery(
                outcome,
                observation,
                driver_owns_identity=self._driver.recovery_eligible(identity),
                expected_task_id=lifecycle.diagnostic_task_id,
            )
            if not authorization.execution_permitted:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code=authorization.code,
                    lifecycle=lifecycle,
                    observation=observation,
                    outcome=outcome,
                    started_at=run_started,
                )

            async def recover_once() -> dict[str, object]:
                record = await self._recovery.recover(
                    identity=identity,
                    diagnostic_artifact=outcome.artifact,
                    observation=observation,
                )
                return _recovery_payload(record)

            execution_identity = ExecutionIdentity(
                task_id=lifecycle.diagnostic_task_id,
                graph_version=RECOVERY_GRAPH_VERSION,
                node_name="compose_restart",
                logical_iteration=0,
                input_payload={
                    "scenarioId": SCENARIO_ID,
                    "action": RECOVERY_ACTION,
                    "target": RECOVERY_TARGET,
                    "runId": identity.run_id,
                    "safeFacts": dict(observation.safe_facts),
                },
                execution_kind="recovery",
                side_effecting=True,
            )
            recovery_intent_id = execution_identity.execution_key
            state = await self._advance_state(
                identity,
                state,
                stage="recovery_dispatched",
                recovery_execution_key=recovery_intent_id,
            )
            recovery_started = self._clock()
            try:
                coordinated = await self._recovery_coordinator.run_once(
                    execution_identity,
                    recover_once,
                    outcome_known_on_error=False,
                )
            except UnsafeExecutionReplay:
                return await self._finish(
                    identity,
                    validity="MANUAL_REVIEW",
                    authorization_code="uncertain_previous_attempt",
                    lifecycle=lifecycle,
                    observation=observation,
                    outcome=outcome,
                    recovery_intent_id=recovery_intent_id,
                    started_at=run_started,
                )
            except Exception:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code="recovery_failed",
                    lifecycle=lifecycle,
                    observation=observation,
                    outcome=outcome,
                    recovery_intent_id=recovery_intent_id,
                    started_at=run_started,
                )
            recovery = _recovery_from_payload(coordinated.output)
            self._record_stage_latency("recovery", recovery_started)
            self._progress("recovery_completed")
            if recovery.executed:
                self._driver.mark_recovery_completed(identity)
            state = await self._advance_state(
                identity,
                state,
                stage="recovery_completed",
                recovery_execution_key=recovery_intent_id,
                recovery_payload=_recovery_payload(recovery),
            )
            if not (recovery.authorized and recovery.executed):
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code=recovery.authorization_code,
                    lifecycle=lifecycle,
                    observation=observation,
                    outcome=outcome,
                    recovery=recovery,
                    recovery_intent_id=recovery_intent_id,
                    started_at=run_started,
                )
            verification_started = self._clock()
            verification = await self._driver.verify(identity)
            await self._lifecycles.record_verification(
                owner_user_id=self._owner_user_id,
                source_id=self._source_id,
                scenario_id=SCENARIO_ID,
                run_id=identity.run_id,
                status="passed" if verification.passed else "failed",
                summary=_verification_summary(verification),
                verified_at=datetime.now(timezone.utc),
            )
            state = await self._advance_state(
                identity,
                state,
                stage="verification_recorded",
                verification_payload=_verification_payload(verification),
            )
            self._record_stage_latency("verification", verification_started)
            if not verification.passed:
                if self._metrics is not None:
                    self._metrics.record_verification("failed")
                validity = "VALID_FAIL"
                authorization_code = "independent_verification_failed"
            else:
                self._progress("verification_passed")
                resolved_started = self._clock()
                lifecycle = await self._wait_resolved(identity.run_id)
                if lifecycle is None:
                    validity = "VALID_FAIL"
                    authorization_code = "alert_resolved_timeout"
                else:
                    self._record_stage_latency("resolved", resolved_started)
                    if self._metrics is not None:
                        self._metrics.record_verification("passed")
                    self._progress("alert_resolved")
                    validity = "VALID_PASS"
                    authorization_code = "verified_closed"
                    state = await self._advance_state(
                        identity,
                        state,
                        stage="resolved",
                        correlation=_lifecycle_payload(lifecycle),
                    )
        except Exception:
            validity = "VALID_FAIL"
            authorization_code = "orchestration_failed"
        return await self._finish(
            identity,
            validity=validity,
            authorization_code=authorization_code,
            lifecycle=lifecycle,
            observation=observation,
            outcome=outcome,
            recovery=recovery,
            verification=verification,
            recovery_intent_id=recovery_intent_id,
            started_at=run_started,
        )

    async def _load_state(self, identity: LiveRunIdentity) -> AutoClosureState | None:
        if self._state_repository is None:
            return None
        return await self._state_repository.load(
            owner_user_id=self._owner_user_id,
            source_id=self._source_id,
            scenario_id=SCENARIO_ID,
            run_id=identity.run_id,
        )

    async def _create_state(self, identity: LiveRunIdentity) -> AutoClosureState | None:
        if self._state_repository is None:
            return None
        return await self._state_repository.create(
            owner_user_id=self._owner_user_id,
            source_id=self._source_id,
            scenario_id=SCENARIO_ID,
            run_id=identity.run_id,
            driver_state=self._driver.export_resume_state(identity),
        )

    async def _advance_state(
        self,
        identity: LiveRunIdentity,
        state: AutoClosureState | None,
        *,
        stage: AutoClosureStage,
        observation: LiveFaultObservation | None = None,
        correlation: dict[str, str | None] | None = None,
        recovery_execution_key: str | None = None,
        recovery_payload: dict[str, object] | None = None,
        verification_payload: dict[str, object] | None = None,
    ) -> AutoClosureState | None:
        if self._state_repository is None or state is None:
            return state
        target_stage = state.stage if stage_at_least(state.stage, stage) else stage
        candidate = replace(
            state,
            stage=target_stage,
            observation=observation if observation is not None else state.observation,
            correlation=correlation if correlation is not None else state.correlation,
            recovery_execution_key=(
                recovery_execution_key
                if recovery_execution_key is not None
                else state.recovery_execution_key
            ),
            recovery_payload=(
                recovery_payload if recovery_payload is not None else state.recovery_payload
            ),
            verification_payload=(
                verification_payload
                if verification_payload is not None
                else state.verification_payload
            ),
        )
        if candidate == state:
            return state
        return await self._state_repository.save(
            owner_user_id=self._owner_user_id,
            source_id=self._source_id,
            scenario_id=SCENARIO_ID,
            run_id=identity.run_id,
            state=candidate,
        )

    async def _wait_lifecycle(
        self,
        run_id: str,
        *,
        seconds: float,
        require_report: bool,
    ) -> LiveAlertLifecycle | None:
        deadline = self._clock() + max(0, seconds)
        while True:
            lifecycle = await self._lifecycles.get_live_lifecycle(
                owner_user_id=self._owner_user_id,
                source_id=self._source_id,
                scenario_id=SCENARIO_ID,
                run_id=run_id,
            )
            if lifecycle is not None and (
                not require_report or lifecycle.report_id is not None
            ):
                return lifecycle
            if self._clock() >= deadline:
                return None
            await self._sleep(self._budgets.poll_seconds)

    async def _wait_resolved(self, run_id: str) -> LiveAlertLifecycle | None:
        deadline = self._clock() + max(0, self._budgets.resolved_seconds)
        while True:
            lifecycle = await self._lifecycles.get_live_lifecycle(
                owner_user_id=self._owner_user_id,
                source_id=self._source_id,
                scenario_id=SCENARIO_ID,
                run_id=run_id,
            )
            if lifecycle is not None and lifecycle.closed_verified:
                return lifecycle
            if self._clock() >= deadline:
                return None
            await self._sleep(self._budgets.poll_seconds)

    async def _wait_completed_diagnostic_outcome(
        self,
        *,
        diagnostic_task_id: str,
        deadline: float,
    ) -> PersistedDiagnosticOutcome | None:
        while True:
            outcome = await self._diagnostic_loader.load(
                owner_user_id=self._owner_user_id,
                diagnostic_task_id=diagnostic_task_id,
            )
            if outcome.artifact.completed and outcome.artifact.report_produced:
                return outcome
            if self._clock() >= deadline:
                return None
            await self._sleep(self._budgets.poll_seconds)

    async def _finish(
        self,
        identity: LiveRunIdentity,
        *,
        validity: AutoClosureValidity,
        authorization_code: str,
        lifecycle: LiveAlertLifecycle | None = None,
        observation: LiveFaultObservation | None = None,
        outcome: PersistedDiagnosticOutcome | None = None,
        recovery: LiveRecoveryRecord | None = None,
        verification: LiveVerification | None = None,
        recovery_intent_id: str | None = None,
        started_at: float | None = None,
    ) -> LiveAutoClosureResult:
        cleanup: LiveCleanupResult | None = None
        try:
            cleanup = await self._driver.cleanup(identity)
        except Exception:
            if validity == "VALID_PASS":
                validity = "VALID_FAIL"
                authorization_code = "cleanup_failed"
        if cleanup is not None and not cleanup.passed and validity == "VALID_PASS":
            validity = "VALID_FAIL"
            authorization_code = "cleanup_failed"
        if cleanup is not None:
            self._progress("cleanup_completed")
        if started_at is not None:
            self._record_stage_latency("total", started_at)
        correlation = AutoClosureCorrelation(
            incident_id=lifecycle.incident_id if lifecycle else None,
            diagnostic_task_id=lifecycle.diagnostic_task_id if lifecycle else None,
            background_job_id=lifecycle.background_job_id if lifecycle else None,
            report_id=lifecycle.report_id if lifecycle else None,
        )
        closed_verified = bool(
            validity == "VALID_PASS"
            and lifecycle is not None
            and lifecycle.closed_verified
            and verification is not None
            and verification.passed
            and cleanup is not None
            and cleanup.passed
        )
        return LiveAutoClosureResult(
            validity=validity,
            strategy="single_agent",
            authorization_code=authorization_code,
            correlation=correlation,
            recovery_intent_id=recovery_intent_id,
            closed_verified=closed_verified,
            observation=observation,
            diagnostic_artifact=outcome.artifact if outcome else None,
            recovery=recovery,
            verification=verification,
            cleanup=cleanup,
        )

    def _record_stage_latency(
        self,
        stage: ClosureMetricStage,
        started_at: float,
    ) -> None:
        if self._metrics is not None:
            self._metrics.record_stage_latency(
                stage,
                latency_ms=max(0.0, (self._clock() - started_at) * 1000),
            )

    def _progress(self, stage: str) -> None:
        if self._progress_callback is not None:
            self._progress_callback(stage)


def _recovery_payload(record: LiveRecoveryRecord) -> dict[str, object]:
    return {
        "action": record.action,
        "targetRef": record.target_ref,
        "expectation": record.expectation,
        "authorized": record.authorized,
        "executed": record.executed,
        "authorizationCode": record.authorization_code,
    }


def _recovery_from_payload(payload: Mapping[str, object]) -> LiveRecoveryRecord:
    expectation = payload.get("expectation")
    if expectation not in {"executed_recovery", "proposal_only"}:
        raise ValueError("Stored recovery expectation is invalid.")
    return LiveRecoveryRecord(
        action=str(payload.get("action") or ""),
        target_ref=str(payload.get("targetRef") or ""),
        expectation=cast(RecoveryExpectation, expectation),
        authorized=payload.get("authorized") is True,
        executed=payload.get("executed") is True,
        authorization_code=str(payload.get("authorizationCode") or ""),
    )


def _verification_summary(verification: LiveVerification) -> str:
    passed = sum(check.passed for check in verification.checks)
    return f"{passed}/{len(verification.checks)} independent checks passed"


def _verification_payload(verification: LiveVerification) -> dict[str, object]:
    return {
        "checks": [
            {"name": check.name, "passed": check.passed}
            for check in verification.checks
        ],
        "passed": verification.passed,
    }


def _lifecycle_payload(lifecycle: LiveAlertLifecycle) -> dict[str, str | None]:
    return {
        "incidentId": lifecycle.incident_id,
        "diagnosticTaskId": lifecycle.diagnostic_task_id,
        "backgroundJobId": lifecycle.background_job_id,
        "reportId": lifecycle.report_id,
    }
