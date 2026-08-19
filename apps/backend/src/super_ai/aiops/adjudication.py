"""Deterministic, evidence-audited hypothesis adjudication."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
class _TrustedRuleTemplate:
    step_tool: str
    hypothesis_id: str
    predicate: EvidencePredicate
    when_true: Disposition
    reason_code: str
    causal_role: Literal["trigger", "mechanism", "impact", "context"]
    parameters: tuple[tuple[str, str], ...]


_TRUSTED_RULE_TEMPLATES: dict[str, _TrustedRuleTemplate] = {
    "nginx_upstream_port_matches_container_port": _TrustedRuleTemplate(
        step_tool="InspectNginx",
        hypothesis_id="upstream_port_mismatch",
        predicate=EvidencePredicate(
            left_fact="InspectNginx.upstreamPort",
            operator="in",
            right_fact="InspectContainer.configuredPorts",
        ),
        when_true="refuted",
        reason_code="configured_route_port_matches_service",
        causal_role="mechanism",
        parameters=(
            ("containerFact", "InspectContainer.configuredPorts"),
            ("nginxFact", "InspectNginx.upstreamPort"),
        ),
    ),
    "container_process_exited": _TrustedRuleTemplate(
        step_tool="InspectContainer",
        hypothesis_id="upstream_process_down",
        predicate=EvidencePredicate(
            left_fact="InspectContainer.status",
            operator="in",
            expected=("dead", "exited", "stopped"),
        ),
        when_true="supported",
        reason_code="upstream_process_not_running",
        causal_role="trigger",
        parameters=(("statusFact", "InspectContainer.status"),),
    ),
    "container_process_running": _TrustedRuleTemplate(
        step_tool="InspectContainer",
        hypothesis_id="upstream_process_down",
        predicate=EvidencePredicate(
            left_fact="InspectContainer.status",
            operator="eq",
            expected="running",
        ),
        when_true="refuted",
        reason_code="upstream_process_running",
        causal_role="context",
        parameters=(("statusFact", "InspectContainer.status"),),
    ),
    "nginx_resolved_address_present": _TrustedRuleTemplate(
        step_tool="InspectNginx",
        hypothesis_id="dns_resolution_failure",
        predicate=EvidencePredicate(
            left_fact="InspectNginx.resolvedAddresses",
            operator="truthy",
        ),
        when_true="refuted",
        reason_code="upstream_address_resolved",
        causal_role="context",
        parameters=(("addressesFact", "InspectNginx.resolvedAddresses"),),
    ),
    "redis_process_stopped": _TrustedRuleTemplate(
        step_tool="InspectRedis",
        hypothesis_id="redis_server_availability",
        predicate=EvidencePredicate(
            left_fact="InspectRedis.processStatus",
            operator="in",
            expected=("dead", "exited", "stopped"),
        ),
        when_true="supported",
        reason_code="redis_server_process_stopped",
        causal_role="trigger",
        parameters=(("statusFact", "InspectRedis.processStatus"),),
    ),
    "redis_no_stale_connections": _TrustedRuleTemplate(
        step_tool="InspectRedisClientPool",
        hypothesis_id="redis_client_connection_lifecycle",
        predicate=EvidencePredicate(
            left_fact="InspectRedisClientPool.staleConnections",
            operator="eq",
            expected=0,
        ),
        when_true="refuted",
        reason_code="redis_client_pool_has_no_stale_connections",
        causal_role="mechanism",
        parameters=(
            ("staleConnectionsFact", "InspectRedisClientPool.staleConnections"),
        ),
    ),
    "redis_connection_refused_reaches_endpoint": _TrustedRuleTemplate(
        step_tool="InspectRedisClientPool",
        hypothesis_id="redis_network_path",
        predicate=EvidencePredicate(
            left_fact="InspectRedisClientPool.lastError",
            operator="contains",
            expected="connection refused",
        ),
        when_true="refuted",
        reason_code="redis_endpoint_refused_reachable_connection",
        causal_role="mechanism",
        parameters=(("lastErrorFact", "InspectRedisClientPool.lastError"),),
    ),
}


def instantiate_trusted_evidence_rule(
    *,
    template_id: str,
    hypothesis_id: str,
    parameters: Mapping[str, object],
    step_tool: str,
) -> HypothesisEvidenceRule | None:
    """Instantiate only an exact code-owned fact-to-disposition contract."""
    template = _TRUSTED_RULE_TEMPLATES.get(template_id)
    if template is None:
        return None
    expected_parameters = dict(template.parameters)
    actual_parameters = {
        key: value for key, value in parameters.items() if isinstance(value, str)
    }
    if (
        template.step_tool != step_tool
        or template.hypothesis_id != hypothesis_id
        or actual_parameters != expected_parameters
        or len(actual_parameters) != len(parameters)
    ):
        return None
    return HypothesisEvidenceRule(
        template_id=template_id,
        hypothesis_id=hypothesis_id,
        predicate=template.predicate,
        when_true=template.when_true,
        reason_code=template.reason_code,
    )


def trusted_evidence_rule_catalog() -> tuple[dict[str, object], ...]:
    """Return the bounded public template choices exposed to Planner."""
    return tuple(
        {
            "templateId": template_id,
            "tool": template.step_tool,
            "hypothesisId": template.hypothesis_id,
            "parameters": dict(template.parameters),
        }
        for template_id, template in sorted(_TRUSTED_RULE_TEMPLATES.items())
    )


def trusted_reason_causal_role(
    reason_code: str,
) -> Literal["trigger", "mechanism", "impact", "context"] | None:
    """Resolve only code-owned rule semantics into an auditable causal role."""
    roles = {
        template.causal_role
        for template in _TRUSTED_RULE_TEMPLATES.values()
        if template.reason_code == reason_code
    }
    return (
        cast(
            Literal["trigger", "mechanism", "impact", "context"],
            next(iter(roles)),
        )
        if len(roles) == 1
        else None
    )


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
        evidence_ids = _matching_evidence_ids(rule.predicate, public_facts)
        if not evidence_ids:
            continue
        dispositions = proposed.setdefault(rule.hypothesis_id, {})
        outcomes = dispositions.setdefault(rule.when_true, [])
        outcomes.extend((rule, evidence_id) for evidence_id in evidence_ids)

    reduced: list[HypothesisAssessment] = []
    for hypothesis_id in sorted(current):
        assessment = current[hypothesis_id]
        outcomes = proposed.get(hypothesis_id, {})
        reduced.append(_apply_outcomes(assessment, outcomes))
    return tuple(reduced)


def apply_deterministic_transition(
    assessment: HypothesisAssessment,
    *,
    disposition: Disposition,
    evidence_ids: Sequence[str],
    reason_code: str,
) -> HypothesisAssessment:
    """Apply one evidence-cited transition while preserving sticky conflicts."""
    normalized_evidence = tuple(
        sorted(
            set(assessment.evidence_ids)
            | {value.strip() for value in evidence_ids if value.strip()}
        )
    )
    if disposition in _CLOSED_DISPOSITIONS and not normalized_evidence:
        raise ValueError("Closed hypothesis disposition requires public evidence.")
    if _REASON_CODE_PATTERN.fullmatch(reason_code) is None:
        raise ValueError("Hypothesis reason code is invalid.")

    if assessment.has_high_quality_conflict:
        if normalized_evidence == assessment.evidence_ids:
            return assessment
        transition = HypothesisTransition(
            previous_disposition=assessment.disposition,
            next_disposition="unresolved",
            evidence_ids=normalized_evidence,
            reason_code="high_quality_evidence_conflict",
            assessment_source="deterministic",
        )
        return replace(
            assessment,
            disposition="unresolved",
            evidence_ids=normalized_evidence,
            reason_code="high_quality_evidence_conflict",
            assessment_source="deterministic",
            has_high_quality_conflict=True,
            transitions=assessment.transitions + (transition,),
        )

    conflicts = (
        assessment.disposition in _CLOSED_DISPOSITIONS
        and disposition in _CLOSED_DISPOSITIONS
        and assessment.disposition != disposition
    )
    next_disposition: Disposition = "unresolved" if conflicts else disposition
    next_reason = "high_quality_evidence_conflict" if conflicts else reason_code
    if (
        assessment.disposition == next_disposition
        and assessment.evidence_ids == normalized_evidence
        and assessment.reason_code == next_reason
    ):
        return assessment
    transition = HypothesisTransition(
        previous_disposition=assessment.disposition,
        next_disposition=next_disposition,
        evidence_ids=normalized_evidence,
        reason_code=next_reason,
        assessment_source="deterministic",
    )
    return replace(
        assessment,
        disposition=next_disposition,
        evidence_ids=normalized_evidence,
        reason_code=next_reason,
        assessment_source="deterministic",
        has_high_quality_conflict=conflicts,
        transitions=assessment.transitions + (transition,),
    )


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


def _matching_evidence_ids(
    predicate: EvidencePredicate,
    facts: Sequence[DiagnosticFact],
) -> tuple[str, ...]:
    from super_ai.aiops.facts import predicate_evidence_ids

    return predicate_evidence_ids(facts, predicate)


def _apply_outcomes(
    assessment: HypothesisAssessment,
    outcomes: dict[Disposition, list[tuple[HypothesisEvidenceRule, str]]],
) -> HypothesisAssessment:
    if not outcomes or assessment.has_high_quality_conflict:
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
        first_disposition = sorted(active)[0]
        seeded = assessment
        if assessment.disposition == "unresolved":
            first_rule = sorted(
                outcomes[first_disposition],
                key=lambda item: (item[0].template_id, item[1]),
            )[0][0]
            seeded = apply_deterministic_transition(
                assessment,
                disposition=first_disposition,
                evidence_ids=tuple(
                    evidence_id for _, evidence_id in outcomes[first_disposition]
                ),
                reason_code=first_rule.reason_code,
            )
        conflicting_disposition = cast(
            Disposition,
            next(
                disposition
                for disposition in sorted(active)
                if disposition != seeded.disposition
            ),
        )
        conflict_values = outcomes.get(conflicting_disposition, [])
        return apply_deterministic_transition(
            seeded,
            disposition=conflicting_disposition,
            evidence_ids=evidence_ids,
            reason_code=(
                sorted(
                    conflict_values,
                    key=lambda item: (item[0].template_id, item[1]),
                )[0][0].reason_code
                if conflict_values
                else "high_quality_evidence_conflict"
            ),
        )

    next_disposition = next(iter(active))
    first_rule = sorted(
        outcomes[next_disposition], key=lambda item: (item[0].template_id, item[1])
    )[0][0]
    return apply_deterministic_transition(
        assessment,
        disposition=next_disposition,
        evidence_ids=evidence_ids,
        reason_code=first_rule.reason_code,
    )
