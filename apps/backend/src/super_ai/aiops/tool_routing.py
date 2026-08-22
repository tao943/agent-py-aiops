"""Deterministic Task-level routing for automatic alert diagnostic tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ORDER_POOL_SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"
ORDER_POOL_AUTOMATIC_TOOLS = frozenset(
    {
        "SearchLog",
        "InspectOrderPoolState",
        "InspectOrderDatabaseSessions",
        "VerifyOrderDatabaseReachability",
    }
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SCOPE_KEYS = frozenset({"runId", "scenarioId", "incidentId", "fromMs", "toMs"})


@dataclass(frozen=True, slots=True)
class AutomaticLiveEvidenceScope:
    run_id: str
    scenario_id: str
    incident_id: str
    from_ms: int
    to_ms: int


@dataclass(frozen=True, slots=True)
class TaskReadOnlyToolRoute:
    scoped: bool
    allowed_tools: frozenset[str] | None
    scope: AutomaticLiveEvidenceScope | None


def route_task_read_only_tools(
    input_payload: Mapping[str, object],
) -> TaskReadOnlyToolRoute:
    """Return a restrictive route before resolving any owner MCP client."""
    automatic_order_pool = (
        input_payload.get("benchmarkMode") == "live"
        and input_payload.get("benchmarkScenarioId") == ORDER_POOL_SCENARIO_ID
    )
    if not automatic_order_pool:
        if "liveEvidenceScope" in input_payload:
            return TaskReadOnlyToolRoute(True, frozenset(), None)
        return TaskReadOnlyToolRoute(False, None, None)

    scope = parse_automatic_live_evidence_scope(input_payload.get("liveEvidenceScope"))
    if scope is None:
        return TaskReadOnlyToolRoute(True, frozenset(), None)
    return TaskReadOnlyToolRoute(True, ORDER_POOL_AUTOMATIC_TOOLS, scope)


def parse_automatic_live_evidence_scope(
    raw_scope: object,
) -> AutomaticLiveEvidenceScope | None:
    if not isinstance(raw_scope, Mapping):
        return None
    scope = raw_scope
    if set(scope) != _SCOPE_KEYS or not all(isinstance(key, str) for key in scope):
        return None
    run_id = scope.get("runId")
    scenario_id = scope.get("scenarioId")
    incident_id = scope.get("incidentId")
    from_ms = scope.get("fromMs")
    to_ms = scope.get("toMs")
    if (
        not isinstance(run_id, str)
        or not _RUN_ID.fullmatch(run_id)
        or ".." in run_id
        or "/" in run_id
        or "\\" in run_id
        or scenario_id != ORDER_POOL_SCENARIO_ID
        or incident_id != f"{ORDER_POOL_SCENARIO_ID}-{run_id}"
        or not isinstance(from_ms, int)
        or isinstance(from_ms, bool)
        or not isinstance(to_ms, int)
        or isinstance(to_ms, bool)
        or from_ms <= 0
        or not from_ms < to_ms
        or to_ms - from_ms > 3_600_000
    ):
        return None
    return AutomaticLiveEvidenceScope(
        run_id=run_id,
        scenario_id=ORDER_POOL_SCENARIO_ID,
        incident_id=incident_id,
        from_ms=from_ms,
        to_ms=to_ms,
    )
