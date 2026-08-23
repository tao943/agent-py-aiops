from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from super_ai.aiops.diagnostics import AiopsDiagnosticService, task_scoped_source_fingerprint
from super_ai.aiops.investigation import TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES
from super_ai.aiops.tool_routing import (
    ORDER_POOL_AUTOMATIC_TOOLS,
    route_task_read_only_tools,
)
from super_ai.mcp.scoped_client import ScopedCompositeMcpClient, ScopedMcpSource
from super_ai.mcp_client import McpClientError, McpToolDefinition

OBJECT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _scope(run_id: str = "run-a") -> dict[str, object]:
    return {
        "runId": run_id,
        "scenarioId": "APY-LIVE-ORDER-POOL-LEAK-001",
        "incidentId": f"APY-LIVE-ORDER-POOL-LEAK-001-{run_id}",
        "fromMs": 1_777_000_000_000,
        "toMs": 1_777_001_800_000,
    }


def _automatic_payload(run_id: str = "run-a") -> dict[str, object]:
    return {
        "automaticClosureMode": True,
        "benchmarkMode": "live",
        "benchmarkScenarioId": "APY-LIVE-ORDER-POOL-LEAK-001",
        "liveEvidenceScope": _scope(run_id),
    }


def test_order_pool_task_routes_exact_read_only_tools() -> None:
    route = route_task_read_only_tools(_automatic_payload())

    assert route.scoped
    assert route.allowed_tools == ORDER_POOL_AUTOMATIC_TOOLS
    assert route.scope is not None
    assert route.scope.run_id == "run-a"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "automaticClosureMode": True,
            "benchmarkMode": "live",
            "benchmarkScenarioId": "APY-LIVE-ORDER-POOL-LEAK-001",
        },
        {
            **_automatic_payload(),
            "liveEvidenceScope": {**_scope(), "runId": "../run-a"},
        },
        {
            **_automatic_payload(),
            "liveEvidenceScope": {**_scope(), "extra": "not-allowed"},
        },
        {
            **_automatic_payload(),
            "liveEvidenceScope": {
                **_scope(),
                "scenarioId": "APY-LIVE-PG-LOCK-001",
            },
        },
    ],
)
def test_invalid_automatic_scope_never_falls_back_to_owner_tools(
    payload: Mapping[str, object],
) -> None:
    route = route_task_read_only_tools(payload)

    assert route.scoped
    assert route.allowed_tools == frozenset()
    assert route.scope is None


def test_ordinary_task_remains_unscoped() -> None:
    route = route_task_read_only_tools({"query": "inspect a normal incident"})

    assert not route.scoped
    assert route.allowed_tools is None
    assert route.scope is None


def test_direct_live_benchmark_without_automatic_marker_remains_unscoped() -> None:
    route = route_task_read_only_tools(
        {
            "benchmarkMode": "live",
            "benchmarkScenarioId": "APY-LIVE-ORDER-POOL-LEAK-001",
        }
    )

    assert not route.scoped
    assert route.allowed_tools is None


@pytest.mark.asyncio
async def test_ordinary_state_does_not_materialize_an_absent_live_scope() -> None:
    owner = FakeRuntimeMcpClient((_definition("InspectContainer", "runtime"),))
    service = AiopsDiagnosticService(
        repositories=cast(Any, object()),
        llm_provider=cast(Any, object()),
        retrieval_tool=cast(Any, object()),
        mcp_client=cast(Any, owner),
        cls_region="unused",
        cls_topic_id="unused",
    )
    routed = await service._mcp_client_for(  # pyright: ignore[reportPrivateUsage]
        "owner",
        cast(
            Any,
            {
                "automatic_closure_mode": False,
                "benchmark_mode": "",
                "benchmark_scenario_id": "",
                "live_evidence_scope": {},
                "task_local_live_scope": False,
            },
        ),
    )

    assert routed is owner


class FakeRuntimeMcpClient:
    def __init__(self, definitions: Sequence[McpToolDefinition]) -> None:
        self.definitions = tuple(definitions)
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        return self.definitions

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.calls.append((name, dict(arguments)))
        return {"tool": name}

    async def get_langchain_tools(self) -> list[Any]:
        return []


def _definition(name: str, server_name: str) -> McpToolDefinition:
    return McpToolDefinition(name, name, OBJECT_SCHEMA, server_name)


@pytest.mark.asyncio
async def test_scoped_composite_exposes_only_explicit_trusted_read_only_tools() -> None:
    owner = FakeRuntimeMcpClient(
        (
            _definition("SearchLog", "cls"),
            _definition("RestartService", "unsafe"),
        )
    )
    runtime = FakeRuntimeMcpClient((_definition("InspectOrderPoolState", "order-pool-live"),))
    client = ScopedCompositeMcpClient(
        (
            ScopedMcpSource(owner, frozenset({"SearchLog"})),
            ScopedMcpSource(runtime, frozenset({"InspectOrderPoolState"})),
        ),
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    )

    tools = await client.discover_tools()

    assert {tool.name for tool in tools} == {"SearchLog", "InspectOrderPoolState"}
    assert await client.call_tool("SearchLog", {"Query": "safe"}) == {"tool": "SearchLog"}
    with pytest.raises(McpClientError, match="not available"):
        await client.call_tool("RestartService", {})
    assert all(name != "RestartService" for name, _ in owner.calls)


@pytest.mark.asyncio
async def test_scoped_composite_rejects_wrong_server_and_duplicate_routes() -> None:
    wrong_server = ScopedCompositeMcpClient(
        (
            ScopedMcpSource(
                FakeRuntimeMcpClient((_definition("SearchLog", "fake"),)), frozenset({"SearchLog"})
            ),
        ),
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    )
    with pytest.raises(McpClientError, match="provenance"):
        await wrong_server.discover_tools()

    first = FakeRuntimeMcpClient((_definition("SearchLog", "cls"),))
    second = FakeRuntimeMcpClient((_definition("SearchLog", "cls"),))
    duplicate = ScopedCompositeMcpClient(
        (
            ScopedMcpSource(first, frozenset({"SearchLog"})),
            ScopedMcpSource(second, frozenset({"SearchLog"})),
        ),
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    )
    with pytest.raises(McpClientError, match="Duplicate"):
        await duplicate.discover_tools()


def test_source_fingerprint_is_unique_by_task_and_tool() -> None:
    pool = task_scoped_source_fingerprint(
        "task-a", "InspectOrderPoolState", {}
    )
    sessions = task_scoped_source_fingerprint(
        "task-a", "InspectOrderDatabaseSessions", {}
    )
    other_task = task_scoped_source_fingerprint(
        "task-b", "InspectOrderPoolState", {}
    )

    assert len({pool, sessions, other_task}) == 3
