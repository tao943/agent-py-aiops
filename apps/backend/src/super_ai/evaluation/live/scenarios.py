"""Strict loaders that preserve the Docker Live answer boundary."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import yaml

from super_ai.evaluation.domain import (
    PublicHypothesis,
    ScenarioOracle,
)
from super_ai.evaluation.live.domain import LiveRunIdentity, LiveScenario
from super_ai.evaluation.scenarios import load_scenario_oracle

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "ground_truth",
        "oracle",
        "primary_cause",
        "required_evidence",
        "required_rule_outs",
        "recovery_expectation",
        "root_cause_semantics",
        "answer",
    }
)


def validate_run_id(value: str) -> LiveRunIdentity:
    """Validate a run ID and derive a safe opaque SQL identifier token."""
    if not _RUN_ID.fullmatch(value):
        raise ValueError("Live run ID must contain only letters, digits and hyphens.")
    token = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return LiveRunIdentity(run_id=value, run_token=token)


def resolve_live_scenario_directory(root: Path, scenario_id: str) -> Path:
    """Resolve exactly one scenario directory without path traversal."""
    if not _SCENARIO_ID.fullmatch(scenario_id):
        raise ValueError("Scenario ID must be a single directory name.")
    scenario_dir = (root.resolve() / scenario_id).resolve()
    if scenario_dir.parent != root.resolve():
        raise ValueError("Scenario ID must be a single directory name.")
    return scenario_dir


def load_live_scenario(path: Path) -> LiveScenario:
    """Load public Live data while rejecting nested answer fields."""
    payload = _load_mapping(path / "scenario.yaml")
    leaked = _find_forbidden_keys(payload)
    if leaked:
        raise ValueError(
            "Public Live scenario contains ground-truth keys: "
            + ", ".join(sorted(leaked))
        )
    hypotheses = tuple(
        PublicHypothesis(
            id=_required_str(item_mapping, "id"),
            description=_required_str(item_mapping, "description"),
        )
        for item in _required_sequence(payload, "hypotheses")
        for item_mapping in (_as_mapping(item, "hypothesis"),)
    )
    ids = [item.id for item in hypotheses]
    if len(ids) != len(set(ids)):
        raise ValueError("Public Live scenario hypothesis IDs must be unique.")
    modes = _string_tuple(payload, "modes")
    if modes != ("live",):
        raise ValueError("Docker Live scenario modes must contain only live.")
    return LiveScenario(
        id=_required_str(payload, "id"),
        title=_required_str(payload, "title"),
        symptom_family=_required_str(payload, "symptom_family"),
        difficulty=_required_str(payload, "difficulty"),
        modes=modes,
        driver=_required_str(payload, "driver"),
        alert=dict(_required_mapping(payload, "alert")),
        hypotheses=hypotheses,
    )


def load_live_oracle(path: Path) -> ScenarioOracle:
    """Load the private Live oracle and its required semantic scoring rubric."""
    oracle = load_scenario_oracle(path)
    if oracle.root_cause_semantics is None:
        raise ValueError("Live root_cause_semantics rubric is required.")
    payload = _load_mapping(path / "ground_truth.yaml")
    recovery_expectation = _required_str(payload, "recovery_expectation")
    if recovery_expectation not in {"executed_recovery", "proposal_only"}:
        raise ValueError(
            "Live scenario field 'recovery_expectation' must be executed_recovery "
            "or proposal_only."
        )
    return replace(
        oracle,
        recovery_expectation=cast(
            Literal["executed_recovery", "proposal_only"],
            recovery_expectation,
        ),
    )


def _load_mapping(path: Path) -> Mapping[str, object]:
    try:
        parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Live scenario file does not exist: {path}.") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Live scenario YAML is invalid: {path}.") from exc
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
        raise ValueError(f"Live scenario field '{key}' is required.")
    return _as_mapping(payload[key], key)


def _required_sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = payload.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"Live scenario field '{key}' must be a sequence.")
    return cast(Sequence[object], value)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Live scenario field '{key}' must be a non-empty string.")
    return value.strip()


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = _required_sequence(payload, key)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"Live scenario field '{key}' must contain non-empty strings.")
    return tuple(cast(str, value).strip() for value in values)


def _find_forbidden_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in cast(Mapping[object, object], value).items():
            if isinstance(key, str) and _normalize_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                found.add(key)
            found.update(_find_forbidden_keys(nested))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in cast(Sequence[object], value):
            found.update(_find_forbidden_keys(item))
    return found


def _normalize_key(key: str) -> str:
    return "_".join(key.strip().lower().replace("-", " ").split())
