"""Deterministic, evidence-audited hypothesis adjudication."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

Disposition = Literal["supported", "refuted", "causally_inactive", "unresolved"]
AssessmentSource = Literal["deterministic", "llm_adjudicated"]
FactQuality = Literal["direct", "context"]
PredicateOperator = Literal["eq", "ne", "in", "contains", "exists", "empty", "truthy"]

_DISPOSITIONS = frozenset({"supported", "refuted", "causally_inactive", "unresolved"})
_CLOSED_DISPOSITIONS = frozenset({"supported", "refuted", "causally_inactive"})
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,80}$")


@dataclass(frozen=True, slots=True)
class DiagnosticFact:
    """One bounded fact derived from a public tool observation."""

    key: str
    value: object
    evidence_id: str
    source_tool: str
    quality: FactQuality
    public: bool = True

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.evidence_id.strip() or not self.source_tool.strip():
            raise ValueError("Diagnostic fact identity must be non-empty.")


@dataclass(frozen=True, slots=True)
class EvidencePredicate:
    left_fact: str
    operator: PredicateOperator
    expected: object = None
    right_fact: str | None = None


@dataclass(frozen=True, slots=True)
class HypothesisEvidenceRule:
    """An instantiated rule; Task 3 restricts construction to trusted templates."""

    template_id: str
    hypothesis_id: str
    predicate: EvidencePredicate
    when_true: Disposition
    reason_code: str

    @classmethod
    def for_literal(
        cls,
        *,
        template_id: str,
        hypothesis_id: str,
        predicate: EvidencePredicate,
        when_true: Disposition,
        reason_code: str,
    ) -> HypothesisEvidenceRule:
        return cls(
            template_id=template_id,
            hypothesis_id=hypothesis_id,
            predicate=predicate,
            when_true=when_true,
            reason_code=reason_code,
        )

    def __post_init__(self) -> None:
        if not self.template_id.strip() or not self.hypothesis_id.strip():
            raise ValueError("Evidence rule identity must be non-empty.")
        if self.when_true not in _CLOSED_DISPOSITIONS:
            raise ValueError("Evidence rule must produce a closed disposition.")
        if _REASON_CODE_PATTERN.fullmatch(self.reason_code) is None:
            raise ValueError("Evidence rule reason code is invalid.")


@dataclass(frozen=True, slots=True)
class HypothesisTransition:
    previous_disposition: Disposition
    next_disposition: Disposition
    evidence_ids: tuple[str, ...]
    reason_code: str
    assessment_source: AssessmentSource


@dataclass(frozen=True, slots=True)
class HypothesisAssessment:
    hypothesis_id: str
    disposition: Disposition
    evidence_ids: tuple[str, ...]
    reason_code: str
    assessment_source: AssessmentSource
    has_high_quality_conflict: bool = False
    transitions: tuple[HypothesisTransition, ...] = ()

    def __post_init__(self) -> None:
        if not self.hypothesis_id.strip():
            raise ValueError("Hypothesis ID must be non-empty.")
        if self.disposition not in _DISPOSITIONS:
            raise ValueError("Hypothesis disposition is invalid.")
        if _REASON_CODE_PATTERN.fullmatch(self.reason_code) is None:
            raise ValueError("Hypothesis reason code is invalid.")
        normalized_evidence = tuple(sorted(set(self.evidence_ids)))
        object.__setattr__(self, "evidence_ids", normalized_evidence)
        if self.disposition in _CLOSED_DISPOSITIONS and not normalized_evidence:
            raise ValueError("Closed hypothesis disposition requires public evidence.")


def reduce_hypotheses(
    *,
    assessments: Sequence[HypothesisAssessment],
    facts: Sequence[DiagnosticFact],
    rules: Sequence[HypothesisEvidenceRule],
) -> tuple[HypothesisAssessment, ...]:
    """Apply declared public-evidence rules and return ID-sorted assessments."""
    current = _assessment_map(assessments)
    public_facts = _deduplicate_public_facts(facts)
    proposed: dict[str, dict[Disposition, list[tuple[HypothesisEvidenceRule, str]]]] = {}

    for rule in sorted(rules, key=lambda item: (item.hypothesis_id, item.template_id)):
        if rule.hypothesis_id not in current:
            raise ValueError(f"Evidence rule references unknown hypothesis: {rule.hypothesis_id}.")
        matching = tuple(
            fact
            for fact in public_facts
            if fact.quality == "direct" and _predicate_matches(rule.predicate, fact)
        )
        if not matching:
            continue
        dispositions = proposed.setdefault(rule.hypothesis_id, {})
        outcomes = dispositions.setdefault(rule.when_true, [])
        outcomes.extend((rule, fact.evidence_id) for fact in matching)

    reduced: list[HypothesisAssessment] = []
    for hypothesis_id in sorted(current):
        assessment = current[hypothesis_id]
        outcomes = proposed.get(hypothesis_id, {})
        reduced.append(_apply_outcomes(assessment, outcomes))
    return tuple(reduced)


def assess_sufficiency(
    assessments: Sequence[HypothesisAssessment],
):
    """Require one supported cause and no unresolved active competitor."""
    from super_ai.aiops.reasoning import EvidenceSufficiencyDecision

    ordered = tuple(sorted(assessments, key=lambda item: item.hypothesis_id))
    _assessment_map(ordered)
    supported = tuple(
        item.hypothesis_id for item in ordered if item.disposition == "supported"
    )
    refuted = tuple(
        item.hypothesis_id
        for item in ordered
        if item.disposition in {"refuted", "causally_inactive"}
    )
    unresolved = tuple(
        item.hypothesis_id for item in ordered if item.disposition == "unresolved"
    )
    missing = tuple(
        f"public_evidence:{item.hypothesis_id}"
        for item in ordered
        if item.disposition in _CLOSED_DISPOSITIONS and not item.evidence_ids
    )
    sufficient = len(supported) == 1 and not unresolved and not missing
    evidence_ids = tuple(sorted({value for item in ordered for value in item.evidence_ids}))
    summary = (
        "Evidence supports exactly one root cause and grounds every alternative."
        if sufficient
        else "Evidence is insufficient: exactly one supported root cause and no unresolved "
        "competitor are required."
    )
    return EvidenceSufficiencyDecision(
        status="sufficient" if sufficient else "insufficient",
        evidence_ids=evidence_ids,
        supported_hypotheses=supported,
        refuted_hypotheses=refuted,
        unresolved_hypotheses=unresolved,
        missing_evidence=missing,
        recommended_tools=(),
        summary=summary,
    )


def _assessment_map(
    assessments: Sequence[HypothesisAssessment],
) -> dict[str, HypothesisAssessment]:
    result: dict[str, HypothesisAssessment] = {}
    for assessment in assessments:
        if assessment.hypothesis_id in result:
            raise ValueError(f"Duplicate hypothesis assessment: {assessment.hypothesis_id}.")
        result[assessment.hypothesis_id] = assessment
    return result


def _deduplicate_public_facts(
    facts: Sequence[DiagnosticFact],
) -> tuple[DiagnosticFact, ...]:
    deduplicated: dict[tuple[str, str, str, str], DiagnosticFact] = {}
    for fact in facts:
        if not fact.public:
            continue
        key = (fact.evidence_id, fact.key, repr(fact.value), fact.quality)
        deduplicated[key] = fact
    return tuple(deduplicated[key] for key in sorted(deduplicated))


def _predicate_matches(predicate: EvidencePredicate, fact: DiagnosticFact) -> bool:
    if fact.key != predicate.left_fact:
        return False
    if predicate.right_fact is not None:
        return False
    if predicate.operator == "eq":
        return fact.value == predicate.expected
    if predicate.operator == "ne":
        return fact.value != predicate.expected
    if predicate.operator == "exists":
        return True
    if predicate.operator == "empty":
        return fact.value in (None, "", (), [], {})
    if predicate.operator == "truthy":
        return bool(fact.value)
    if predicate.operator == "contains":
        try:
            return predicate.expected in cast(object, fact.value)  # type: ignore[operator]
        except TypeError:
            return False
    if predicate.operator == "in":
        try:
            return fact.value in cast(object, predicate.expected)  # type: ignore[operator]
        except TypeError:
            return False
    return False


def _apply_outcomes(
    assessment: HypothesisAssessment,
    outcomes: dict[Disposition, list[tuple[HypothesisEvidenceRule, str]]],
) -> HypothesisAssessment:
    if not outcomes:
        return assessment
    active = set(outcomes)
    if assessment.disposition != "unresolved":
        active.add(assessment.disposition)
    evidence_ids = tuple(
        sorted(
            set(assessment.evidence_ids)
            | {evidence_id for values in outcomes.values() for _, evidence_id in values}
        )
    )
    if len(active) > 1:
        reason_code = "high_quality_evidence_conflict"
        next_disposition: Disposition = "unresolved"
        source: AssessmentSource = "deterministic"
        conflict = True
    else:
        next_disposition = next(iter(active))
        first_rule = sorted(
            outcomes[next_disposition], key=lambda item: (item[0].template_id, item[1])
        )[0][0]
        reason_code = first_rule.reason_code
        source = "deterministic"
        conflict = False

    if (
        assessment.disposition == next_disposition
        and assessment.evidence_ids == evidence_ids
        and assessment.has_high_quality_conflict == conflict
    ):
        return assessment
    transition = HypothesisTransition(
        previous_disposition=assessment.disposition,
        next_disposition=next_disposition,
        evidence_ids=evidence_ids,
        reason_code=reason_code,
        assessment_source=source,
    )
    return replace(
        assessment,
        disposition=next_disposition,
        evidence_ids=evidence_ids,
        reason_code=reason_code,
        assessment_source=source,
        has_high_quality_conflict=conflict,
        transitions=assessment.transitions + (transition,),
    )
