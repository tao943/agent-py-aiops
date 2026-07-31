"""Owner-scoped cache decorator for MCP tool discovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, cast
from urllib.parse import urlsplit

from super_ai.mcp_client import McpToolDefinition
from super_ai.redis_runtime.cache import RuntimeCache, build_cache_key

DEFAULT_MCP_DISCOVERY_CACHE_TTL_SECONDS = 300


class McpClient(Protocol):
    """Minimal boundary shared by MCP client implementations and decorators."""

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        """Discover tool definitions exposed by the configured MCP server."""
        ...

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        """Execute an MCP tool without cache involvement."""
        ...


class CachedMcpClient:
    """Cache validated discovery DTOs while always forwarding tool execution."""

    def __init__(
        self,
        inner: McpClient,
        *,
        cache: RuntimeCache,
        owner_id: str,
        connection_id: str,
        connection_version: str,
        ttl_seconds: int = DEFAULT_MCP_DISCOVERY_CACHE_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        self._inner = inner
        self._cache = cache
        self._key = build_cache_key(
            purpose="mcp-discovery",
            owner_id=owner_id,
            version=connection_version,
            input_value={"connectionId": connection_id},
        )
        self._ttl_seconds = ttl_seconds

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        lookup = await self._cache.get_json(self._key)
        if lookup.state == "hit" and lookup.value is not None:
            cached = _tool_definitions_from_payload(lookup.value)
            if cached is not None:
                return cached
            await self._cache.delete(self._key)

        tools = await self._inner.discover_tools()
        await self._cache.set_json(
            self._key,
            _tool_definitions_payload(tools),
            ttl_seconds=self._ttl_seconds,
        )
        return tools

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        return await self._inner.call_tool(name, arguments)


def connection_cache_version(
    *, updated_at: datetime, behavioral_config: Mapping[str, object]
) -> str:
    """Hash canonical non-secret connection behavior with its database revision."""
    payload = {
        "updatedAt": updated_at.isoformat(),
        "behavior": _behavioral_config(behavioral_config),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _behavioral_config(config: Mapping[str, object]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key in ("transport", "enabled", "timeoutSeconds", "retries"):
        if key in config:
            value[key] = config[key]
    raw_url = config.get("url")
    if isinstance(raw_url, str):
        value["url"] = _safe_endpoint_url(raw_url)
    return value


def _safe_endpoint_url(raw_url: str) -> str:
    """Retain only endpoint behavior; drop userinfo, query strings, and fragments."""
    parsed = urlsplit(raw_url)
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = hostname.lower()
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}{parsed.path}"


def _tool_definitions_payload(tools: Sequence[McpToolDefinition]) -> dict[str, object]:
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "serverName": tool.server_name,
            }
            for tool in tools
        ]
    }


def _tool_definitions_from_payload(payload: Mapping[str, object]) -> list[McpToolDefinition] | None:
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, list):
        return None
    definitions: list[McpToolDefinition] = []
    names: set[str] = set()
    for raw_item in cast(list[object], raw_tools):
        if not isinstance(raw_item, Mapping):
            return None
        item = cast(Mapping[str, object], raw_item)
        name = item.get("name")
        description = item.get("description")
        input_schema = item.get("inputSchema")
        server_name = item.get("serverName")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(description, str)
            or not isinstance(input_schema, dict)
            or not isinstance(server_name, str)
            or not server_name
        ):
            return None
        names.add(name)
        definitions.append(
            McpToolDefinition(
                name=name,
                description=description,
                input_schema=cast(dict[str, object], input_schema),
                server_name=server_name,
            )
        )
    return definitions
