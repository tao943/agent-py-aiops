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
