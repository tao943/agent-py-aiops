"""Public causal-intent contracts for diagnostic tool plans."""

from __future__ import annotations

from collections.abc import Sequence
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
        "GetServiceMetrics",
        "GetServiceTopology",
        "InspectContainer",
        "InspectDatabasePool",
        "InspectGatewayRequestTimeline",
        "InspectHostLimits",
        "InspectNginx",
        "InspectPostgres",
        "InspectRedis",
        "InspectRedisClientPool",
        "InspectRedisServer",
        "InspectTrafficAndDependencyHealth",
        "ListRedisClients",
        "ProbeUpstreamHealth",
        "QueryMetrics",
        "QueryTrace",
    }
)
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
