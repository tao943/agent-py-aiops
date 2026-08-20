"""Validated composite MCP evidence boundary for Docker Live evaluation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from langchain_core.tools import StructuredTool

from super_ai.evaluation.live.domain import LiveClsScope, LiveEvidenceContext
from super_ai.mcp_client import McpClientError, McpToolDefinition


class LiveMcpClient(Protocol):
    async def discover_tools(self) -> Sequence[McpToolDefinition]: ...

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object: ...


class LiveCompositeEvidenceMcpClient:
    """Route scenario read tools and one validated, run-scoped CLS search."""

    def __init__(
        self,
        *,
        postgres_client: LiveMcpClient,
        cls_client: LiveMcpClient | None,
        context: LiveEvidenceContext,
    ) -> None:
        if context.source == "cls" and (cls_client is None or context.cls_scope is None):
            raise ValueError("CLS evidence context requires a CLS MCP client and scope.")
        if context.source == "local" and cls_client is not None:
            raise ValueError("Local evidence context cannot expose a CLS MCP client.")
        self._postgres_client = postgres_client
        self._cls_client = cls_client
        self._context = context

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        postgres_tools = tuple(await self._postgres_client.discover_tools())
        if self._context.source == "local":
            return postgres_tools
        assert self._cls_client is not None
        search_tools = tuple(
            item
            for item in await self._cls_client.discover_tools()
            if item.name == "SearchLog"
        )
        if len(search_tools) != 1:
            raise McpClientError("CLS Live requires exactly one SearchLog tool.")
        combined = postgres_tools + search_tools
        names = [item.name for item in combined]
        if len(names) != len(set(names)):
            raise McpClientError("Live evidence tool names must be unique.")
        return combined

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        if name != "SearchLog":
            return await self._postgres_client.call_tool(name, arguments)
        if self._context.source != "cls" or self._cls_client is None:
            raise McpClientError("SearchLog is unavailable in local Live mode.")
        scope = self._context.cls_scope
        assert scope is not None
        _validate_search_arguments(arguments, scope)
        output = await self._cls_client.call_tool(name, arguments)
        records = parse_cls_search_records(output)
        matching = tuple(record for record in records if _matches_scope(record, scope))
        return {
            "benchmarkEvidenceId": _cls_evidence_id(scope.scenario_id),
            "recordCount": len(matching),
            "rejectedRecordCount": len(records) - len(matching),
            "records": matching[:10],
        }

    async def get_langchain_tools(self) -> list[Any]:
        tools: list[Any] = []
        for definition in await self.discover_tools():

            async def invoke(
                _name: str = definition.name,
                **arguments: object,
            ) -> object:
                return await self.call_tool(_name, arguments)

            tools.append(
                StructuredTool(
                    name=definition.name,
                    description=definition.description,
                    args_schema=definition.input_schema,
                    coroutine=invoke,
                )
            )
        return tools


def parse_cls_search_records(output: object) -> tuple[dict[str, object], ...]:
    """Parse official MCP payloads and the bounded project-owned mapping."""
    if isinstance(output, Mapping):
        return _mapping_records(cast(Mapping[object, object], output))
    if not isinstance(output, Sequence) or isinstance(output, str | bytes):
        return ()
    records: list[dict[str, object]] = []
    for item in cast(Sequence[object], output):
        if not isinstance(item, Mapping):
            continue
        raw_item = cast(Mapping[object, object], item)
        raw_log = raw_item.get("LogJson")
        if isinstance(raw_log, str):
            parsed = _json_mapping(raw_log)
            if parsed is not None:
                records.append(parsed)
            continue
        text = raw_item.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            records.extend(_mapping_records(cast(Mapping[object, object], payload)))
        elif isinstance(payload, list):
            records.extend(parse_cls_search_records(cast(list[object], payload)))
    return tuple(records)


def _mapping_records(output: Mapping[object, object]) -> tuple[dict[str, object], ...]:
    raw_records = output.get("records")
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, str | bytes):
        return ()
    return tuple(
        _safe_mapping(cast(Mapping[object, object], record))
        for record in cast(Sequence[object], raw_records)
        if isinstance(record, Mapping)
    )


def _json_mapping(raw: str) -> dict[str, object] | None:
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    return _safe_mapping(cast(Mapping[object, object], payload))


def _safe_mapping(raw: Mapping[object, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str)
        and (value is None or isinstance(value, str | int | float | bool))
    }


def _validate_search_arguments(
    arguments: Mapping[str, object],
    scope: LiveClsScope,
) -> None:
    from_ms = arguments.get("From")
    to_ms = arguments.get("To")
    limit = arguments.get("Limit")
    query = arguments.get("Query")
    terms = (
        f'run_id:"{scope.run_id}"',
        f'scenario_id:"{scope.scenario_id}"',
        f'incident_id:"{scope.incident_id}"',
    )
    valid = (
        arguments.get("Region") == scope.region
        and arguments.get("TopicId") == scope.topic_id
        and isinstance(from_ms, int)
        and not isinstance(from_ms, bool)
        and isinstance(to_ms, int)
        and not isinstance(to_ms, bool)
        and scope.from_ms <= from_ms < to_ms <= scope.to_ms
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and 1 <= limit <= 100
        and isinstance(query, str)
        and all(term in query for term in terms)
    )
    if not valid:
        raise McpClientError("CLS Live query scope is invalid.")


def _matches_scope(record: Mapping[str, object], scope: LiveClsScope) -> bool:
    return (
        record.get("run_id") == scope.run_id
        and record.get("scenario_id") == scope.scenario_id
        and record.get("incident_id") == scope.incident_id
    )


def _cls_evidence_id(scenario_id: str) -> str:
    identifiers = {
        "APY-LIVE-PG-LOCK-001": "cls-live-request-timeout",
        "APY-LIVE-PG-DEADLOCK-001": "cls-live-database-deadlock",
        "APY-LIVE-REDIS-MAXCLIENTS-001": "cls-live-redis-connection-rejected",
        "APY-LIVE-NGINX-TIMEOUT-001": "cls-live-nginx-upstream-timeout",
        "APY-LIVE-ORDER-POOL-LEAK-001": "cls-order-connection-lifecycle",
    }
    try:
        return identifiers[scenario_id]
    except KeyError as exc:
        raise McpClientError("CLS Live scenario is not supported.") from exc
