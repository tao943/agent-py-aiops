"""Lifecycle orchestration for answer-isolated Docker Live evaluations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast

from super_ai.aiops.execution import (
    ExecutionCoordinator,
    ExecutionIdentity,
    UnsafeExecutionReplay,
)
from super_ai.evaluation.artifacts import InvestigationAudit, RunArtifact
from super_ai.evaluation.live.diagnostics import append_live_outcome
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveCleanupResult,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveInfrastructureError,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveScenario,
    LiveVerification,
    RecoveryExpectation,
)
from super_ai.evaluation.live.failure_diagnostics import LiveFailureDiagnostics
from super_ai.evaluation.live.scenarios import (
    load_live_oracle,
    load_live_scenario,
    resolve_live_scenario_directory,
    validate_run_id,
)

EvaluationT = TypeVar("EvaluationT")
EvaluationT_co = TypeVar("EvaluationT_co", covariant=True)
AwaitedT = TypeVar("AwaitedT")


class LiveBenchmarkError(RuntimeError):
    """Safe classified failure at a Live evaluation boundary."""

    def __init__(
        self,
        category: str,
        *,
        stage: str | None = None,
        authorization_code: str | None = None,
        cleanup_succeeded: bool | None = None,
        diagnostics: LiveFailureDiagnostics | None = None,
        diagnostic_task_id: str | None = None,
        investigation_audit: InvestigationAudit | None = None,
    ) -> None:
        super().__init__("Docker Live benchmark failed at a classified boundary.")
        self.category = category
        self.stage = stage
        self.authorization_code = authorization_code
        self.cleanup_succeeded = cleanup_succeeded
        self.diagnostics = diagnostics
        self.diagnostic_task_id = diagnostic_task_id
        self.investigation_audit = investigation_audit


class LiveScenarioDriver(Protocol):
    async def preflight(self, identity: LiveRunIdentity) -> None: ...

    async def baseline(self, identity: LiveRunIdentity) -> None: ...

    async def inject(self, identity: LiveRunIdentity) -> LiveFaultObservation: ...

    async def verify(self, identity: LiveRunIdentity) -> LiveVerification: ...

    async def cleanup(self, identity: LiveRunIdentity) -> LiveCleanupResult: ...


class LiveDiagnosticAdapter(Protocol):
    async def diagnose(
        self,
        *,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        evidence_context: LiveEvidenceContext,
    ) -> object: ...


class LiveEvidencePreparer(Protocol):
    async def prepare(
        self,
        *,
        identity: LiveRunIdentity,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
    ) -> LiveEvidenceContext: ...


class LocalLiveEvidencePreparer:
    """Prepare the explicit no-network evidence context."""

    async def prepare(
        self,
        *,
        identity: LiveRunIdentity,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
    ) -> LiveEvidenceContext:
        del observation
        return LiveEvidenceContext.local(incident_id=f"{scenario.id}-{identity.run_id}")


class LiveRecoveryService(Protocol):
    async def recover(
        self,
        *,
        identity: LiveRunIdentity,
        diagnostic_artifact: object,
        observation: LiveFaultObservation,
    ) -> LiveRecoveryRecord: ...


class LiveEvaluator(Protocol[EvaluationT_co]):
    def evaluate(
        self,
        *,
        diagnostic_artifact: object,
        observation: LiveFaultObservation,
        recovery: LiveRecoveryRecord,
        verification: LiveVerification,
        oracle: object,
    ) -> EvaluationT_co: ...


class LiveBenchmarkRunner(Generic[EvaluationT]):
    """Run one Live scenario and guarantee scoped cleanup."""

    def __init__(
        self,
        *,
        scenario_root: Path,
        driver: LiveScenarioDriver,
        evidence_preparer: LiveEvidencePreparer,
        diagnostic: LiveDiagnosticAdapter,
        recovery: LiveRecoveryService,
        evaluator: LiveEvaluator[EvaluationT],
        recovery_coordinator: ExecutionCoordinator | None = None,
        recovery_coordinator_factory: Callable[[str], ExecutionCoordinator]
        | None = None,
    ) -> None:
        self._scenario_root = scenario_root.resolve()
        self._driver = driver
        self._evidence_preparer = evidence_preparer
        self._diagnostic = diagnostic
        self._recovery = recovery
        self._evaluator = evaluator
        self._recovery_coordinator = recovery_coordinator
        self._recovery_coordinator_factory = recovery_coordinator_factory

    async def run(self, scenario_id: str, *, run_id: str) -> EvaluationT:
        identity = validate_run_id(run_id)
        scenario_dir = resolve_live_scenario_directory(self._scenario_root, scenario_id)
        scenario = load_live_scenario(scenario_dir)
        if scenario.id != scenario_id:
            raise ValueError("Live scenario ID must match its directory name.")

        active_error: BaseException | None = None
        diagnostic_artifact: object | None = None
        try:
            await self._classified(
                self._driver.preflight(identity), "preflight_failed", "preflight"
            )
            await self._classified(
                self._driver.baseline(identity), "baseline_failed", "baseline"
            )
            observation = await self._classified(
                self._driver.inject(identity), "fault_injection_failed", "inject"
            )
            if not observation.confirmed:
                raise LiveBenchmarkError(
                    "fault_injection_failed",
                    stage="inject",
                    diagnostics=LiveFailureDiagnostics.from_observation(observation),
                )
            evidence_context = await self._classified(
                self._evidence_preparer.prepare(
                    identity=identity,
                    scenario=scenario,
                    observation=observation,
                ),
                "evidence_preparation_failed",
                "evidence",
            )
            diagnostic_artifact = await self._classified(
                self._diagnostic.diagnose(
                    run_id=identity.run_id,
                    scenario=scenario,
                    observation=observation,
                    evidence_context=evidence_context,
                ),
                "diagnostic_failed",
                "diagnose",
            )
            async def recover_once() -> dict[str, object]:
                record = await self._recovery.recover(
                    identity=identity,
                    diagnostic_artifact=diagnostic_artifact,
                    observation=observation,
                )
                return _recovery_payload(record)

            try:
                recovery_coordinator = self._recovery_coordinator
                if (
                    recovery_coordinator is None
                    and self._recovery_coordinator_factory is not None
                    and isinstance(diagnostic_artifact, RunArtifact)
                    and diagnostic_artifact.diagnostic_task_id is not None
                ):
                    recovery_coordinator = self._recovery_coordinator_factory(
                        diagnostic_artifact.diagnostic_task_id
                    )
                if recovery_coordinator is None:
                    recovery = _recovery_from_payload(await recover_once())
                else:
                    coordinated = await recovery_coordinator.run_once(
                        ExecutionIdentity(
                            task_id=identity.run_id,
                            graph_version="live-eval-v1",
                            node_name="recovery",
                            logical_iteration=0,
                            input_payload={
                                "runId": identity.run_id,
                                "scenarioId": scenario.id,
                                "safeFacts": dict(observation.safe_facts),
                            },
                            execution_kind="recovery",
                            side_effecting=True,
                        ),
                        recover_once,
                        outcome_known_on_error=False,
                    )
                    recovery = _recovery_from_payload(coordinated.output)
            except UnsafeExecutionReplay as exc:
                raise LiveBenchmarkError(
                    "recovery_denied",
                    stage="recover",
                    authorization_code="uncertain_previous_attempt",
                ) from exc
            except LiveBenchmarkError:
                raise
            except Exception as exc:
                raise LiveBenchmarkError("recovery_failed", stage="recover") from exc
            oracle = load_live_oracle(scenario_dir)
            if (
                oracle.recovery_expectation is None
                or recovery.expectation != oracle.recovery_expectation
                or not _recovery_contract_satisfied(recovery)
            ):
                raise LiveBenchmarkError(
                    "recovery_denied",
                    stage="recover",
                    authorization_code=recovery.authorization_code,
                )
            verification = await self._classified(
                self._driver.verify(identity), "recovery_verification_failed", "verify"
            )
            if not verification.passed:
                raise LiveBenchmarkError(
                    "recovery_verification_failed",
                    stage="verify",
                )
            if isinstance(diagnostic_artifact, RunArtifact):
                diagnostic_artifact = append_live_outcome(
                    diagnostic_artifact,
                    recovery=recovery,
                    verification=verification,
                )
            try:
                return self._evaluator.evaluate(
                    diagnostic_artifact=diagnostic_artifact,
                    observation=observation,
                    recovery=recovery,
                    verification=verification,
                    oracle=oracle,
                )
            except Exception as exc:
                raise LiveBenchmarkError("evaluation_failed", stage="evaluate") from exc
        except BaseException as exc:
            if isinstance(exc, LiveBenchmarkError) and isinstance(
                diagnostic_artifact, RunArtifact
            ):
                exc.diagnostic_task_id = diagnostic_artifact.diagnostic_task_id
                exc.investigation_audit = diagnostic_artifact.investigation_audit
            active_error = exc
            raise
        finally:
            try:
                cleanup = await self._driver.cleanup(identity)
                if not cleanup.passed:
                    raise LiveBenchmarkError(
                        "cleanup_failed",
                        stage="cleanup",
                        cleanup_succeeded=False,
                    )
            except BaseException as cleanup_exc:
                if isinstance(cleanup_exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if active_error is not None:
                    active_error.cleanup_succeeded = False  # pyright: ignore[reportAttributeAccessIssue]
                elif isinstance(cleanup_exc, LiveBenchmarkError):
                    cleanup_exc.cleanup_succeeded = False
                    raise cleanup_exc from active_error
                else:
                    raise LiveBenchmarkError(
                        "cleanup_failed",
                        stage="cleanup",
                        cleanup_succeeded=False,
                    ) from cleanup_exc
            else:
                if active_error is not None:
                    active_error.cleanup_succeeded = True  # pyright: ignore[reportAttributeAccessIssue]

    @staticmethod
    async def _classified(
        awaitable: Awaitable[AwaitedT],
        category: str,
        stage: str,
    ) -> AwaitedT:
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except LiveBenchmarkError:
            raise
        except LiveInfrastructureError as exc:
            raise LiveBenchmarkError(exc.category, stage=stage) from exc
        except Exception as exc:
            raise LiveBenchmarkError(category, stage=stage) from exc


def _recovery_contract_satisfied(record: LiveRecoveryRecord) -> bool:
    if not record.authorized:
        return False
    if record.expectation == "executed_recovery":
        return record.executed
    return (
        not record.executed
        and bool(record.proposal_checks)
        and all(check.passed for check in record.proposal_checks)
    )


def _recovery_payload(record: LiveRecoveryRecord) -> dict[str, object]:
    return {
        "action": record.action,
        "targetRef": record.target_ref,
        "expectation": record.expectation,
        "authorized": record.authorized,
        "executed": record.executed,
        "authorizationCode": record.authorization_code,
        "proposalChecks": [
            {"name": item.name, "passed": item.passed, "source": item.source}
            for item in record.proposal_checks
        ],
    }


def _recovery_from_payload(payload: dict[str, object]) -> LiveRecoveryRecord:
    expectation = payload.get("expectation")
    if expectation not in {"executed_recovery", "proposal_only"}:
        raise ValueError("Recovery payload expectation is invalid.")
    raw_checks = payload.get("proposalChecks")
    check_items = cast(list[object], raw_checks) if isinstance(raw_checks, list) else []
    checks: list[LiveCheck] = []
    for raw in check_items:
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        source = item.get("source")
        checks.append(
            LiveCheck(
                name=name,
                passed=item.get("passed") is True,
                source=source if isinstance(source, str) else "driver",
            )
        )
    return LiveRecoveryRecord(
        action=str(payload.get("action") or ""),
        target_ref=str(payload.get("targetRef") or ""),
        expectation=cast(RecoveryExpectation, expectation),
        authorized=payload.get("authorized") is True,
        executed=payload.get("executed") is True,
        authorization_code=str(payload.get("authorizationCode") or ""),
        proposal_checks=tuple(checks),
    )
