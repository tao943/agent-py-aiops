"""Resilient root-cause validation using only public, persisted evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Literal, cast

from super_ai.aiops.reasoning import RootCauseDecision

DecisionValidationErrorCategory = Literal[
    "candidate_missing",
    "deterministic_gap",
    "model_call_failed",
    "invalid_model_output",
    "model_rejected",
    "retry_exhausted",
]
DeterministicCheckCode = Literal[
    "unique_supported_hypothesis",
    "no_open_competitor",
    "public_label_match",
    "task_evidence_only",
    "supporting_evidence_only",
    "independent_positive_evidence",
    "supporting_observations",
    "grounded_causal_chain",
    "trigger_present",
    "confidence_in_range",
]
RootCauseField = Literal["component", "mechanism", "trigger", "causalChain"]


@dataclass(frozen=True, slots=True)
class DeterministicCheck:
    code: DeterministicCheckCode
    passed: bool


@dataclass(frozen=True, slots=True)
class DeterministicValidationResult:
    passed: bool
    supported_hypothesis_id: str | None
    checks: tuple[DeterministicCheck, ...]
    unsupported_fields: tuple[RootCauseField, ...]
    missing_evidence: tuple[str, ...]


def validate_grounded_candidate(
    *,
    candidate: RootCauseDecision,
    available_evidence_ids: Set[str],
    hypothesis_states: Sequence[Mapping[str, object]],
    observation_decisions: Sequence[Mapping[str, object]],
    decision_vocabulary: Mapping[str, object],
) -> DeterministicValidationResult:
    """Validate one candidate without model output, RAG prose, or hidden answers."""
    supported = _hypothesis_ids_with_status(hypothesis_states, "supported")
    supported_hypothesis_id = supported[0] if len(supported) == 1 else None
    open_hypotheses = _hypothesis_ids_with_status(hypothesis_states, "open")
    unique_supported = supported_hypothesis_id is not None
    no_open_competitor = not open_hypotheses

    labels = _mapping(decision_vocabulary.get("labelsByHypothesis"))
    public_label = _mapping(
        labels.get(supported_hypothesis_id) if supported_hypothesis_id is not None else None
    )
    public_label_match = bool(public_label) and (
        public_label.get("component") == candidate.component
        and public_label.get("mechanism") == candidate.mechanism
    )

    candidate_evidence = set(candidate.evidence_ids)
    task_evidence_only = bool(candidate_evidence) and candidate_evidence.issubset(
        set(available_evidence_ids)
    )
    supporting_observations = _supporting_observations(
        observation_decisions,
        supported_hypothesis_id=supported_hypothesis_id,
    )
    positive_evidence_ids = {
        evidence_id
        for observation in supporting_observations
        for evidence_id in _string_items(observation.get("evidenceIds"))
        if evidence_id in available_evidence_ids
    }
    supporting_evidence_only = bool(candidate_evidence) and candidate_evidence.issubset(
        positive_evidence_ids
    )
    independent_positive_evidence = len(positive_evidence_ids) >= 2
    enough_supporting_observations = len(supporting_observations) >= 2
    supporting_summaries = {
        summary.strip()
        for observation in supporting_observations
        if isinstance((summary := observation.get("summary")), str) and summary.strip()
    }
    grounded_causal_chain = (
        2 <= len(candidate.causal_chain) <= 6
        and all(
            item.strip() and item.strip() in supporting_summaries
            for item in candidate.causal_chain
        )
    )
    trigger_present = bool(candidate.trigger.strip())
    confidence_in_range = 0.0 <= candidate.confidence <= 1.0

    checks = (
        DeterministicCheck("unique_supported_hypothesis", unique_supported),
        DeterministicCheck("no_open_competitor", no_open_competitor),
        DeterministicCheck("public_label_match", public_label_match),
        DeterministicCheck("task_evidence_only", task_evidence_only),
        DeterministicCheck("supporting_evidence_only", supporting_evidence_only),
        DeterministicCheck(
            "independent_positive_evidence", independent_positive_evidence
        ),
        DeterministicCheck(
            "supporting_observations", enough_supporting_observations
        ),
        DeterministicCheck("grounded_causal_chain", grounded_causal_chain),
        DeterministicCheck("trigger_present", trigger_present),
        DeterministicCheck("confidence_in_range", confidence_in_range),
    )
    unsupported_fields: list[RootCauseField] = []
    if not public_label_match:
        unsupported_fields.extend(("component", "mechanism"))
    if not grounded_causal_chain:
        unsupported_fields.append("causalChain")
    if not trigger_present:
        unsupported_fields.append("trigger")
    evidence_check_codes = {
        "unique_supported_hypothesis",
        "no_open_competitor",
        "task_evidence_only",
        "supporting_evidence_only",
        "independent_positive_evidence",
        "supporting_observations",
    }
    missing_evidence = tuple(
        check.code
        for check in checks
        if not check.passed and check.code in evidence_check_codes
    )
    return DeterministicValidationResult(
        passed=all(check.passed for check in checks),
        supported_hypothesis_id=supported_hypothesis_id,
        checks=checks,
        unsupported_fields=tuple(dict.fromkeys(unsupported_fields)),
        missing_evidence=missing_evidence,
    )


def deterministic_checks_payload(
    result: DeterministicValidationResult,
) -> list[dict[str, object]]:
    """Return a secret-safe allowlisted audit representation."""
    return [{"code": check.code, "passed": check.passed} for check in result.checks]


def _hypothesis_ids_with_status(
    states: Sequence[Mapping[str, object]],
    status: str,
) -> tuple[str, ...]:
    return tuple(
        identifier
        for item in states
        if item.get("status") == status
        and isinstance((identifier := item.get("id")), str)
        and identifier
    )


def _supporting_observations(
    observations: Sequence[Mapping[str, object]],
    *,
    supported_hypothesis_id: str | None,
) -> tuple[Mapping[str, object], ...]:
    if supported_hypothesis_id is None:
        return ()
    return tuple(
        item
        for item in observations
        if supported_hypothesis_id in _string_items(item.get("supports"))
    )


def _string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in cast(Sequence[object], value) if isinstance(item, str))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in cast(Mapping[object, object], value).items()
        if isinstance(key, str)
    }
