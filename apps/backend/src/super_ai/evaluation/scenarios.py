"""Strict YAML loaders that preserve the benchmark answer boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import yaml

from super_ai.evaluation.domain import (
    EvidenceMilestone,
    PublicDecisionLabel,
    PublicHypothesis,
    PublicScenario,
    RootCause,
    RootCauseSemantics,
    ScenarioBundle,
    ScenarioOracle,
    SemanticConcept,
    SemanticRequirement,
)

_PUBLIC_FORBIDDEN_KEYS = frozenset(
    {
        "ground_truth",
        "oracle",
        "primary_cause",
        "required_evidence",
        "required_rule_outs",
        "root_cause_semantics",
        "answer",
    }
)


def load_public_scenario(path: Path) -> PublicScenario:
    """Load only public scenario data and reject embedded answer fields."""
    payload = _load_yaml_mapping(path / "scenario.yaml")
    leaked_keys = _find_forbidden_keys(payload)
    if leaked_keys:
        names = ", ".join(sorted(leaked_keys))
        raise ValueError(f"Public scenario contains ground-truth keys: {names}.")

    hypotheses = tuple(
        _public_hypothesis(item) for item in _required_sequence(payload, "hypotheses")
    )
    hypothesis_ids = [item.id for item in hypotheses]
    if len(set(hypothesis_ids)) != len(hypothesis_ids):
        raise ValueError("Public scenario hypothesis IDs must be unique.")

    alert = dict(_required_mapping(payload, "alert"))
    return PublicScenario(
        id=_required_str(payload, "id"),
        title=_required_str(payload, "title"),
        symptom_family=_required_str(payload, "symptom_family"),
        difficulty=_required_str(payload, "difficulty"),
        modes=_string_tuple(payload, "modes"),
        alert=alert,
        hypotheses=hypotheses,
        snapshot_file=_required_str(payload, "snapshot_file"),
    )


def load_scenario_oracle(path: Path) -> ScenarioOracle:
    """Load the evaluator-only oracle without reading public scenario data."""
    payload = _load_yaml_mapping(path / "ground_truth.yaml")
    raw_semantics = payload.get("root_cause_semantics")
    semantics = (
        None
        if raw_semantics is None
        else load_root_cause_semantics(
            _as_mapping(raw_semantics, "root_cause_semantics")
        )
    )
    return ScenarioOracle(
        primary_cause=_root_cause(_required_mapping(payload, "primary_cause")),
        contributing_causes=tuple(
            _root_cause(_as_mapping(item, "contributing cause"))
            for item in _optional_sequence(payload, "contributing_causes")
        ),
        causal_chain=_string_tuple(payload, "causal_chain"),
        required_evidence=tuple(
            _evidence_milestone(_as_mapping(item, "evidence milestone"))
            for item in _required_sequence(payload, "required_evidence")
        ),
        cls_required_evidence=tuple(
            _evidence_milestone(_as_mapping(item, "CLS evidence milestone"))
            for item in _optional_sequence(payload, "cls_required_evidence")
        ),
        required_rule_outs=_string_tuple(payload, "required_rule_outs"),
        forbidden_claims=_string_tuple(payload, "forbidden_claims"),
        root_cause_semantics=semantics,
    )


def load_root_cause_semantics(
    payload: Mapping[str, object],
) -> RootCauseSemantics:
    """Load one strict evaluator-only natural-language scoring rubric."""
    raw_concepts = _required_mapping(payload, "concepts")
    if not raw_concepts:
        raise ValueError("root_cause_semantics concepts must not be empty.")
    concepts: list[SemanticConcept] = []
    for concept_id, raw_aliases in raw_concepts.items():
        aliases = _non_empty_string_sequence(
            raw_aliases, f"root_cause_semantics concept '{concept_id}' aliases"
        )
        normalized_aliases = [alias.casefold() for alias in aliases]
        if len(normalized_aliases) != len(set(normalized_aliases)):
            raise ValueError(
                f"root_cause_semantics concept '{concept_id}' aliases must be unique."
            )
        concepts.append(SemanticConcept(concept_id, aliases))

    known_concepts = {concept.id for concept in concepts}
    trigger = _semantic_requirement(
        _required_mapping(payload, "trigger"), "trigger", default_id="trigger"
    )
    raw_milestones = _required_sequence(payload, "causal_milestones")
    if len(raw_milestones) != 3:
        raise ValueError(
            "root_cause_semantics causal_milestones must contain exactly three items."
        )
    milestones = tuple(
        _semantic_requirement(
            _as_mapping(item, "causal milestone"), "causal_milestones"
        )
        for item in raw_milestones
    )
    milestone_ids = [item.id for item in milestones]
    if len(milestone_ids) != len(set(milestone_ids)):
        raise ValueError("root_cause_semantics causal milestone IDs must be unique.")
    for requirement in (trigger, *milestones):
        unknown = set(requirement.all_of) - known_concepts
        if unknown:
            raise ValueError(
                f"root_cause_semantics requirement '{requirement.id}' references "
                "an unknown concept."
            )
    return RootCauseSemantics(tuple(concepts), trigger, milestones)


def _semantic_requirement(
    payload: Mapping[str, object],
    label: str,
    *,
    default_id: str | None = None,
) -> SemanticRequirement:
    identifier = default_id or _required_str(payload, "id")
    all_of = _non_empty_string_sequence(
        payload.get("all_of"), f"root_cause_semantics {label} all_of"
    )
    if len(all_of) != len(set(all_of)):
        raise ValueError(f"root_cause_semantics {label} all_of must be unique.")
    return SemanticRequirement(identifier, all_of)


def _non_empty_string_sequence(
    value: object,
    label: str,
) -> tuple[str, ...]:
    values = _as_sequence(value, label)
    if not values or not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError(f"{label} must contain non-empty strings.")
    return tuple(cast(str, item).strip() for item in values)


def validate_scenario_bundle(bundle: ScenarioBundle) -> None:
    """Validate cross-file invariants without exposing oracle data to the Agent."""
    root = bundle.root.resolve()
    snapshot_path = (root / bundle.public.snapshot_file).resolve()
    if not snapshot_path.is_relative_to(root):
        raise ValueError("Scenario snapshot path must remain inside its scenario directory.")
    if not snapshot_path.is_file():
        raise ValueError(f"Scenario snapshot does not exist: {snapshot_path}.")
    if bundle.public.id != root.name:
        raise ValueError("Public scenario ID must match its scenario directory name.")
    if not bundle.oracle.required_evidence:
        raise ValueError("Scenario oracle must require at least one evidence milestone.")


def _load_yaml_mapping(path: Path) -> Mapping[str, object]:
    try:
        parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Scenario file does not exist: {path}.") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Scenario YAML is invalid: {path}.") from exc
    return _as_mapping(parsed, path.name)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    return cast(Mapping[str, object], mapping)


def _required_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    if key not in payload:
        raise ValueError(f"Scenario field '{key}' is required.")
    return _as_mapping(payload[key], key)


def _required_sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    if key not in payload:
        raise ValueError(f"Scenario field '{key}' is required.")
    return _as_sequence(payload[key], key)


def _optional_sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    return _as_sequence(payload.get(key, ()), key)


def _as_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence.")
    return cast(Sequence[object], value)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Scenario field '{key}' must be a non-empty string.")
    return value.strip()


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = _required_sequence(payload, key)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"Scenario field '{key}' must contain non-empty strings.")
    return tuple(cast(str, value).strip() for value in values)


def _root_cause(payload: Mapping[str, object]) -> RootCause:
    return RootCause(
        component=_required_str(payload, "component"),
        mechanism=_required_str(payload, "mechanism"),
        trigger=_required_str(payload, "trigger"),
    )


def _public_hypothesis(value: object) -> PublicHypothesis:
    payload = _as_mapping(value, "hypothesis")
    decision_label = _required_mapping(payload, "decision_label")
    return PublicHypothesis(
        id=_required_str(payload, "id"),
        description=_required_str(payload, "description"),
        decision_label=PublicDecisionLabel(
            component=_required_str(decision_label, "component"),
            mechanism=_required_str(decision_label, "mechanism"),
        ),
    )


def _evidence_milestone(payload: Mapping[str, object]) -> EvidenceMilestone:
    alternatives = tuple(
        _string_sequence_tuple(item, "evidence alternative")
        for item in _required_sequence(payload, "alternatives")
    )
    if not alternatives:
        raise ValueError("Evidence milestone must contain at least one alternative.")
    return EvidenceMilestone(id=_required_str(payload, "id"), alternatives=alternatives)


def _string_sequence_tuple(value: object, label: str) -> tuple[str, ...]:
    values = _as_sequence(value, label)
    if not values or not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError(f"{label} must contain non-empty strings.")
    return tuple(cast(str, item).strip() for item in values)


def _find_forbidden_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in cast(Mapping[object, object], value).items():
            if isinstance(key, str) and _normalize_public_key(key) in _PUBLIC_FORBIDDEN_KEYS:
                found.add(key)
            found.update(_find_forbidden_keys(nested))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in cast(Sequence[object], value):
            found.update(_find_forbidden_keys(item))
    return found


def _normalize_public_key(key: str) -> str:
    return "_".join(key.strip().lower().replace("-", " ").split())
