from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest

from super_ai.evaluation.live.diagnostics import LivePostgresEvidenceMcpClient
from super_ai.evaluation.live.domain import (
    LiveClsScope,
    LiveEvidenceContext,
    LiveEvidenceReadiness,
    LiveFaultObservation,
)
from super_ai.evaluation.live.evidence_client import LiveCompositeEvidenceMcpClient
from super_ai.mcp_client import McpClientError, McpToolDefinition

SCOPE = LiveClsScope(
    region="ap-guangzhou",
    topic_id="topic-live",
    from_ms=1_000,
    to_ms=10_000,
    run_id="run-1",
    scenario_id="APY-LIVE-PG-LOCK-001",
    incident_id="APY-LIVE-PG-LOCK-001-run-1",
)
CLS_CONTEXT = LiveEvidenceContext(
    source="cls",
    incident_id=SCOPE.incident_id,
    cls_scope=SCOPE,
    readiness=LiveEvidenceReadiness(3, 3, 1, 2_000, 3_000),
)
LOCAL_CONTEXT = LiveEvidenceContext.local(incident_id=SCOPE.incident_id)
OBSERVATION = LiveFaultObservation(101, 102, True, True)


def _record(*, run_id: str = "run-1") -> dict[str, object]:
    return {
        "run_id": run_id,
        "scenario_id": SCOPE.scenario_id,
        "incident_id": (
            SCOPE.incident_id if run_id == "run-1" else f"{SCOPE.scenario_id}-{run_id}"
        ),
        "event": "order_update_timeout",
        "message": "Order update timed out.",
    }


def _official_output(*records: Mapping[str, object]) -> list[dict[str, str]]:
    logs = [{"LogJson": json.dumps(record)} for record in records]
    return [{"type": "text", "text": json.dumps(logs)}]


def valid_search_arguments() -> dict[str, object]:
    return {
        "Region": SCOPE.region,
        "TopicId": SCOPE.topic_id,
        "From": SCOPE.from_ms,
        "To": SCOPE.to_ms,
        "Query": (
            f'run_id:"{SCOPE.run_id}" AND scenario_id:"{SCOPE.scenario_id}" '
            f'AND incident_id:"{SCOPE.incident_id}"'
        ),
        "Limit": 20,
    }


class FakeClsClient:
    def __init__(self, output: object) -> None:
        self.output = output
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        return (
            McpToolDefinition(
                "SearchLog",
                "Search real CLS logs.",
                {"type": "object"},
                "cls",
            ),
        )

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.calls.append((name, arguments))
        return self.output


@pytest.mark.asyncio
async def test_local_composite_exposes_only_postgres_tools() -> None:
    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=None,
        context=LOCAL_CONTEXT,
    )

    assert {item.name for item in await client.discover_tools()} == {
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "VerifyServiceHealth",
    }


@pytest.mark.asyncio
async def test_cls_composite_discovers_search_and_routes_postgres() -> None:
    cls_client = FakeClsClient(_official_output(_record()))
    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=cls_client,
        context=CLS_CONTEXT,
    )

    assert {item.name for item in await client.discover_tools()} == {
        "SearchLog",
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "VerifyServiceHealth",
    }
    postgres = await client.call_tool("InspectPostgresLockGraph", {})
    assert isinstance(postgres, dict)
    assert postgres["benchmarkEvidenceId"] == "postgres-blocking-pid-edge"
    assert cls_client.calls == []


@pytest.mark.asyncio
async def test_cls_search_filters_cross_run_records_and_tags_evidence() -> None:
    cls_client = FakeClsClient(_official_output(_record(), _record(run_id="run-2")))
    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=cls_client,
        context=CLS_CONTEXT,
    )

    result = await client.call_tool("SearchLog", valid_search_arguments())

    assert isinstance(result, dict)
    assert result["benchmarkEvidenceId"] == "cls-live-request-timeout"
    assert result["recordCount"] == 1
    assert result["rejectedRecordCount"] == 1
    serialized = json.dumps(result)
    assert '"run_id": "run-1"' in serialized
    assert "run-2" not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("Region", "ap-shanghai"),
        ("TopicId", "other-topic"),
        ("From", 999),
        ("To", 10_001),
        ("Query", "*"),
        ("Limit", 0),
    ),
)
@pytest.mark.asyncio
async def test_cls_search_rejects_query_outside_prepared_scope(
    field: str, value: object
) -> None:
    arguments = valid_search_arguments()
    arguments[field] = value
    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=FakeClsClient(_official_output(_record())),
        context=CLS_CONTEXT,
    )

    with pytest.raises(McpClientError, match="scope is invalid"):
        await client.call_tool("SearchLog", arguments)


@pytest.mark.asyncio
async def test_cls_composite_rejects_missing_or_duplicate_search_tool() -> None:
    class InvalidClsClient(FakeClsClient):
        async def discover_tools(self) -> Sequence[McpToolDefinition]:
            return ()

    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=InvalidClsClient([]),
        context=CLS_CONTEXT,
    )

    with pytest.raises(McpClientError, match="exactly one SearchLog"):
        await client.discover_tools()


@pytest.mark.asyncio
async def test_composite_langchain_tools_keep_validation_boundary() -> None:
    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=FakeClsClient(_official_output(_record())),
        context=CLS_CONTEXT,
    )

    tools = await client.get_langchain_tools()

    assert {cast_tool.name for cast_tool in tools} == {
        "SearchLog",
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "VerifyServiceHealth",
    }
    search = next(item for item in tools if item.name == "SearchLog")
    with pytest.raises(McpClientError, match="scope is invalid"):
        await search.ainvoke({**valid_search_arguments(), "TopicId": "other-topic"})
