"""Immutable domain contracts for AgentPy SRE benchmark scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class PublicHypothesis:
    """A candidate explanation intentionally visible to the diagnostic Agent."""

    id: str
    description: str


@dataclass(frozen=True, slots=True)
class PublicScenario:
    """The answer-free portion of one benchmark scenario."""

    id: str
    title: str
    symptom_family: str
    difficulty: str
    modes: tuple[str, ...]
    alert: dict[str, object]
    hypotheses: tuple[PublicHypothesis, ...]
    snapshot_file: str


@dataclass(frozen=True, slots=True)
class RootCause:
    """A normalized causal identity used only by the evaluator."""

    component: str
    mechanism: str
    trigger: str


@dataclass(frozen=True, slots=True)
class EvidenceMilestone:
    """Alternative evidence-ID combinations that satisfy one required milestone."""

    id: str
    alternatives: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class SemanticConcept:
    """Evaluator-only aliases for one domain concept."""

    id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticRequirement:
    """Concepts that must all occur within one scored text field."""

    id: str
    all_of: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootCauseSemantics:
    """Private deterministic rubric for natural-language root-cause fields."""

    concepts: tuple[SemanticConcept, ...]
    trigger: SemanticRequirement
    causal_milestones: tuple[SemanticRequirement, ...]


@dataclass(frozen=True, slots=True)
class ScenarioOracle:
    """Evaluator-only answer key for one scenario."""

    primary_cause: RootCause
    contributing_causes: tuple[RootCause, ...]
    causal_chain: tuple[str, ...]
    required_evidence: tuple[EvidenceMilestone, ...]
    required_rule_outs: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
    root_cause_semantics: RootCauseSemantics | None = None
    cls_required_evidence: tuple[EvidenceMilestone, ...] = ()
    recovery_expectation: Literal["executed_recovery", "proposal_only"] | None = None


@dataclass(frozen=True, slots=True)
class ScenarioBundle:
    """A pairing of public inputs and evaluator-only oracle data."""

    public: PublicScenario
    oracle: ScenarioOracle
    root: Path
