"""Deterministic validation and fan-in for Investigator evidence packets."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from super_ai.aiops.investigation import (
    EvidenceClaim,
    EvidencePacket,
    EvidenceQuality,
    InvestigatorType,
    JsonValue,
)
from super_ai.aiops.specialists import (
    PublicAssessmentSignal,
    SpecialistAssignment,
    SpecialistResult,
    SpecialistRole,
)
from super_ai.memory.repositories import (
    AgentToolCallAuditRecord,
    DiagnosticEvidenceRecord,
    ToolCallAuditRecord,
)

_INVESTIGATOR_ORDER: Mapping[InvestigatorType, int] = MappingProxyType(
    {"knowledge": 0, "runtime": 1, "log": 2, "change": 3}
)
_QUALITY_RANK: Mapping[EvidenceQuality, int] = MappingProxyType(
    {"reference": 0, "context": 1, "direct": 2}
)
_INVALID_PACKET = "invalid_evidence_packet"


@dataclass(frozen=True, slots=True)
class AggregationContext:
    owner_user_id: str
    task_id: str
    investigator_by_dispatch: Mapping[str, InvestigatorType]
    evidence_ids: frozenset[str]
    completed_tool_call_ids: frozenset[str]
    tool_name_by_call_id: Mapping[str, str]
    tool_call_id_by_evidence_id: Mapping[str, str]
    allowed_tools_by_investigator: Mapping[InvestigatorType, frozenset[str]]
    maximum_quality_by_evidence_id: Mapping[str, EvidenceQuality]

    def __post_init__(self) -> None:
        if not self.owner_user_id.strip() or not self.task_id.strip():
            raise ValueError("Aggregation context requires owner and task identity.")
        object.__setattr__(
            self,
            "investigator_by_dispatch",
            MappingProxyType(dict(self.investigator_by_dispatch)),
        )
        object.__setattr__(
            self,
            "tool_name_by_call_id",
            MappingProxyType(dict(self.tool_name_by_call_id)),
        )
        object.__setattr__(
            self,
            "tool_call_id_by_evidence_id",
            MappingProxyType(dict(self.tool_call_id_by_evidence_id)),
        )
        object.__setattr__(
            self,
            "allowed_tools_by_investigator",
            MappingProxyType(
                {
                    investigator: frozenset(tools)
                    for investigator, tools in self.allowed_tools_by_investigator.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "maximum_quality_by_evidence_id",
            MappingProxyType(dict(self.maximum_quality_by_evidence_id)),
        )


@dataclass(frozen=True, slots=True)
class AggregationResult:
    accepted_packets: tuple[EvidencePacket, ...]
    rejected_dispatches: Mapping[str, str]
    claims: tuple[EvidenceClaim, ...]
    conflicts: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class SpecialistAggregationContext:
    """Code-owned scope and persisted provenance for Specialist fan-in."""

    owner_user_id: str
    task_id: str
    graph_version: str
    assignments: Mapping[SpecialistRole, SpecialistAssignment]
    evidence_by_id: Mapping[str, DiagnosticEvidenceRecord]
    completed_tool_audit_by_id: Mapping[
        str, ToolCallAuditRecord | AgentToolCallAuditRecord
    ]

    def __post_init__(self) -> None:
        if not self.owner_user_id.strip() or not self.task_id.strip():
            raise ValueError("Specialist aggregation requires owner and task identity.")
        if not self.graph_version.strip():
            raise ValueError("Specialist aggregation requires a graph version.")
        if any(role != assignment.role for role, assignment in self.assignments.items()):
            raise ValueError("Specialist aggregation assignment role is invalid.")
        object.__setattr__(
            self,
            "assignments",
            MappingProxyType(dict(sorted(self.assignments.items()))),
        )
        object.__setattr__(
            self,
            "evidence_by_id",
            MappingProxyType(dict(sorted(self.evidence_by_id.items()))),
        )
        object.__setattr__(
            self,
            "completed_tool_audit_by_id",
            MappingProxyType(dict(sorted(self.completed_tool_audit_by_id.items()))),
        )


@dataclass(frozen=True, slots=True)
class AggregatedInvestigation:
    """Deterministic Specialist output; it cannot decide or authorize recovery."""

    specialist_statuses: Mapping[str, str]
    specialist_evidence_statuses: Mapping[str, str]
    specialist_analysis_statuses: Mapping[str, str]
    specialist_analysis_error_codes: Mapping[str, str]
    specialist_analysis_attempt_counts: Mapping[str, int]
    specialist_follow_up_question_counts: Mapping[str, int]
    specialist_soft_deadline_exceeded: Mapping[str, bool]
    specialist_hard_deadline_exceeded: Mapping[str, bool]
    specialist_completed_tool_counts: Mapping[str, int]
    specialist_expected_tool_counts: Mapping[str, int]
    evidence: tuple[str, ...]
    normalized_facts: tuple[EvidenceClaim, ...]
    hypothesis_signals: tuple[PublicAssessmentSignal, ...]
    conflicts: tuple[Mapping[str, object], ...]
    source_groups: Mapping[str, tuple[str, ...]]
    missing_domains: tuple[str, ...]
    budget_usage: Mapping[str, int]
    aggregation_checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specialist_statuses",
            MappingProxyType(dict(sorted(self.specialist_statuses.items()))),
        )
        for field_name in (
            "specialist_evidence_statuses",
            "specialist_analysis_statuses",
            "specialist_analysis_error_codes",
            "specialist_analysis_attempt_counts",
            "specialist_follow_up_question_counts",
            "specialist_soft_deadline_exceeded",
            "specialist_hard_deadline_exceeded",
            "specialist_completed_tool_counts",
            "specialist_expected_tool_counts",
        ):
            value = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(dict(sorted(value.items()))),
            )
        object.__setattr__(
            self,
            "source_groups",
            MappingProxyType(
                {
                    key: tuple(sorted(set(value)))
                    for key, value in sorted(self.source_groups.items())
                }
            ),
        )
        object.__setattr__(
            self,
            "budget_usage",
            MappingProxyType(dict(sorted(self.budget_usage.items()))),
        )
        object.__setattr__(
            self,
            "conflicts",
            tuple(MappingProxyType(dict(item)) for item in self.conflicts),
        )

    @property
    def terminal_failure_category(self) -> str | None:
        failed = {"failed", "timeout", "cancelled", "missing"}
        if self.specialist_statuses and all(
            status in failed for status in self.specialist_statuses.values()
        ):
            return "multi_investigation_failed"
        return None

    def to_checkpoint_payload(self) -> dict[str, object]:
        """Project only bounded public aggregation data into a checkpoint."""
        return {
            "specialistStatuses": dict(self.specialist_statuses),
            "specialistEvidenceStatuses": dict(self.specialist_evidence_statuses),
            "specialistAnalysisStatuses": dict(self.specialist_analysis_statuses),
            "specialistAnalysisErrorCodes": dict(
                self.specialist_analysis_error_codes
            ),
            "specialistAnalysisAttemptCounts": dict(
                self.specialist_analysis_attempt_counts
            ),
            "specialistFollowUpQuestionCounts": dict(
                self.specialist_follow_up_question_counts
            ),
            "specialistSoftDeadlineExceeded": dict(
                self.specialist_soft_deadline_exceeded
            ),
            "specialistHardDeadlineExceeded": dict(
                self.specialist_hard_deadline_exceeded
            ),
            "specialistCompletedToolCounts": dict(
                self.specialist_completed_tool_counts
            ),
            "specialistExpectedToolCounts": dict(self.specialist_expected_tool_counts),
            "evidence": list(self.evidence),
            "normalizedFacts": [_claim_checkpoint_payload(item) for item in self.normalized_facts],
            "hypothesisSignals": [
                _signal_checkpoint_payload(item) for item in self.hypothesis_signals
            ],
            "conflicts": [dict(item) for item in self.conflicts],
            "sourceGroups": {
                key: list(value) for key, value in self.source_groups.items()
            },
            "missingDomains": list(self.missing_domains),
            "budgetUsage": dict(self.budget_usage),
            "aggregationChecksum": self.aggregation_checksum,
            "terminalFailureCategory": self.terminal_failure_category,
        }


def aggregate_specialist_results(
    results: Sequence[SpecialistResult],
    *,
    context: SpecialistAggregationContext,
) -> AggregatedInvestigation:
    """Validate and merge Specialist results using persisted provenance only."""
    result_by_role: dict[SpecialistRole, SpecialistResult] = {}
    for result in results:
        existing = result_by_role.get(result.role)
        if existing is not None and existing != result:
            raise ValueError("conflicting_specialist_result")
        result_by_role[result.role] = result

    statuses: dict[str, str] = {}
    for role in context.assignments:
        result = result_by_role.get(role)
        statuses[role] = result.terminal_status if result is not None else "missing"
    if set(result_by_role).difference(context.assignments):
        raise ValueError("unexpected_specialist_result")

    evidence_ids: set[str] = set()
    source_groups: dict[str, set[str]] = defaultdict(set)
    ordered_facts: list[tuple[str, EvidenceClaim]] = []
    ordered_signals: list[tuple[str, PublicAssessmentSignal]] = []
    seen_fact_fingerprints: set[str] = set()
    seen_signal_fingerprints: set[str] = set()
    for role, result in sorted(result_by_role.items()):
        assignment = context.assignments[role]
        owned_ids = frozenset(result.evidence_ids)
        for evidence_id in owned_ids:
            record = context.evidence_by_id.get(evidence_id)
            if not _specialist_evidence_is_valid(
                record,
                role=role,
                assignment=assignment,
                context=context,
            ):
                raise ValueError("invalid_specialist_evidence")
            assert record is not None
            source_fingerprint = record.payload.get("sourceFingerprint")
            assert isinstance(source_fingerprint, str)
            evidence_ids.add(evidence_id)
            source_groups[source_fingerprint].add(evidence_id)

        for fact in result.fact_candidates:
            if not set(fact.evidence_ids).issubset(owned_ids):
                raise ValueError("invalid_specialist_evidence")
            if not set(fact.supports).issubset(assignment.hypotheses_to_test):
                raise ValueError("invalid_specialist_evidence")
            if not set(fact.refutes).issubset(assignment.hypotheses_to_test):
                raise ValueError("invalid_specialist_evidence")
            fingerprint = _specialist_claim_fingerprint(role, fact)
            if fingerprint not in seen_fact_fingerprints:
                seen_fact_fingerprints.add(fingerprint)
                ordered_facts.append((fingerprint, fact))

        for signal in result.proposed_assessments:
            if signal.hypothesis_id not in assignment.hypotheses_to_test:
                raise ValueError("invalid_specialist_evidence")
            if not set(signal.evidence_ids).issubset(owned_ids):
                raise ValueError("invalid_specialist_evidence")
            fingerprint = _specialist_signal_fingerprint(role, signal)
            if fingerprint not in seen_signal_fingerprints:
                seen_signal_fingerprints.add(fingerprint)
                ordered_signals.append((fingerprint, signal))

    ordered_facts.sort(key=lambda item: item[0])
    ordered_signals.sort(key=lambda item: item[0])
    facts = tuple(item[1] for item in ordered_facts)
    signals = tuple(item[1] for item in ordered_signals)
    missing_domains = tuple(
        role
        for role, status in sorted(statuses.items())
        if status in {"failed", "timeout", "cancelled", "missing"}
    )
    budget_usage = {
        role: result.model_call_count for role, result in sorted(result_by_role.items())
    }
    budget_usage["total"] = sum(budget_usage.values())
    checksum_material = "\x1f".join(
        (
            context.task_id,
            context.graph_version,
            *sorted(result.result_checksum for result in result_by_role.values()),
        )
    )
    return AggregatedInvestigation(
        specialist_statuses=statuses,
        specialist_evidence_statuses={
            role: result.evidence_status
            for role, result in sorted(result_by_role.items())
        },
        specialist_analysis_statuses={
            role: result.analysis_status
            for role, result in sorted(result_by_role.items())
        },
        specialist_analysis_error_codes={
            role: result.analysis_error_code
            for role, result in sorted(result_by_role.items())
            if result.analysis_error_code is not None
        },
        specialist_analysis_attempt_counts={
            role: result.analysis_attempt_count
            for role, result in sorted(result_by_role.items())
        },
        specialist_follow_up_question_counts={
            role: result.follow_up_question_count
            for role, result in sorted(result_by_role.items())
        },
        specialist_soft_deadline_exceeded={
            role: result.soft_deadline_exceeded
            for role, result in sorted(result_by_role.items())
        },
        specialist_hard_deadline_exceeded={
            role: result.hard_deadline_exceeded
            for role, result in sorted(result_by_role.items())
        },
        specialist_completed_tool_counts={
            role: result.completed_tool_count
            for role, result in sorted(result_by_role.items())
        },
        specialist_expected_tool_counts={
            role: result.expected_tool_count
            for role, result in sorted(result_by_role.items())
        },
        evidence=tuple(sorted(evidence_ids)),
        normalized_facts=facts,
        hypothesis_signals=signals,
        conflicts=_find_specialist_conflicts(ordered_facts),
        source_groups={
            key: tuple(sorted(value)) for key, value in sorted(source_groups.items())
        },
        missing_domains=missing_domains,
        budget_usage=budget_usage,
        aggregation_checksum=hashlib.sha256(checksum_material.encode("utf-8")).hexdigest(),
    )


def _specialist_evidence_is_valid(
    record: DiagnosticEvidenceRecord | None,
    *,
    role: SpecialistRole,
    assignment: SpecialistAssignment,
    context: SpecialistAggregationContext,
) -> bool:
    if record is None:
        return False
    if record.owner_user_id != context.owner_user_id or record.task_id != context.task_id:
        return False
    if record.tool_call_id is None:
        return False
    source_fingerprint = record.payload.get("sourceFingerprint")
    if not isinstance(source_fingerprint, str) or not source_fingerprint.strip():
        return False
    audit = context.completed_tool_audit_by_id.get(record.tool_call_id)
    if audit is None:
        return False
    if (
        audit.owner_user_id != context.owner_user_id
        or _tool_audit_task_id(audit) != context.task_id
        or audit.status != "completed"
        or audit.completed_at is None
    ):
        return False
    if audit.tool_name not in assignment.allowed_tools or record.source != audit.tool_name:
        return False
    capability_role = context.assignments.get(role)
    return capability_role is assignment


def _tool_audit_task_id(
    audit: ToolCallAuditRecord | AgentToolCallAuditRecord,
) -> str | None:
    if isinstance(audit, ToolCallAuditRecord):
        return audit.task_id
    return audit.diagnostic_task_id


def _specialist_claim_fingerprint(role: SpecialistRole, claim: EvidenceClaim) -> str:
    return hashlib.sha256(
        _canonical_json(
            cast(
                JsonValue,
                {
                    "role": role,
                    "claim": _claim_checkpoint_payload(claim),
                },
            )
        ).encode("utf-8")
    ).hexdigest()


def _specialist_signal_fingerprint(
    role: SpecialistRole,
    signal: PublicAssessmentSignal,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            cast(
                JsonValue,
                {
                    "role": role,
                    "signal": _signal_checkpoint_payload(signal),
                },
            )
        ).encode("utf-8")
    ).hexdigest()


def _claim_checkpoint_payload(claim: EvidenceClaim) -> dict[str, object]:
    return {
        "claimId": claim.claim_id,
        "value": _plain_json(claim.value),
        "quality": claim.quality,
        "causalRole": claim.causal_role,
        "supports": list(claim.supports),
        "refutes": list(claim.refutes),
        "evidenceIds": list(claim.evidence_ids),
        "targetComponent": claim.target_component,
        "observedAt": claim.observed_at.isoformat() if claim.observed_at else None,
        "timeScope": claim.time_scope,
    }


def _signal_checkpoint_payload(signal: PublicAssessmentSignal) -> dict[str, object]:
    return {
        "hypothesisId": signal.hypothesis_id,
        "disposition": signal.disposition,
        "evidenceIds": list(signal.evidence_ids),
        "summary": signal.summary,
    }


def _find_specialist_conflicts(
    facts: Sequence[tuple[str, EvidenceClaim]],
) -> tuple[Mapping[str, object], ...]:
    grouped: dict[tuple[str, str, str], list[tuple[str, EvidenceClaim]]] = defaultdict(
        list
    )
    for fingerprint, fact in facts:
        if fact.quality == "direct":
            grouped[(fact.claim_id, fact.target_component, fact.time_scope)].append(
                (fingerprint, fact)
            )
    conflicts: list[Mapping[str, object]] = []
    for (claim_id, component, time_scope), members in sorted(grouped.items()):
        if len({_canonical_json(item.value) for _, item in members}) <= 1:
            continue
        conflicts.append(
            MappingProxyType(
                {
                    "claimId": claim_id,
                    "targetComponent": component,
                    "timeScope": time_scope,
                    "claimFingerprints": tuple(
                        sorted(fingerprint for fingerprint, _ in members)
                    ),
                }
            )
        )
    return tuple(conflicts)


def aggregate_evidence_packets(
    packets: Sequence[EvidencePacket], *, context: AggregationContext
) -> AggregationResult:
    """Validate, normalize, and merge packets without making a diagnosis."""
    grouped: dict[str, list[EvidencePacket]] = defaultdict(list)
    for packet in packets:
        grouped[packet.dispatch_id].append(packet)

    accepted: list[EvidencePacket] = []
    rejected: dict[str, str] = {}
    for dispatch_id, dispatch_packets in grouped.items():
        packet = dispatch_packets[0]
        if any(candidate != packet for candidate in dispatch_packets[1:]):
            rejected[dispatch_id] = _INVALID_PACKET
            continue
        if not _packet_is_valid(packet, context=context):
            rejected[dispatch_id] = _INVALID_PACKET
            continue
        accepted.append(packet)

    accepted.sort(key=_packet_sort_key)
    ordered_claims: list[tuple[int, str, str, EvidenceClaim]] = []
    seen_fingerprints: set[str] = set()
    for packet in accepted:
        for claim in packet.claims:
            fingerprint = _claim_fingerprint(claim, packet.investigator_type)
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            ordered_claims.append(
                (
                    _INVESTIGATOR_ORDER[packet.investigator_type],
                    packet.dispatch_id,
                    fingerprint,
                    claim,
                )
            )
    ordered_claims.sort(key=lambda item: item[:3])
    claims = tuple(item[3] for item in ordered_claims)
    conflicts = _find_conflicts(ordered_claims)
    return AggregationResult(
        accepted_packets=tuple(accepted),
        rejected_dispatches=MappingProxyType(dict(sorted(rejected.items()))),
        claims=claims,
        conflicts=conflicts,
    )


def _packet_is_valid(
    packet: EvidencePacket, *, context: AggregationContext
) -> bool:
    if packet.owner_user_id != context.owner_user_id or packet.task_id != context.task_id:
        return False
    expected_investigator = context.investigator_by_dispatch.get(packet.dispatch_id)
    if expected_investigator != packet.investigator_type:
        return False
    allowed_tools = context.allowed_tools_by_investigator.get(
        packet.investigator_type, frozenset()
    )
    packet_call_ids = set(packet.tool_call_ids)
    for call_id in packet.tool_call_ids:
        if call_id not in context.completed_tool_call_ids:
            return False
        tool_name = context.tool_name_by_call_id.get(call_id)
        if tool_name is None or tool_name not in allowed_tools:
            return False

    for claim in packet.claims:
        if packet.investigator_type == "knowledge" and claim.quality != "reference":
            return False
        for evidence_id in claim.evidence_ids:
            if evidence_id not in context.evidence_ids:
                return False
            call_id = context.tool_call_id_by_evidence_id.get(evidence_id)
            if call_id is None or call_id not in packet_call_ids:
                return False
            maximum_quality = context.maximum_quality_by_evidence_id.get(evidence_id)
            if maximum_quality is None:
                return False
            if _QUALITY_RANK[claim.quality] > _QUALITY_RANK[maximum_quality]:
                return False
    return True


def _packet_sort_key(packet: EvidencePacket) -> tuple[int, str]:
    return (_INVESTIGATOR_ORDER[packet.investigator_type], packet.dispatch_id)


def _claim_fingerprint(
    claim: EvidenceClaim, investigator_type: InvestigatorType
) -> str:
    evidence_ids_hash = hashlib.sha256(
        "\x1f".join(sorted(claim.evidence_ids)).encode("utf-8")
    ).hexdigest()
    payload = (
        claim.claim_id,
        _canonical_json(claim.value),
        investigator_type,
        claim.target_component,
        claim.time_scope,
        evidence_ids_hash,
    )
    return hashlib.sha256("\x1e".join(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return cast(object, value)


def _find_conflicts(
    ordered_claims: Sequence[tuple[int, str, str, EvidenceClaim]],
) -> tuple[dict[str, object], ...]:
    groups: dict[tuple[str, str, str], list[tuple[str, EvidenceClaim]]] = defaultdict(
        list
    )
    for _, _, fingerprint, claim in ordered_claims:
        if claim.quality != "direct":
            continue
        groups[(claim.claim_id, claim.target_component, claim.time_scope)].append(
            (fingerprint, claim)
        )

    conflicts: list[dict[str, object]] = []
    for (claim_id, component, time_scope), members in sorted(groups.items()):
        distinct_values = {_canonical_json(claim.value) for _, claim in members}
        if len(distinct_values) <= 1:
            continue
        conflicts.append(
            {
                "claimId": claim_id,
                "targetComponent": component,
                "timeScope": time_scope,
                "claimFingerprints": tuple(
                    sorted(fingerprint for fingerprint, _ in members)
                ),
            }
        )
    return tuple(conflicts)
