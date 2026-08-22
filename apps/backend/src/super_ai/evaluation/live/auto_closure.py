"""Trusted orchestration for the isolated order-pool automatic-remediation slice."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Literal, Protocol, cast

from super_ai.aiops.execution import (
    ExecutionIdentity,
    ExecutionResult,
    UnsafeExecutionReplay,
)
from super_ai.alert_ingestion.repositories import LiveAlertLifecycle
from super_ai.evaluation.artifacts import RunArtifact, build_run_artifact
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

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation: ...

    def recovery_eligible(self, identity: LiveRunIdentity) -> bool: ...

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


class PersistedDiagnosticOutcomeLoader:
    """Rebuild a trusted artifact from records, never from report prose."""

    def __init__(
        self,
        repositories: Any,
        *,
        artifact_builder: Callable[..., RunArtifact] = build_run_artifact,
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
        sufficiency = task.result_payload.get("evidenceSufficiency")
        if sufficiency not in {"sufficient", "insufficient"} and reports:
            sufficiency = reports[-1].payload.get("evidenceSufficiency")
        if sufficiency not in {"sufficient", "insufficient"}:
            sufficiency = "insufficient"
        return PersistedDiagnosticOutcome(artifact, sufficiency)


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
) -> RecoveryAuthorization:
    artifact = outcome.artifact
    decision = artifact.decision
    checks = {check.name for check in observation.checks if check.passed}
    predicates = (
        (artifact.scenario_id == SCENARIO_ID, "scenario_mismatch"),
        (observation.scenario_id == SCENARIO_ID, "observation_scenario_mismatch"),
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
        budgets: AutoClosureBudgets | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._owner_user_id = owner_user_id
        self._source_id = source_id
        self._driver = driver
        self._lifecycles = lifecycles
        self._diagnostic_loader = diagnostic_loader
        self._recovery = recovery
        self._recovery_coordinator = recovery_coordinator
        self._evidence_preparer = evidence_preparer
        self._budgets = budgets or AutoClosureBudgets()
        self._sleep = sleep

    async def run(
        self,
        scenario_id: str,
        *,
        run_id: str,
        resume: bool = False,
    ) -> LiveAutoClosureResult:
        del resume
        if scenario_id != SCENARIO_ID:
            raise ValueError("Automatic closure supports only the order-pool scenario.")
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
        try:
            try:
                await self._driver.preflight(identity)
                await self._driver.baseline(identity)
                observation = await self._driver.inject(identity)
                if observation.confirmed and self._evidence_preparer is not None:
                    evidence_context = await self._evidence_preparer.prepare(
                        identity,
                        observation,
                    )
            except Exception:
                return await self._finish(
                    identity,
                    validity="INFRA_INVALID",
                    authorization_code="fixture_infrastructure_failed",
                    observation=observation,
                )
            if not observation.confirmed:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code="fault_observation_incomplete",
                    observation=observation,
                )
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
                )
            lifecycle = await self._wait_lifecycle(
                run_id,
                seconds=self._budgets.diagnosis_seconds,
                require_report=True,
            )
            if lifecycle is None:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code="diagnostic_report_timeout",
                    observation=observation,
                )
            outcome = await self._diagnostic_loader.load(
                owner_user_id=self._owner_user_id,
                diagnostic_task_id=lifecycle.diagnostic_task_id,
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
            )
            if not authorization.execution_permitted:
                return await self._finish(
                    identity,
                    validity="VALID_FAIL",
                    authorization_code=authorization.code,
                    lifecycle=lifecycle,
                    observation=observation,
                    outcome=outcome,
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
                )
            recovery = _recovery_from_payload(coordinated.output)
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
                )
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
            if not verification.passed:
                validity = "VALID_FAIL"
                authorization_code = "independent_verification_failed"
            else:
                lifecycle = await self._wait_resolved(identity.run_id)
                if lifecycle is None:
                    validity = "VALID_FAIL"
                    authorization_code = "alert_resolved_timeout"
                else:
                    validity = "VALID_PASS"
                    authorization_code = "verified_closed"
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
        )

    async def _wait_lifecycle(
        self,
        run_id: str,
        *,
        seconds: float,
        require_report: bool,
    ) -> LiveAlertLifecycle | None:
        deadline = monotonic() + max(0, seconds)
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
            if monotonic() >= deadline:
                return None
            await self._sleep(self._budgets.poll_seconds)

    async def _wait_resolved(self, run_id: str) -> LiveAlertLifecycle | None:
        deadline = monotonic() + max(0, self._budgets.resolved_seconds)
        while True:
            lifecycle = await self._lifecycles.get_live_lifecycle(
                owner_user_id=self._owner_user_id,
                source_id=self._source_id,
                scenario_id=SCENARIO_ID,
                run_id=run_id,
            )
            if lifecycle is not None and lifecycle.closed_verified:
                return lifecycle
            if monotonic() >= deadline:
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
