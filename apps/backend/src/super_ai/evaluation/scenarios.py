"""Strict YAML loaders that preserve the benchmark answer boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import yaml

from super_ai.evaluation.domain import (
    EvidenceMilestone,
    PublicHypothesis,
    PublicScenario,
    RootCause,
    ScenarioBundle,
    ScenarioOracle,
)

_PUBLIC_FORBIDDEN_KEYS = frozenset(
    {"ground_truth", "oracle", "primary_cause", "required_evidence", "answer"}
)


def load_public_scenario(path: Path) -> PublicScenario:
    """Load only public scenario data and reject embedded answer fields."""
    payload = _load_yaml_mapping(path / "scenario.yaml")
    leaked_keys = _find_forbidden_keys(payload)
    if leaked_keys:
        names = ", ".join(sorted(leaked_keys))
        raise ValueError(f"Public scenario contains ground-truth keys: {names}.")

    hypotheses = tuple(
        PublicHypothesis(
            id=_required_str(_as_mapping(item, "hypothesis"), "id"),
            description=_required_str(_as_mapping(item, "hypothesis"), "description"),
        )
        for item in _required_sequence(payload, "hypotheses")
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
        required_rule_outs=_string_tuple(payload, "required_rule_outs"),
        forbidden_claims=_string_tuple(payload, "forbidden_claims"),
    )


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
            if isinstance(key, str) and key.lower() in _PUBLIC_FORBIDDEN_KEYS:
                found.add(key)
            found.update(_find_forbidden_keys(nested))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in cast(Sequence[object], value):
            found.update(_find_forbidden_keys(item))
    return found
