"""Archive-first coordination for durable evaluation run history."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

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
    {
        "deterministic_fast_path",
        "single_agent",
        "multi_agent",
        "multi_agent_unavailable",
    }
)
_INVESTIGATION_FALLBACK_REASONS = frozenset(
    {
        "fallback_to_single_agent",
        "manual_review_required",
        "late_result_ignored",
        "partial_specialist_result",
        "multi_investigation_failed",
    }
)
_SPECIALIST_ROLES = frozenset({"runtime", "log", "change", "knowledge"})
_SPECIALIST_STATUSES = frozenset(
    {"completed", "inconclusive", "failed", "timeout", "cancelled", "missing"}
)
_SPECIALIST_EVIDENCE_STATUSES = frozenset({"complete", "partial", "none"})
_SPECIALIST_ANALYSIS_STATUSES = frozenset(
    {"complete", "degraded", "timeout", "failed", "skipped"}
)
_SPECIALIST_ANALYSIS_ERROR_CODES = frozenset(
    {
        "parse_error",
        "schema_validation_failed",
        "scope_rejected",
        "provider_4xx",
        "provider_5xx",
        "provider_timeout",
        "retry_exhausted",
        "retry_skipped_insufficient_deadline",
        "specialist_soft_deadline_expired",
        "specialist_hard_deadline_expired",
        "specialist_model_budget_exhausted",
    }
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
        total_score=_required_bounded_int(metrics, "total", maximum=100),
        tool_call_count=_required_bounded_int(
            metrics, "toolCallCount", maximum=64
        ),
        role_statuses=_required_role_statuses(metrics, "specialistRoleStatuses"),
        role_duration_ms=_required_role_counts(
            metrics, "specialistRoleDurationMs", maximum=360_000
        ),
        role_model_call_counts=_required_role_counts(
            metrics, "specialistRoleModelCallCounts", maximum=2
        ),
        role_tool_call_counts=_required_role_counts(
            metrics, "specialistRoleToolCallCounts", maximum=16
        ),
        role_evidence_counts=_required_role_counts(
            metrics, "specialistRoleEvidenceCounts", maximum=16
        ),
        role_evidence_statuses=_optional_role_choices(
            metrics,
            "specialistEvidenceStatuses",
            allowed=_SPECIALIST_EVIDENCE_STATUSES,
        ),
        role_analysis_statuses=_optional_role_choices(
            metrics,
            "specialistAnalysisStatuses",
            allowed=_SPECIALIST_ANALYSIS_STATUSES,
        ),
        role_analysis_error_codes=_optional_role_choices(
            metrics,
            "specialistAnalysisErrorCodes",
            allowed=_SPECIALIST_ANALYSIS_ERROR_CODES,
        ),
        role_analysis_attempt_counts=_optional_role_counts(
            metrics, "specialistAnalysisAttemptCounts", maximum=2
        ),
        role_follow_up_question_counts=_optional_role_counts(
            metrics, "specialistFollowUpQuestionCounts", maximum=16
        ),
        specialist_evidence_completion_basis_points=_optional_bounded_int(
            metrics, "specialistEvidenceCompletionBasisPoints", maximum=10_000
        ),
        specialist_analysis_completion_basis_points=_optional_bounded_int(
            metrics, "specialistAnalysisCompletionBasisPoints", maximum=10_000
        ),
        specialist_degradation_basis_points=_optional_bounded_int(
            metrics, "specialistDegradationBasisPoints", maximum=10_000
        ),
        specialist_deadline_hit_basis_points=_optional_bounded_int(
            metrics, "specialistDeadlineHitBasisPoints", maximum=10_000
        ),
        specialist_structured_retry_basis_points=_optional_bounded_int(
            metrics, "specialistStructuredRetryBasisPoints", maximum=10_000
        ),
        source_group_count=_required_bounded_int(
            metrics, "sourceGroupCount", maximum=64
        ),
        duplicate_evidence_count=_required_bounded_int(
            metrics, "duplicateEvidenceCount", maximum=64
        ),
        conflict_count=_required_bounded_int(
            metrics, "conflictCount", maximum=64
        ),
        missing_domains=_required_missing_domains(metrics),
        aggregation_checksum=_optional_checksum(metrics, "aggregationChecksum"),
        terminal_failure_category=_optional_choice(
            metrics,
            "terminalFailureCategory",
            frozenset({"multi_investigation_failed"}),
        ),
        run_id=run.run_id,
        scenario_id=run.scenario_id,
        campaign_id=(
            campaign
            if isinstance(
                campaign := metadata.get("acceptanceCampaignId"), str
            )
            else None
        ),
    )


def _required_role_statuses(
    values: dict[str, object], key: str
) -> tuple[tuple[str, str], ...]:
    raw = values.get(key)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Investigation metric {key} is invalid.")
    safe = cast(Mapping[object, object], raw)
    if len(safe) > 4:
        raise ValueError(f"Investigation metric {key} is invalid.")
    statuses: list[tuple[str, str]] = []
    for role, status in sorted(safe.items(), key=lambda item: str(item[0])):
        if (
            not isinstance(role, str)
            or role not in _SPECIALIST_ROLES
            or not isinstance(status, str)
            or status not in _SPECIALIST_STATUSES
        ):
            raise ValueError(f"Investigation metric {key} is invalid.")
        statuses.append((role, status))
    return tuple(statuses)


def _required_role_counts(
    values: dict[str, object], key: str, *, maximum: int
) -> tuple[tuple[str, int], ...]:
    raw = values.get(key)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Investigation metric {key} is invalid.")
    safe = cast(Mapping[object, object], raw)
    if len(safe) > 4:
        raise ValueError(f"Investigation metric {key} is invalid.")
    counts: list[tuple[str, int]] = []
    for role, count in sorted(safe.items(), key=lambda item: str(item[0])):
        if (
            not isinstance(role, str)
            or role not in _SPECIALIST_ROLES
            or not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= maximum
        ):
            raise ValueError(f"Investigation metric {key} is invalid.")
        counts.append((role, count))
    return tuple(counts)


def _optional_role_choices(
    values: dict[str, object],
    key: str,
    *,
    allowed: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    raw = values.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError(f"Investigation metric {key} is invalid.")
    safe = cast(Mapping[object, object], raw)
    if len(safe) > 4:
        raise ValueError(f"Investigation metric {key} is invalid.")
    choices: list[tuple[str, str]] = []
    for role, value in sorted(safe.items(), key=lambda item: str(item[0])):
        if (
            not isinstance(role, str)
            or role not in _SPECIALIST_ROLES
            or not isinstance(value, str)
            or value not in allowed
        ):
            raise ValueError(f"Investigation metric {key} is invalid.")
        choices.append((role, value))
    return tuple(choices)


def _optional_role_counts(
    values: dict[str, object], key: str, *, maximum: int
) -> tuple[tuple[str, int], ...]:
    if values.get(key) is None:
        return ()
    return _required_role_counts(values, key, maximum=maximum)


def _required_missing_domains(values: dict[str, object]) -> tuple[str, ...]:
    raw = values.get("missingDomains")
    if not isinstance(raw, list):
        raise ValueError("Investigation metric missingDomains is invalid.")
    safe = cast(list[object], raw)
    if len(safe) > 4:
        raise ValueError("Investigation metric missingDomains is invalid.")
    if any(
        not isinstance(item, str) or item not in _SPECIALIST_ROLES
        for item in safe
    ):
        raise ValueError("Investigation metric missingDomains is invalid.")
    return tuple(sorted({cast(str, item) for item in safe}))


def _optional_checksum(values: dict[str, object], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"Investigation metric {key} is invalid.")
    return value


def _optional_choice(
    values: dict[str, object], key: str, allowed: frozenset[str]
) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"Investigation metric {key} is invalid.")
    return value


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


def _optional_bounded_int(
    values: dict[str, object], key: str, *, maximum: int
) -> int | None:
    if values.get(key) is None:
        return None
    return _required_bounded_int(values, key, maximum=maximum)
