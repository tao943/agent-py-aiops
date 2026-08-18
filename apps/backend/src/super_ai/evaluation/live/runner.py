"""Lifecycle orchestration for answer-isolated Docker Live evaluations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from super_ai.evaluation.artifacts import RunArtifact
from super_ai.evaluation.live.diagnostics import append_live_outcome
from super_ai.evaluation.live.domain import (
    LiveCleanupResult,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveInfrastructureError,
    LiveRecoveryRecord,
    LiveRunIdentity,
    LiveScenario,
    LiveVerification,
)
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
    ) -> None:
        super().__init__("Docker Live benchmark failed at a classified boundary.")
        self.category = category
        self.stage = stage
        self.authorization_code = authorization_code
        self.cleanup_succeeded = cleanup_succeeded


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
    ) -> None:
        self._scenario_root = scenario_root.resolve()
        self._driver = driver
        self._evidence_preparer = evidence_preparer
        self._diagnostic = diagnostic
        self._recovery = recovery
        self._evaluator = evaluator

    async def run(self, scenario_id: str, *, run_id: str) -> EvaluationT:
        identity = validate_run_id(run_id)
        scenario_dir = resolve_live_scenario_directory(self._scenario_root, scenario_id)
        scenario = load_live_scenario(scenario_dir)
        if scenario.id != scenario_id:
            raise ValueError("Live scenario ID must match its directory name.")

        active_error: BaseException | None = None
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
                raise LiveBenchmarkError("fault_injection_failed", stage="inject")
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
            recovery = await self._classified(
                self._recovery.recover(
                    identity=identity,
                    diagnostic_artifact=diagnostic_artifact,
                    observation=observation,
                ),
                "recovery_failed",
                "recover",
            )
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
