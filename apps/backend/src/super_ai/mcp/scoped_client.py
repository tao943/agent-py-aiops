"""Fail-closed composition for Task-scoped read-only MCP tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from super_ai.mcp.cached_client import McpClient
from super_ai.mcp_client import McpClientError, McpToolDefinition


class TrustedToolCapabilityLike(Protocol):
    @property
    def tool_name(self) -> str: ...

    @property
    def read_only(self) -> bool: ...

    @property
    def allowed_server_names(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class ScopedMcpSource:
    client: McpClient
    allowed_tools: frozenset[str]


class ScopedCompositeMcpClient:
    """Expose only explicitly routed tools whose provenance is code-trusted."""

    def __init__(
        self,
        sources: Sequence[ScopedMcpSource],
        *,
        trusted_tool_capabilities: Mapping[str, TrustedToolCapabilityLike],
    ) -> None:
        self._sources = tuple(sources)
        self._trusted_tool_capabilities = trusted_tool_capabilities

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        definitions: list[McpToolDefinition] = []
        names: set[str] = set()
        for source in self._sources:
            for definition in await source.client.discover_tools():
                if definition.name not in source.allowed_tools:
                    continue
                capability = self._trusted_tool_capabilities.get(definition.name)
                if (
                    capability is None
                    or capability.tool_name != definition.name
                    or capability.read_only is not True
                    or definition.server_name not in capability.allowed_server_names
                ):
                    raise McpClientError(f"MCP tool provenance is not trusted: {definition.name}")
                if definition.name in names:
                    raise McpClientError(f"Duplicate MCP tool name: {definition.name}")
                names.add(definition.name)
                definitions.append(definition)
        return tuple(definitions)

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        matches: list[McpClient] = []
        for source in self._sources:
            if name not in source.allowed_tools:
                continue
            definitions = await source.client.discover_tools()
            for definition in definitions:
                if definition.name != name:
                    continue
                capability = self._trusted_tool_capabilities.get(name)
                if (
                    capability is None
                    or capability.tool_name != name
                    or capability.read_only is not True
                    or definition.server_name not in capability.allowed_server_names
                ):
                    raise McpClientError(f"MCP tool provenance is not trusted: {name}")
                matches.append(source.client)
        if len(matches) != 1:
            raise McpClientError(f"MCP tool is not available in this Task scope: {name}")
        return await matches[0].call_tool(name, arguments)

    async def get_langchain_tools(self) -> list[Any]:
        from langchain_core.tools import StructuredTool

        tools: list[Any] = []
        for definition in await self.discover_tools():

            async def invoke(
                _tool_name: str = definition.name,
                **arguments: object,
            ) -> object:
                return await self.call_tool(_tool_name, arguments)

            tools.append(
                StructuredTool(
                    name=definition.name,
                    description=definition.description,
                    args_schema=definition.input_schema,
                    coroutine=invoke,
                )
            )
        return tools
