"""Deterministic evaluator-only matching for root-cause prose."""

from __future__ import annotations

import re
from dataclasses import dataclass

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation.domain import (
    RootCauseSemantics,
    ScenarioOracle,
    SemanticRequirement,
)

_LABEL_SEPARATOR = re.compile(r"[\s-]+")
_REPEATED_UNDERSCORE = re.compile(r"_+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class RootCauseSemanticScore:
    """Auditable 4+6+4+6 root-cause score components."""

    component: int
    mechanism: int
    trigger: int
    milestones: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        return (
            self.component
            + self.mechanism
            + self.trigger
            + sum(points for _, points in self.milestones)
        )


def score_root_cause_semantics(
    decision: RootCauseDecision | None,
    oracle: ScenarioOracle,
) -> RootCauseSemanticScore:
    """Score canonical identity and ordered private semantic requirements."""
    semantics = oracle.root_cause_semantics
    if semantics is None:
        raise ValueError("Root-cause semantic rubric is required.")
    milestone_zeros = tuple(
        (milestone.id, 0) for milestone in semantics.causal_milestones
    )
    if decision is None:
        return RootCauseSemanticScore(0, 0, 0, milestone_zeros)

    component = (
        4
        if _normalize_label(decision.component)
        == _normalize_label(oracle.primary_cause.component)
        else 0
    )
    mechanism = (
        6
        if _normalize_label(decision.mechanism)
        == _normalize_label(oracle.primary_cause.mechanism)
        else 0
    )
    if component != 4 or mechanism != 6:
        return RootCauseSemanticScore(component, mechanism, 0, milestone_zeros)

    trigger = (
        4 if _requirement_matches(decision.trigger, semantics.trigger, semantics) else 0
    )
    milestones = _ordered_milestone_scores(
        decision.causal_chain,
        semantics.causal_milestones,
        semantics,
    )
    return RootCauseSemanticScore(component, mechanism, trigger, milestones)


def _ordered_milestone_scores(
    steps: tuple[str, ...],
    requirements: tuple[SemanticRequirement, ...],
    semantics: RootCauseSemantics,
) -> tuple[tuple[str, int], ...]:
    next_index = 0
    scores: list[tuple[str, int]] = []
    for requirement in requirements:
        match = next(
            (
                index
                for index in range(next_index, len(steps))
                if _requirement_matches(steps[index], requirement, semantics)
            ),
            None,
        )
        scores.append((requirement.id, 2 if match is not None else 0))
        if match is not None:
            next_index = match + 1
    return tuple(scores)


def _requirement_matches(
    text: str,
    requirement: SemanticRequirement,
    semantics: RootCauseSemantics,
) -> bool:
    normalized_text = _normalize_text(text)
    aliases_by_id = {concept.id: concept.aliases for concept in semantics.concepts}
    return all(
        any(_contains_alias(normalized_text, alias) for alias in aliases_by_id[concept_id])
        for concept_id in requirement.all_of
    )


def _normalize_label(value: str) -> str:
    normalized = _LABEL_SEPARATOR.sub("_", value.strip().casefold())
    return _REPEATED_UNDERSCORE.sub("_", normalized).strip("_")


def _normalize_text(value: str) -> str:
    normalized = _NON_WORD.sub(" ", value.casefold().replace("_", " "))
    return _WHITESPACE.sub(" ", normalized).strip()


def _contains_alias(normalized_text: str, alias: str) -> bool:
    normalized_alias = _normalize_text(alias)
    return bool(normalized_alias) and f" {normalized_alias} " in f" {normalized_text} "
