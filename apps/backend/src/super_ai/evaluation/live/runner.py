"""Lifecycle orchestration for answer-isolated Docker Live evaluations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from super_ai.evaluation.artifacts import RunArtifact
from super_ai.evaluation.live.diagnostics import append_live_outcome
from super_ai.evaluation.live.domain import (
    LiveFaultObservation,
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

    async def cleanup(self, identity: LiveRunIdentity) -> None: ...


class LiveDiagnosticAdapter(Protocol):
    async def diagnose(
        self,
        *,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
    ) -> object: ...


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
        diagnostic: LiveDiagnosticAdapter,
        recovery: LiveRecoveryService,
        evaluator: LiveEvaluator[EvaluationT],
    ) -> None:
        self._scenario_root = scenario_root.resolve()
        self._driver = driver
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
            diagnostic_artifact = await self._classified(
                self._diagnostic.diagnose(
                    run_id=identity.run_id,
                    scenario=scenario,
                    observation=observation,
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
            if not (recovery.authorized and recovery.executed):
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
            oracle = load_live_oracle(scenario_dir)
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
                await self._driver.cleanup(identity)
            except BaseException as cleanup_exc:
                if isinstance(cleanup_exc, (KeyboardInterrupt, SystemExit)):
                    raise
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
        except Exception as exc:
            raise LiveBenchmarkError(category) from exc
