"""Archive-first coordination for durable evaluation run history."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from super_ai.evaluation.artifacts import InvestigationBenchmarkMetrics
from super_ai.evaluation.history import (
    EvaluationRunEnvelope,
    artifact_checksum,
    running_from_terminal,
)
from super_ai.evaluation.persistence import EvaluationDatabaseUnavailable
from super_ai.memory.repositories import EvaluationResultRecord, EvaluationRunRecord

_INVESTIGATION_STRATEGIES = frozenset(
    {"auto", "single", "multi"}
)
_EFFECTIVE_INVESTIGATION_STRATEGIES = frozenset(
    {"deterministic_fast_path", "single_agent", "multi_agent"}
)
_INVESTIGATION_FALLBACK_REASONS = frozenset(
    {"fallback_to_single_agent", "manual_review_required", "late_result_ignored"}
)


class EvaluationArchiveWriter(Protocol):
    def start(self, envelope: EvaluationRunEnvelope) -> Path: ...

    def finalize(self, envelope: EvaluationRunEnvelope) -> Path: ...


class EvaluationDatabaseWriter(Protocol):
    async def start_envelope(self, envelope: EvaluationRunEnvelope) -> object: ...

    async def finalize_envelope(
        self,
        envelope: EvaluationRunEnvelope,
        *,
        artifact_checksum: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RecordingOutcome:
    """Persistence result without leaking database exception details."""

    database_pending: bool


class EvaluationRunRecorder:
    """Write local artifacts first, then best-effort synchronize PostgreSQL."""

    def __init__(
        self,
        *,
        archive: EvaluationArchiveWriter,
        repository: EvaluationDatabaseWriter,
    ) -> None:
        self._archive = archive
        self._repository = repository

    async def start(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        self._archive.start(envelope)
        try:
            await self._repository.start_envelope(envelope)
        except EvaluationDatabaseUnavailable:
            return RecordingOutcome(database_pending=True)
        return RecordingOutcome(database_pending=False)

    async def finish(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        self._archive.finalize(envelope)
        try:
            await self._repository.start_envelope(running_from_terminal(envelope))
            await self._repository.finalize_envelope(
                envelope,
                artifact_checksum=artifact_checksum(envelope),
            )
        except EvaluationDatabaseUnavailable:
            return RecordingOutcome(database_pending=True)
        return RecordingOutcome(database_pending=False)

    async def fail(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        """Persist a classified terminal failure through the same safe path."""
        return await self.finish(envelope)


def investigation_metrics_from_persisted_result(
    run: EvaluationRunRecord,
    result: EvaluationResultRecord | None,
) -> InvestigationBenchmarkMetrics:
    """Rebuild safe A/B inputs using only PostgreSQL terminal records."""
    if result is None:
        raise ValueError("Investigation benchmark metrics require a terminal result.")
    metadata = run.run_metadata
    metrics = result.metrics
    strategy = _required_choice(
        metadata, "investigationStrategy", _INVESTIGATION_STRATEGIES
    )
    policy_version = _required_text(metadata, "investigationPolicyVersion")
    fallback = metrics.get("fallbackReason")
    if fallback is not None and fallback not in _INVESTIGATION_FALLBACK_REASONS:
        raise ValueError("Investigation fallback reason is invalid.")
    return InvestigationBenchmarkMetrics(
        strategy=strategy,
        effective_strategy=_required_choice(
            metrics,
            "effectiveInvestigationStrategy",
            _EFFECTIVE_INVESTIGATION_STRATEGIES,
        ),
        policy_version=policy_version,
        root_cause_top1_correct=_required_bool(metrics, "rootCauseTop1Correct"),
        evidence_recall_basis_points=_required_bounded_int(
            metrics, "evidenceRecallBasisPoints", maximum=10_000
        ),
        duration_ms=_required_bounded_int(metrics, "durationMs", maximum=86_400_000),
        model_call_count=_required_bounded_int(metrics, "modelCallCount", maximum=8),
        duplicate_evidence_basis_points=_required_bounded_int(
            metrics, "duplicateEvidenceBasisPoints", maximum=10_000
        ),
        fallback_reason=fallback if isinstance(fallback, str) else None,
        security_hard_gate_passed=_required_bool(
            metrics, "securityHardGatePassed"
        ),
    )


def _required_text(values: dict[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Investigation metric {key} is invalid.")
    return value


def _required_choice(
    values: dict[str, object], key: str, allowed: frozenset[str]
) -> str:
    value = _required_text(values, key)
    if value not in allowed:
        raise ValueError(f"Investigation metric {key} is invalid.")
    return value


def _required_bool(values: dict[str, object], key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Investigation metric {key} is invalid.")
    return value


def _required_bounded_int(
    values: dict[str, object], key: str, *, maximum: int
) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"Investigation metric {key} is invalid.")
    return value
