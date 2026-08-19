"""Public causal-intent contracts for diagnostic tool plans."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Literal, cast

from super_ai.memory.repositories import JsonDict

CausalIntent = Literal["trigger", "mechanism", "impact", "context"]
CausalIntentOrigin = Literal["model", "coverage_repair", "generic"]
CoreCausalRole = Literal["trigger", "mechanism", "impact"]

_ROLE_PRIORITY: tuple[CausalIntent, ...] = (
    "trigger",
    "mechanism",
    "impact",
    "context",
)
_CORE_ROLES: tuple[CoreCausalRole, ...] = ("trigger", "mechanism", "impact")

_TRIGGER_OR_MECHANISM = frozenset(
    {
        "GetDeploymentChanges",
        "InspectClientRetryPolicy",
        "InspectHttpAttempts",
        "InspectPostgresLockGraph",
        "InspectRateLimitTimeline",
        "InspectTransactionResourceOrder",
    }
)
_MECHANISM = frozenset({"InspectPostgresWaitGraph"})
_MECHANISM_OR_IMPACT = frozenset(
    {"InspectGatewayErrors", "InspectPostgresErrors", "InspectPostgresSessions"}
)
_CONTEXT_OR_MECHANISM = frozenset(
    {
        "GetDatabaseMetrics",
        "GetGatewayMetrics",
        "GetRedisConnectionMetrics",
        "GetServiceTopology",
        "InspectContainer",
        "InspectDatabasePool",
        "InspectGatewayRequestTimeline",
        "InspectHostLimits",
        "InspectNginx",
        "InspectRedisClientPool",
        "InspectRedisServer",
        "InspectTrafficAndDependencyHealth",
        "ListRedisClients",
        "ProbeUpstreamHealth",
        "QueryMetrics",
        "QueryTrace",
    }
)
_CONTEXT_OR_MECHANISM_OR_IMPACT = frozenset({"GetServiceMetrics"})
_TRIGGER_OR_CONTEXT_OR_MECHANISM = frozenset({"InspectPostgres", "InspectRedis"})
_CONTEXT_OR_IMPACT = frozenset({"VerifyServiceHealth"})
_LOG_EVIDENCE = frozenset({"SearchLog", "SearchLogs"})
_NON_DIAGNOSTIC_PREFIXES = (
    "Apply",
    "CreateRecovery",
    "Execute",
    "Propose",
    "Restart",
    "Rollback",
    "Write",
)


@dataclass(frozen=True, slots=True)
class PlanCausalCoverage:
    steps: tuple[JsonDict, ...]
    complete: bool
    missing_roles: tuple[CoreCausalRole, ...]
    ambiguous_trigger: bool


@dataclass(frozen=True, slots=True)
class CausalCoverage:
    trigger_count: int
    mechanism_count: int
    impact_count: int
    missing_roles: tuple[CoreCausalRole, ...]
    ambiguous_trigger: bool

    @property
    def complete(self) -> bool:
        return (
            self.trigger_count == 1
            and self.mechanism_count >= 1
            and self.impact_count >= 1
        )


def allowed_causal_intents(tool_name: str) -> frozenset[CausalIntent]:
    """Return the public observation roles a diagnostic tool can establish."""
    if tool_name in _TRIGGER_OR_MECHANISM:
        return frozenset({"trigger", "mechanism"})
    if tool_name in _MECHANISM:
        return frozenset({"mechanism"})
    if tool_name in _MECHANISM_OR_IMPACT:
        return frozenset({"mechanism", "impact"})
    if tool_name in _CONTEXT_OR_MECHANISM:
        return frozenset({"context", "mechanism"})
    if tool_name in _CONTEXT_OR_MECHANISM_OR_IMPACT:
        return frozenset({"context", "mechanism", "impact"})
    if tool_name in _TRIGGER_OR_CONTEXT_OR_MECHANISM:
        return frozenset({"trigger", "context", "mechanism"})
    if tool_name in _CONTEXT_OR_IMPACT:
        return frozenset({"context", "impact"})
    if tool_name in _LOG_EVIDENCE:
        return frozenset({"context", "mechanism", "impact"})
    if tool_name.startswith(_NON_DIAGNOSTIC_PREFIXES):
        return frozenset()
    return frozenset({"context"})


def repair_plan_causal_coverage(
    steps: Sequence[JsonDict],
) -> PlanCausalCoverage:
    """Minimally repair a normalized plan to cover one causal investigation."""
    original = tuple(dict(step) for step in steps)
    allowed_by_step = tuple(
        tuple(
            role
            for role in _ROLE_PRIORITY
            if role in allowed_causal_intents(str(step.get("tool", "")))
        )
        for step in original
    )

    candidates: list[tuple[int, tuple[int, ...], tuple[CausalIntent, ...]]] = []
    if all(allowed_by_step):
        for assignment in product(*allowed_by_step):
            typed_assignment = cast(tuple[CausalIntent, ...], assignment)
            if typed_assignment.count("trigger") != 1:
                continue
            if "mechanism" not in typed_assignment or "impact" not in typed_assignment:
                continue
            changes = sum(
                role != step.get("causalIntent")
                for step, role in zip(original, typed_assignment, strict=True)
            )
            priority = tuple(_ROLE_PRIORITY.index(role) for role in typed_assignment)
            candidates.append((changes, priority, typed_assignment))

    if candidates:
        _, _, assignment = min(candidates)
        repaired: list[JsonDict] = []
        for step, role in zip(original, assignment, strict=True):
            updated = dict(step)
            if role != step.get("causalIntent"):
                updated["causalIntent"] = role
                updated["causalIntentOrigin"] = "coverage_repair"
            repaired.append(updated)
        return PlanCausalCoverage(
            steps=tuple(repaired),
            complete=True,
            missing_roles=(),
            ambiguous_trigger=False,
        )

    trigger_count = sum(step.get("causalIntent") == "trigger" for step in original)
    ambiguous_trigger = trigger_count > 1
    missing_roles: tuple[CoreCausalRole, ...]
    if ambiguous_trigger:
        missing_roles = _CORE_ROLES
    else:
        present = {
            cast(CoreCausalRole, step.get("causalIntent"))
            for step in original
            if step.get("causalIntent") in _CORE_ROLES
        }
        missing_roles = tuple(role for role in _CORE_ROLES if role not in present)
    return PlanCausalCoverage(
        steps=original,
        complete=False,
        missing_roles=missing_roles,
        ambiguous_trigger=ambiguous_trigger,
    )


def supported_causal_coverage(
    *,
    hypothesis_states: Sequence[Mapping[str, object]],
    observation_decisions: Sequence[Mapping[str, object]],
) -> CausalCoverage:
    """Count evidence-linked causal roles for one supported public hypothesis."""
    supported = [
        state
        for state in hypothesis_states
        if state.get("status") == "supported" and isinstance(state.get("id"), str)
    ]
    counts: dict[CoreCausalRole, int] = {
        "trigger": 0,
        "mechanism": 0,
        "impact": 0,
    }
    if len(supported) == 1:
        hypothesis_id = cast(str, supported[0]["id"])
        linked_evidence = _string_set(supported[0].get("evidenceIds"))
        for observation in observation_decisions:
            role = observation.get("causalRole")
            summary = observation.get("summary")
            if (
                role not in counts
                or not isinstance(summary, str)
                or not summary.strip()
                or hypothesis_id not in _string_set(observation.get("supports"))
                or not (
                    linked_evidence & _string_set(observation.get("evidenceIds"))
                )
            ):
                continue
            counts[role] += 1
    missing: tuple[CoreCausalRole, ...] = tuple(
        role
        for role in _CORE_ROLES
        if (counts[role] != 1 if role == "trigger" else counts[role] < 1)
    )
    return CausalCoverage(
        trigger_count=counts["trigger"],
        mechanism_count=counts["mechanism"],
        impact_count=counts["impact"],
        missing_roles=missing,
        ambiguous_trigger=counts["trigger"] > 1,
    )


def next_causal_refinement_index(
    *,
    plan: Sequence[JsonDict],
    plan_index: int,
    missing_roles: Collection[CoreCausalRole],
    supported_hypothesis_id: str | None,
    executed_fingerprints: Collection[str],
    fingerprint: Callable[[Mapping[str, object]], str],
) -> int | None:
    """Select the next unexecuted plan step that can fill a causal-role gap."""
    if supported_hypothesis_id is None or not missing_roles:
        return None
    missing = set(missing_roles)
    executed = set(executed_fingerprints)
    cursor = min(max(plan_index, 0), len(plan))
    candidate_indexes = (*range(cursor, len(plan)), *range(0, cursor))
    for index in candidate_indexes:
        step = plan[index]
        if step.get("causalIntent") not in missing:
            continue
        if supported_hypothesis_id not in _string_set(step.get("testsHypotheses")):
            continue
        if fingerprint(step) in executed:
            continue
        return index
    for index in candidate_indexes:
        step = plan[index]
        if not (allowed_causal_intents(str(step.get("tool") or "")) & missing):
            continue
        if supported_hypothesis_id not in _string_set(step.get("testsHypotheses")):
            continue
        if fingerprint(step) in executed:
            continue
        return index
    return None


def _string_set(value: object) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return set()
    return {item for item in cast(Sequence[object], value) if isinstance(item, str)}
