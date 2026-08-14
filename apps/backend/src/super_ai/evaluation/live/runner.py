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

    def __init__(self, category: str) -> None:
        super().__init__("Docker Live benchmark failed at a classified boundary.")
        self.category = category


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
            await self._classified(self._driver.preflight(identity), "preflight_failed")
            await self._classified(self._driver.baseline(identity), "baseline_failed")
            observation = await self._classified(
                self._driver.inject(identity), "fault_injection_failed"
            )
            if not observation.confirmed:
                raise LiveBenchmarkError("fault_injection_failed")
            evidence_context = await self._classified(
                self._evidence_preparer.prepare(
                    identity=identity,
                    scenario=scenario,
                    observation=observation,
                ),
                "evidence_preparation_failed",
            )
            diagnostic_artifact = await self._classified(
                self._diagnostic.diagnose(
                    run_id=identity.run_id,
                    scenario=scenario,
                    observation=observation,
                    evidence_context=evidence_context,
                ),
                "diagnostic_failed",
            )
            recovery = await self._classified(
                self._recovery.recover(
                    identity=identity,
                    diagnostic_artifact=diagnostic_artifact,
                    observation=observation,
                ),
                "recovery_failed",
            )
            oracle = load_live_oracle(scenario_dir)
            if (
                oracle.recovery_expectation is None
                or recovery.expectation != oracle.recovery_expectation
                or not _recovery_contract_satisfied(recovery)
            ):
                raise LiveBenchmarkError("recovery_denied")
            verification = await self._classified(
                self._driver.verify(identity), "recovery_verification_failed"
            )
            if not verification.passed:
                raise LiveBenchmarkError("recovery_verification_failed")
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
                raise LiveBenchmarkError("evaluation_failed") from exc
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            try:
                cleanup = await self._driver.cleanup(identity)
                if not cleanup.passed:
                    raise LiveBenchmarkError("cleanup_failed")
            except BaseException as cleanup_exc:
                if isinstance(cleanup_exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(cleanup_exc, LiveBenchmarkError):
                    raise cleanup_exc from active_error
                raise LiveBenchmarkError("cleanup_failed") from (
                    cleanup_exc if active_error is None else active_error
                )

    @staticmethod
    async def _classified(awaitable: Awaitable[AwaitedT], category: str) -> AwaitedT:
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except LiveBenchmarkError:
            raise
        except LiveInfrastructureError as exc:
            raise LiveBenchmarkError(exc.category) from exc
        except Exception as exc:
            raise LiveBenchmarkError(category) from exc


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
