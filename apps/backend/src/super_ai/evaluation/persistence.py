"""Application-facing persistence for AgentPy benchmark runs and scorecards."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from sqlalchemy.exc import (
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.evaluation.history import EvaluationRunEnvelope
from super_ai.evaluation.scoring import EvaluationResult
from super_ai.memory.repositories import (
    EVALUATION_FAILURE_CATEGORIES,
    DiagnosticTaskRecord,
    EvaluationFailureStatus,
    EvaluationResultRecord,
    EvaluationRunRecord,
    JsonDict,
)
from super_ai.memory.sqlalchemy import SQLAlchemyEvaluationRepository

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "accesskey",
        "secret",
        "secret_key",
        "secretkey",
        "password",
        "token",
    }
)


class EvaluationDatabaseUnavailable(RuntimeError):
    """The evaluation database cannot currently accept a transaction."""


class EvaluationRepository:
    """Persist public run metadata and deterministic results without credentials."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._repository = SQLAlchemyEvaluationRepository(session_factory)

    async def start_envelope(self, envelope: EvaluationRunEnvelope) -> EvaluationRunRecord:
        if envelope.status != "running":
            raise ValueError("Only a running evaluation envelope can be started.")
        try:
            return await self._repository.start_envelope(
                run_id=envelope.run_id,
                evaluation_kind=envelope.evaluation_kind,
                artifact_schema_version=envelope.artifact_schema_version,
                provenance=envelope.provenance,
                run_metadata=envelope.metadata,
                scenario_id=envelope.scenario_id,
                suite_version=envelope.suite_version,
                created_at=envelope.created_at,
                started_at=envelope.started_at,
            )
        except (InterfaceError, OperationalError, SQLAlchemyTimeoutError) as exc:
            raise EvaluationDatabaseUnavailable("Evaluation database is unavailable.") from exc

    async def finalize_envelope(
        self,
        envelope: EvaluationRunEnvelope,
        *,
        artifact_checksum: str,
    ) -> tuple[EvaluationRunRecord, EvaluationResultRecord | None]:
        if envelope.status == "running" or envelope.completed_at is None:
            raise ValueError("Only a terminal evaluation envelope can be finalized.")
        try:
            return await self._repository.finalize_envelope(
                run_id=envelope.run_id,
                artifact_checksum=artifact_checksum,
                status=envelope.status,
                validity=envelope.validity,
                passed=envelope.passed,
                metrics=envelope.metrics,
                result_payload=envelope.result_payload,
                diagnostic_task_id=envelope.diagnostic_task_id,
                failure_category=envelope.failure_category,
                completed_at=envelope.completed_at,
            )
        except (InterfaceError, OperationalError, SQLAlchemyTimeoutError) as exc:
            raise EvaluationDatabaseUnavailable("Evaluation database is unavailable.") from exc

    async def attach_artifact_checksum(
        self, *, run_id: str, artifact_checksum: str
    ) -> EvaluationRunRecord:
        try:
            return await self._repository.attach_artifact_checksum(
                run_id=run_id,
                artifact_checksum=artifact_checksum,
            )
        except (InterfaceError, OperationalError, SQLAlchemyTimeoutError) as exc:
            raise EvaluationDatabaseUnavailable("Evaluation database is unavailable.") from exc

    async def list_runs_with_results(
        self,
    ) -> list[tuple[EvaluationRunRecord, EvaluationResultRecord | None]]:
        return await self._repository.list_runs_with_results()

    async def list_benchmark_diagnostic_tasks(self) -> list[DiagnosticTaskRecord]:
        return await self._repository.list_benchmark_diagnostic_tasks()

    async def create_run(
        self,
        *,
        run_id: str,
        scenario_id: str,
        mode: str,
        suite_version: str,
        agent_version: JsonDict,
        model_configuration: JsonDict,
        created_at: datetime | None = None,
    ) -> EvaluationRunRecord:
        if _contains_secret_key(model_configuration):
            raise ValueError("Model configuration must not contain secrets.")
        return await self._repository.create_run(
            run_id=run_id,
            scenario_id=scenario_id,
            mode=mode,
            suite_version=suite_version,
            agent_version=agent_version,
            model_configuration=model_configuration,
            created_at=created_at,
        )

    async def complete_run(
        self,
        *,
        run_id: str,
        diagnostic_task_id: str | None,
        completed_at: datetime | None = None,
    ) -> EvaluationRunRecord:
        return await self._repository.complete_run(
            run_id=run_id,
            diagnostic_task_id=diagnostic_task_id,
            completed_at=completed_at,
        )

    async def fail_run(
        self,
        *,
        run_id: str,
        status: EvaluationFailureStatus,
        failure_category: str,
        completed_at: datetime | None = None,
    ) -> EvaluationRunRecord:
        if status not in {"agent_failed", "infra_failed"}:
            raise ValueError(f"Unsupported evaluation failure status: {status}")
        if failure_category not in EVALUATION_FAILURE_CATEGORIES:
            raise ValueError(f"Unsupported evaluation failure category: {failure_category}")
        return await self._repository.fail_run(
            run_id=run_id,
            status=status,
            failure_category=failure_category,
            completed_at=completed_at,
        )

    async def save_result(
        self,
        *,
        result_id: str,
        run_id: str,
        result: EvaluationResult,
        created_at: datetime | None = None,
    ) -> EvaluationResultRecord:
        return await self._repository.save_result(
            result_id=result_id,
            run_id=run_id,
            dimension_scores={
                "outcome": result.outcome,
                "diagnosis": result.diagnosis,
                "evidence": result.evidence,
                "process": result.process,
                "safety": result.safety,
                "efficiency": result.efficiency,
            },
            total=result.total,
            raw_total=result.raw_total,
            validity=result.validity,
            passed=result.passed,
            failures=list(result.failures),
            score_reasons=[
                {
                    "code": reason.code,
                    "points": reason.points,
                    "maximum": reason.maximum,
                    "evidence_ids": list(reason.evidence_ids),
                }
                for reason in result.reasons
            ],
            hard_gate=result.hard_gate,
            created_at=created_at,
        )

    async def finalize_run(
        self,
        *,
        run_id: str,
        result_id: str,
        result: EvaluationResult,
        diagnostic_task_id: str | None,
        completed_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> tuple[EvaluationRunRecord, EvaluationResultRecord]:
        return await self._repository.finalize_run(
            run_id=run_id,
            result_id=result_id,
            dimension_scores={
                "outcome": result.outcome,
                "diagnosis": result.diagnosis,
                "evidence": result.evidence,
                "process": result.process,
                "safety": result.safety,
                "efficiency": result.efficiency,
            },
            total=result.total,
            raw_total=result.raw_total,
            validity=result.validity,
            passed=result.passed,
            failures=list(result.failures),
            score_reasons=[
                {
                    "code": reason.code,
                    "points": reason.points,
                    "maximum": reason.maximum,
                    "evidence_ids": list(reason.evidence_ids),
                }
                for reason in result.reasons
            ],
            hard_gate=result.hard_gate,
            diagnostic_task_id=diagnostic_task_id,
            completed_at=completed_at,
            created_at=created_at,
        )

    async def get_run_with_result(
        self, run_id: str
    ) -> tuple[EvaluationRunRecord, EvaluationResultRecord | None] | None:
        return await self._repository.get_run_with_result(run_id)


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(
            _normalize_key(str(key)) in _SECRET_KEYS or _contains_secret_key(item)
            for key, item in mapping.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return any(_contains_secret_key(item) for item in sequence)
    return False


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")
