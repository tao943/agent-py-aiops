"""Validated, auditable diagnostic decisions without private chain-of-thought."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class DiagnosticPlanStep:
    id: str
    tool: str
    arguments: dict[str, object]
    purpose: str
    tests_hypotheses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HypothesisState:
    id: str
    status: Literal["open", "supported", "refuted"]
    confidence: float
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservationDecision:
    purpose: str
    supports: tuple[str, ...]
    refutes: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class RootCauseDecision:
    component: str
    mechanism: str
    trigger: str
    causal_chain: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float


def parse_plan(
    text: str,
    *,
    available_tools: Set[str],
    known_hypotheses: Set[str],
) -> tuple[DiagnosticPlanStep, ...]:
    """Parse a bounded plan that can use only discovered tools and public hypotheses."""
    payload = _json_mapping(text)
    steps = _required_sequence(payload, "steps")
    if not steps:
        raise ValueError("Diagnostic plan must contain at least one step.")
    if len(steps) > 6:
        raise ValueError("Diagnostic plan cannot contain more than six steps.")

    parsed: list[DiagnosticPlanStep] = []
    ids: set[str] = set()
    for index, raw_step in enumerate(steps):
        step = _as_mapping(raw_step, f"plan step {index + 1}")
        step_id = _required_str(step, "id")
        if step_id in ids:
            raise ValueError(f"Duplicate diagnostic plan step ID: {step_id}.")
        ids.add(step_id)
        tool = _required_str(step, "tool")
        if tool not in available_tools:
            raise ValueError(f"Diagnostic plan references unknown tool: {tool}.")
        tested = _string_tuple(step, "testsHypotheses")
        unknown = set(tested) - set(known_hypotheses)
        if unknown:
            raise ValueError(
                "Diagnostic plan references unknown hypothesis: "
                + ", ".join(sorted(unknown))
                + "."
            )
        arguments = dict(_required_mapping(step, "arguments"))
        parsed.append(
            DiagnosticPlanStep(
                id=step_id,
                tool=tool,
                arguments=arguments,
                purpose=_required_str(step, "purpose"),
                tests_hypotheses=tested,
            )
        )
    return tuple(parsed)


def parse_observation_decision(
    text: str,
    *,
    known_hypotheses: Set[str],
) -> ObservationDecision:
    """Validate how one persisted observation changes public hypotheses."""
    payload = _json_mapping(text)
    supports = _string_tuple(payload, "supports")
    refutes = _string_tuple(payload, "refutes")
    unknown = (set(supports) | set(refutes)) - set(known_hypotheses)
    if unknown:
        raise ValueError(
            "Observation references unknown hypothesis: "
            + ", ".join(sorted(unknown))
            + "."
        )
    overlap = set(supports) & set(refutes)
    if overlap:
        raise ValueError(
            "Observation cannot both support and refute hypothesis: "
            + ", ".join(sorted(overlap))
            + "."
        )
    return ObservationDecision(
        purpose=_required_str(payload, "purpose"),
        supports=supports,
        refutes=refutes,
        summary=_required_str(payload, "summary"),
    )


def parse_root_cause_decision(
    text: str,
    *,
    available_evidence_ids: Set[str],
) -> RootCauseDecision:
    """Validate a final root-cause decision against persisted evidence IDs."""
    payload = _json_mapping(text)
    evidence_ids = _string_tuple(payload, "evidenceIds")
    if not evidence_ids:
        raise ValueError("Root-cause decision must reference evidence.")
    unknown = set(evidence_ids) - set(available_evidence_ids)
    if unknown:
        raise ValueError(
            "Root-cause decision references unknown evidence: "
            + ", ".join(sorted(unknown))
            + "."
        )
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("Root-cause confidence must be a number between zero and one.")
    normalized_confidence = float(confidence)
    if not 0 <= normalized_confidence <= 1:
        raise ValueError("Root-cause confidence must be a number between zero and one.")
    return RootCauseDecision(
        component=_required_str(payload, "component"),
        mechanism=_required_str(payload, "mechanism"),
        trigger=_required_str(payload, "trigger"),
        causal_chain=_string_tuple(payload, "causalChain"),
        evidence_ids=evidence_ids,
        confidence=normalized_confidence,
    )


def _json_mapping(text: str) -> Mapping[str, object]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match is None:
        raise ValueError("Model response does not contain a JSON object.")
    try:
        parsed: object = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("Model response contains invalid JSON.") from exc
    return _as_mapping(parsed, "model response")


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    return cast(Mapping[str, object], mapping)


def _required_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    if key not in payload:
        raise ValueError(f"Model field '{key}' is required.")
    return _as_mapping(payload[key], key)


def _required_sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = payload.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"Model field '{key}' must be a sequence.")
    return cast(Sequence[object], value)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Model field '{key}' must be a non-empty string.")
    return value.strip()


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = _required_sequence(payload, key)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"Model field '{key}' must contain non-empty strings.")
    return tuple(cast(str, value).strip() for value in values)
