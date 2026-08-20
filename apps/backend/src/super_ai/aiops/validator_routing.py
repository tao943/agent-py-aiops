"""Deterministic routing for optional semantic root-cause validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RiskTier = Literal["L0", "L1", "L2", "L3"]
ValidationReasonCode = Literal[
    "llm_adjudicated",
    "execution_requested",
    "elevated_recovery_risk",
    "compound_root_cause",
    "cross_component_causality",
    "high_quality_evidence_conflict",
]
ValidationSkipReason = Literal[
    "no_semantic_risk",
    "deterministic_validation_failed",
]


@dataclass(frozen=True, slots=True)
class ValidatorRiskContext:
    deterministic_valid: bool
    used_llm_adjudication: bool = False
    execution_requested: bool = False
    max_risk_tier: RiskTier = "L0"
    compound_root_cause: bool = False
    causal_components: tuple[str, ...] = ()
    has_high_quality_conflict: bool = False


@dataclass(frozen=True, slots=True)
class ValidatorRoutingDecision:
    required: bool
    reason_codes: tuple[ValidationReasonCode, ...]
    skip_reason: ValidationSkipReason | None


def requires_llm_validation(
    context: ValidatorRiskContext,
) -> ValidatorRoutingDecision:
    """Return an allowlisted risk decision; model text cannot affect this route."""
    if not context.deterministic_valid:
        return ValidatorRoutingDecision(
            required=False,
            reason_codes=(),
            skip_reason="deterministic_validation_failed",
        )
    reasons: list[ValidationReasonCode] = []
    if context.used_llm_adjudication:
        reasons.append("llm_adjudicated")
    if context.execution_requested:
        reasons.append("execution_requested")
    if context.max_risk_tier in {"L2", "L3"}:
        reasons.append("elevated_recovery_risk")
    if context.compound_root_cause:
        reasons.append("compound_root_cause")
    if len({item for item in context.causal_components if item.strip()}) > 1:
        reasons.append("cross_component_causality")
    if context.has_high_quality_conflict:
        reasons.append("high_quality_evidence_conflict")
    return ValidatorRoutingDecision(
        required=bool(reasons),
        reason_codes=tuple(reasons),
        skip_reason=None if reasons else "no_semantic_risk",
    )
