"""Bounded public observation facts for deterministic AIOps reasoning."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from super_ai.aiops.adjudication import DiagnosticFact, EvidencePredicate

JsonScalar = str | int | float | bool | None

_MAX_FACTS_PER_OBSERVATION = 64
_MAX_DEPTH = 3
_SECRET_KEYS = frozenset({"secret", "token", "password", "apikey", "authorization"})
_DIRECT_FACT_KEYS = frozenset(
    {
        "InspectContainer.status",
        "InspectContainer.health",
        "InspectContainer.configuredPorts",
        "InspectContainer.listeningPorts",
        "InspectNginx.upstreamPort",
        "InspectNginx.resolvedAddresses",
        "InspectNginx.responseStatus",
        "InspectNginxRequestTimeline.gatewayStatus",
        "InspectNginxRequestTimeline.requestDurationMs",
        "InspectNginxRequestTimeline.upstreamConnectSucceeded",
        "ReadNginxTimeoutSummary.gatewayTimeoutObserved",
        "ReadNginxTimeoutSummary.readDeadlineElapsed",
        "ProbeLiveEvalUpstream.status",
        "ProbeLiveEvalUpstream.healthy",
        "ProbeLiveEvalGateway.status",
        "ProbeLiveEvalGateway.healthy",
        "ProbeLiveEvalGateway.latencyMs",
        "SearchLog.records.event",
        "InspectRedis.processStatus",
        "InspectRedis.listening",
        "InspectRedisClientPool.lastError",
        "InspectRedisClientPool.staleConnections",
    }
)


@dataclass(frozen=True, slots=True)
class PublicToolObservation:
    tool_name: str
    evidence_id: str
    output: Mapping[str, object]


def extract_public_facts(
    observations: Sequence[PublicToolObservation],
) -> tuple[DiagnosticFact, ...]:
    """Flatten bounded, secret-filtered public observation fields."""
    facts: list[DiagnosticFact] = []
    for observation in observations:
        flattened: list[tuple[str, JsonScalar | tuple[JsonScalar, ...]]] = []
        _flatten_mapping(observation.output, prefix=(), depth=0, output=flattened)
        for path, value in sorted(flattened, key=lambda item: item[0])[
            :_MAX_FACTS_PER_OBSERVATION
        ]:
            key = f"{observation.tool_name}.{path}"
            quality: Literal["direct", "context"] = (
                "direct" if key in _DIRECT_FACT_KEYS else "context"
            )
            facts.append(
                DiagnosticFact(
                    key=key,
                    value=value,
                    evidence_id=observation.evidence_id,
                    source_tool=observation.tool_name,
                    quality=quality,
                    public=True,
                )
            )
    return tuple(
        sorted(facts, key=lambda item: (item.key, item.evidence_id, repr(item.value)))
    )


def evaluate_predicate(
    facts: Sequence[DiagnosticFact],
    predicate: EvidencePredicate,
) -> bool:
    """Evaluate one bounded predicate without interpreting its causal meaning."""
    public_facts = tuple(fact for fact in facts if fact.public)
    left_values = tuple(fact.value for fact in public_facts if fact.key == predicate.left_fact)
    if not left_values:
        return False
    if predicate.right_fact is not None:
        right_values = tuple(
            fact.value for fact in public_facts if fact.key == predicate.right_fact
        )
        return any(
            _evaluate_values(left, predicate.operator, right)
            for left in left_values
            for right in right_values
        )
    return any(
        _evaluate_values(left, predicate.operator, predicate.expected) for left in left_values
    )


def predicate_evidence_ids(
    facts: Sequence[DiagnosticFact],
    predicate: EvidencePredicate,
) -> tuple[str, ...]:
    """Return all direct public Evidence IDs participating in a true predicate."""
    direct = tuple(fact for fact in facts if fact.public and fact.quality == "direct")
    left = tuple(fact for fact in direct if fact.key == predicate.left_fact)
    matches: set[str] = set()
    if predicate.right_fact is None:
        for fact in left:
            if _evaluate_values(fact.value, predicate.operator, predicate.expected):
                matches.add(fact.evidence_id)
        return tuple(sorted(matches))
    right = tuple(fact for fact in direct if fact.key == predicate.right_fact)
    for left_fact in left:
        for right_fact in right:
            if _evaluate_values(left_fact.value, predicate.operator, right_fact.value):
                matches.update((left_fact.evidence_id, right_fact.evidence_id))
    return tuple(sorted(matches))


def _flatten_mapping(
    mapping: Mapping[str, object],
    *,
    prefix: tuple[str, ...],
    depth: int,
    output: list[tuple[str, JsonScalar | tuple[JsonScalar, ...]]],
) -> None:
    if depth >= _MAX_DEPTH or len(output) >= _MAX_FACTS_PER_OBSERVATION:
        return
    for raw_key in sorted(mapping):
        if len(output) >= _MAX_FACTS_PER_OBSERVATION:
            return
        if _is_secret_key(raw_key):
            continue
        value = mapping[raw_key]
        path = prefix + (raw_key,)
        if _is_json_scalar(value):
            output.append((".".join(path), cast(JsonScalar, value)))
            continue
        sequence = _scalar_sequence(value)
        if sequence is not None:
            output.append((".".join(path), sequence))
            continue
        mapping_sequence = _string_mapping_sequence(value)
        if mapping_sequence is not None:
            aggregated: dict[str, list[JsonScalar]] = {}
            for item in mapping_sequence[:_MAX_FACTS_PER_OBSERVATION]:
                nested_output: list[
                    tuple[str, JsonScalar | tuple[JsonScalar, ...]]
                ] = []
                _flatten_mapping(
                    item,
                    prefix=path,
                    depth=depth + 1,
                    output=nested_output,
                )
                for nested_path, nested_value in nested_output:
                    if isinstance(nested_value, tuple):
                        continue
                    aggregated.setdefault(nested_path, []).append(nested_value)
            for nested_path in sorted(aggregated):
                if len(output) >= _MAX_FACTS_PER_OBSERVATION:
                    return
                output.append((nested_path, tuple(aggregated[nested_path])))
            continue
        nested = _string_mapping(value)
        if nested is not None:
            _flatten_mapping(nested, prefix=path, depth=depth + 1, output=output)


def _evaluate_values(left: object, operator: str, right: object) -> bool:
    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if operator == "exists":
        return True
    if operator == "empty":
        return left in (None, "", (), [], {})
    if operator == "truthy":
        return bool(left)
    if operator == "contains":
        return _safe_contains(left, right)
    if operator == "in":
        return _safe_contains(right, left)
    return False


def _safe_contains(container: object, item: object) -> bool:
    if isinstance(container, str):
        return isinstance(item, str) and item in container
    if isinstance(container, Sequence) and not isinstance(container, bytes):
        return item in cast(Sequence[object], container)
    return False


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in _SECRET_KEYS or any(
        normalized.endswith(secret) for secret in _SECRET_KEYS
    )


def _is_json_scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes)
    )


def _scalar_sequence(value: object) -> tuple[JsonScalar, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    values = tuple(cast(Sequence[object], value))
    if not all(_is_json_scalar(item) for item in values):
        return None
    return cast(tuple[JsonScalar, ...], values)


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)


def _string_mapping_sequence(value: object) -> tuple[Mapping[str, object], ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    mappings: list[Mapping[str, object]] = []
    for item in cast(Sequence[object], value):
        mapping = _string_mapping(item)
        if mapping is None:
            return None
        mappings.append(mapping)
    return tuple(mappings) if mappings else None
