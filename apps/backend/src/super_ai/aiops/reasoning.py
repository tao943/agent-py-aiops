"""Validated, auditable diagnostic decisions without private chain-of-thought."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from typing import Literal, cast

from super_ai.aiops.adjudication import HypothesisAssessment
from super_ai.aiops.causal_intents import (
    CausalIntent,
    CausalIntentOrigin,
    allowed_causal_intents,
)


@dataclass(frozen=True, slots=True)
class DiagnosticPlanStep:
    id: str
    tool: str
    arguments: dict[str, object]
    purpose: str
    tests_hypotheses: tuple[str, ...]
    causal_intent: CausalIntent
    causal_intent_origin: CausalIntentOrigin = "model"


@dataclass(frozen=True, slots=True)
class HypothesisState:
    id: str
    status: Literal["open", "supported", "refuted"]
    confidence: float
    evidence_ids: tuple[str, ...]


def project_hypothesis_assessment(assessment: HypothesisAssessment) -> HypothesisState:
    """Project a v4 assessment for legacy v2/v3 readers only."""
    if assessment.disposition == "supported":
        status: Literal["open", "supported", "refuted"] = "supported"
        confidence = 0.95
    elif assessment.disposition in {"refuted", "causally_inactive"}:
        status = "refuted"
        confidence = 0.1 if assessment.disposition == "causally_inactive" else 0.05
    else:
        status = "open"
        confidence = 0.5
    return HypothesisState(
        id=assessment.hypothesis_id,
        status=status,
        confidence=confidence,
        evidence_ids=assessment.evidence_ids,
    )


CausalRole = CausalIntent


@dataclass(frozen=True, slots=True)
class ObservationDecision:
    purpose: str
    supports: tuple[str, ...]
    refutes: tuple[str, ...]
    summary: str
    evidence_ids: tuple[str, ...] = ()
    causal_role: CausalRole = "context"
    causal_role_origin: Literal["model", "plan_contract"] | None = None
    reported_causal_role: CausalRole | None = None
    causal_role_corrected: bool = False


@dataclass(frozen=True, slots=True)
class RootCauseDecision:
    component: str
    mechanism: str
    trigger: str
    causal_chain: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class EvidenceSufficiencyDecision:
    status: Literal["sufficient", "insufficient"]
    evidence_ids: tuple[str, ...]
    supported_hypotheses: tuple[str, ...]
    refuted_hypotheses: tuple[str, ...]
    unresolved_hypotheses: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    recommended_tools: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class RootCauseValidationDecision:
    status: Literal["valid", "invalid"]
    evidence_ids: tuple[str, ...]
    unsupported_fields: tuple[
        Literal["component", "mechanism", "trigger", "causalChain"], ...
    ]
    missing_evidence: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    mode: Literal[
        "no_action", "proposal_only", "external_policy_required", "manual_review"
    ]
    action: str
    target: str
    rationale: str
    tool: str | None
    arguments: dict[str, object]
    risk: str
    rollback: str
    verification_steps: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    decision_confidence: float
    human_approval_required: bool


@dataclass(frozen=True, slots=True)
class RecoveryPolicyDecision:
    status: Literal["allowed", "denied", "deferred"]
    authorization_code: str
    execution_permitted: bool
    proposal_recorded: bool
    human_approval_required: bool
    summary: str


_ROOT_CAUSE_FIELDS = {"component", "mechanism", "trigger", "causalChain"}
_RECOVERY_MODES = {
    "no_action",
    "proposal_only",
    "external_policy_required",
    "manual_review",
}
_MAX_AUDIT_ITEMS = 6


def normalize_root_cause_decision(
    decision: RootCauseDecision,
    *,
    component_aliases: Mapping[str, str],
    mechanism_aliases: Mapping[str, str],
) -> RootCauseDecision:
    """Apply only explicitly declared public output-label aliases."""
    return replace(
        decision,
        component=component_aliases.get(decision.component, decision.component),
        mechanism=mechanism_aliases.get(decision.mechanism, decision.mechanism),
    )


def parse_plan(
    text: str,
    *,
    available_tools: Set[str],
    known_hypotheses: Set[str],
    causal_capabilities: Mapping[str, Collection[CausalIntent]] | None = None,
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
        causal_intent_value = _required_str(step, "causalIntent")
        if causal_intent_value not in {"trigger", "mechanism", "impact", "context"}:
            raise ValueError(
                "Plan causalIntent must be trigger, mechanism, impact, or context."
            )
        causal_intent = cast(CausalIntent, causal_intent_value)
        capabilities = (
            frozenset(causal_capabilities.get(tool, ()))
            if causal_capabilities is not None
            else allowed_causal_intents(tool)
        )
        if causal_intent not in capabilities:
            raise ValueError(
                f"Plan causalIntent {causal_intent!r} is not allowed for tool {tool}."
            )
        parsed.append(
            DiagnosticPlanStep(
                id=step_id,
                tool=tool,
                arguments=arguments,
                purpose=_required_str(step, "purpose"),
                tests_hypotheses=tested,
                causal_intent=causal_intent,
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
    causal_role = payload.get("causalRole", "context")
    if causal_role not in {"trigger", "mechanism", "impact", "context"}:
        raise ValueError(
            "Observation causalRole must be trigger, mechanism, impact, or context."
        )
    return ObservationDecision(
        purpose=_required_str(payload, "purpose"),
        supports=supports,
        refutes=refutes,
        summary=_required_str(payload, "summary"),
        causal_role=cast(CausalRole, causal_role),
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
        causal_chain=_string_sequence_or_singleton(payload, "causalChain"),
        evidence_ids=evidence_ids,
        confidence=normalized_confidence,
    )


def parse_evidence_sufficiency(
    text: str,
    *,
    available_evidence_ids: Set[str],
    known_hypotheses: Set[str],
    available_tools: Set[str],
) -> EvidenceSufficiencyDecision:
    """Validate an evidence-sufficiency assessment against public runtime IDs."""
    payload = _json_mapping(text)
    status = _required_choice(payload, "status", {"sufficient", "insufficient"})
    evidence_ids = _known_strings(
        payload,
        "evidenceIds",
        allowed=available_evidence_ids,
        label="Sufficiency evidence",
    )
    supported = _known_strings(
        payload,
        "supportedHypotheses",
        allowed=known_hypotheses,
        label="Supported hypotheses",
    )
    refuted = _known_strings(
        payload,
        "refutedHypotheses",
        allowed=known_hypotheses,
        label="Refuted hypotheses",
    )
    unresolved = _known_strings(
        payload,
        "unresolvedHypotheses",
        allowed=known_hypotheses,
        label="Unresolved hypotheses",
    )
    if (set(supported) & set(refuted)) or (
        set(unresolved) & (set(supported) | set(refuted))
    ):
        raise ValueError("Sufficiency hypothesis classifications must be disjoint.")
    missing_evidence = _bounded_strings(payload, "missingEvidence")
    recommended_tools = _known_strings(
        payload,
        "recommendedTools",
        allowed=available_tools,
        label="Recommended tools",
    )
    if status == "sufficient" and (unresolved or missing_evidence):
        raise ValueError("Sufficient evidence cannot declare unresolved gaps.")
    return EvidenceSufficiencyDecision(
        status=cast(Literal["sufficient", "insufficient"], status),
        evidence_ids=evidence_ids,
        supported_hypotheses=supported,
        refuted_hypotheses=refuted,
        unresolved_hypotheses=unresolved,
        missing_evidence=missing_evidence,
        recommended_tools=recommended_tools,
        summary=_required_str(payload, "summary"),
    )


def parse_root_cause_validation(
    text: str,
    *,
    available_evidence_ids: Set[str],
) -> RootCauseValidationDecision:
    """Validate a public audit of a candidate root-cause decision."""
    payload = _json_mapping(text)
    status = _required_choice(payload, "status", {"valid", "invalid"})
    evidence_ids = _known_strings(
        payload,
        "evidenceIds",
        allowed=available_evidence_ids,
        label="Validation evidence",
    )
    raw_fields = _bounded_strings(payload, "unsupportedFields")
    unknown = set(raw_fields) - _ROOT_CAUSE_FIELDS
    if unknown:
        raise ValueError(
            "Root-cause validation references unsupported field: "
            + ", ".join(sorted(unknown))
            + "."
        )
    missing_evidence = _bounded_strings(payload, "missingEvidence")
    if status == "valid" and (raw_fields or missing_evidence):
        raise ValueError("A valid root-cause decision cannot declare unsupported fields.")
    return RootCauseValidationDecision(
        status=cast(Literal["valid", "invalid"], status),
        evidence_ids=evidence_ids,
        unsupported_fields=cast(
            tuple[Literal["component", "mechanism", "trigger", "causalChain"], ...],
            raw_fields,
        ),
        missing_evidence=missing_evidence,
        summary=_required_str(payload, "summary"),
    )


def parse_recovery_plan(
    text: str,
    *,
    available_evidence_ids: Set[str],
    proposal_tools: Set[str],
) -> RecoveryPlan:
    """Validate a non-authorizing recovery recommendation."""
    payload = _json_mapping(text)
    mode = _required_choice(payload, "mode", _RECOVERY_MODES)
    raw_tool = payload.get("tool")
    tool = raw_tool.strip() if isinstance(raw_tool, str) and raw_tool.strip() else None
    if mode == "proposal_only":
        if tool is None or tool not in proposal_tools:
            raise ValueError("Proposal-only recovery must use a known proposal tool.")
    elif tool is not None:
        raise ValueError("Only proposal-only recovery may select a tool.")
    arguments = dict(_required_mapping(payload, "arguments")) if tool is not None else {}
    evidence_ids = _known_strings(
        payload,
        "evidenceIds",
        allowed=available_evidence_ids,
        label="Recovery evidence",
    )
    verification_steps = _bounded_strings(payload, "verificationSteps")
    if mode in {"proposal_only", "external_policy_required"} and len(verification_steps) < 2:
        raise ValueError("Actionable recovery requires at least two verification steps.")
    return RecoveryPlan(
        mode=cast(
            Literal[
                "no_action", "proposal_only", "external_policy_required", "manual_review"
            ],
            mode,
        ),
        action=_required_str(payload, "action"),
        target=_required_str(payload, "target"),
        rationale=_required_str(payload, "rationale"),
        tool=tool,
        arguments=arguments,
        risk=_required_str(payload, "risk"),
        rollback=_required_str(payload, "rollback"),
        verification_steps=verification_steps,
        evidence_ids=evidence_ids,
        decision_confidence=_number_zero_one(payload, "decisionConfidence"),
        human_approval_required=_required_bool(payload, "humanApprovalRequired"),
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


def _string_sequence_or_singleton(
    payload: Mapping[str, object],
    key: str,
) -> tuple[str, ...]:
    value = payload.get(key)
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"Model field '{key}' must contain non-empty strings.")
        return (value.strip(),)
    return _string_tuple(payload, key)


def _bounded_strings(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = _string_tuple(payload, key)
    if len(values) > _MAX_AUDIT_ITEMS:
        raise ValueError(f"Model field '{key}' cannot contain more than {_MAX_AUDIT_ITEMS} items.")
    return values


def _known_strings(
    payload: Mapping[str, object],
    key: str,
    *,
    allowed: Set[str],
    label: str,
) -> tuple[str, ...]:
    values = _bounded_strings(payload, key)
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"{label} references unknown value: {', '.join(sorted(unknown))}.")
    return values


def _required_choice(
    payload: Mapping[str, object], key: str, choices: Set[str]
) -> str:
    value = _required_str(payload, key)
    if value not in choices:
        raise ValueError(f"Model field '{key}' contains an unsupported value.")
    return value


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Model field '{key}' must be a boolean.")
    return value


def _number_zero_one(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Model field '{key}' must be a number between zero and one.")
    normalized = float(value)
    if not 0 <= normalized <= 1:
        raise ValueError(f"Model field '{key}' must be a number between zero and one.")
    return normalized
