"""Runtime-owned MCP tool arguments with exact, auditable call contracts."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from super_ai.mcp_client import McpToolDefinition

ToolArgumentContractErrorCode = Literal[
    "unknown_field",
    "ambiguous_variant",
    "invalid_variant",
    "schema_mismatch",
]


class ToolArgumentContractError(ValueError):
    """Bounded rejection that never serializes model-supplied argument values."""

    def __init__(
        self,
        *,
        code: ToolArgumentContractErrorCode,
        tool_name: str,
    ) -> None:
        super().__init__(f"Tool arguments violate the runtime contract for {tool_name} ({code}).")
        self.code = code
        self.tool_name = tool_name


@dataclass(frozen=True, slots=True)
class ToolArgumentContract:
    """Exact registered argument mappings for one request-scoped MCP tool."""

    tool_name: str
    registered_calls: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not self.tool_name.strip() or not self.registered_calls:
            raise ValueError("Tool argument contracts require a name and registered calls.")
        copied: list[Mapping[str, object]] = []
        canonical_calls: set[str] = set()
        for call in self.registered_calls:
            arguments = copy.deepcopy(dict(call))
            canonical = _canonical_arguments(arguments)
            if canonical in canonical_calls:
                raise ValueError(
                    f"Tool argument contract contains a duplicate call for {self.tool_name}."
                )
            canonical_calls.add(canonical)
            copied.append(MappingProxyType(arguments))
        object.__setattr__(self, "registered_calls", tuple(copied))

    @property
    def fixed_arguments(self) -> dict[str, object]:
        """Return fields whose values are identical in every registered call."""
        first = dict(self.registered_calls[0])
        return {
            key: copy.deepcopy(value)
            for key, value in first.items()
            if all(key in call and call[key] == value for call in self.registered_calls)
        }


@runtime_checkable
class ToolArgumentContractProvider(Protocol):
    """Optional structural capability exposed by request-scoped MCP clients."""

    @property
    def tool_argument_contracts(self) -> Mapping[str, ToolArgumentContract]:
        """Return contracts without evidence results or oracle data."""
        ...


def normalize_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, object],
    contracts: Mapping[str, ToolArgumentContract],
) -> dict[str, object]:
    """Resolve model arguments to one exact registered call when a contract exists."""
    contract = contracts.get(tool_name)
    if contract is None:
        return copy.deepcopy(dict(arguments))

    supplied = dict(arguments)
    allowed_keys = {
        key for registered in contract.registered_calls for key in registered
    }
    if set(supplied) - allowed_keys:
        raise ToolArgumentContractError(code="unknown_field", tool_name=tool_name)

    fixed_keys = set(contract.fixed_arguments)
    supplied_variants = {
        key: value for key, value in supplied.items() if key not in fixed_keys
    }
    matching = [
        registered
        for registered in contract.registered_calls
        if all(
            key in registered and registered[key] == value
            for key, value in supplied_variants.items()
        )
    ]
    if len(matching) > 1:
        raise ToolArgumentContractError(code="ambiguous_variant", tool_name=tool_name)
    if not matching:
        raise ToolArgumentContractError(code="invalid_variant", tool_name=tool_name)
    return copy.deepcopy(dict(matching[0]))


def constrain_tool_definitions(
    definitions: Sequence[McpToolDefinition],
    contracts: Mapping[str, ToolArgumentContract],
) -> list[McpToolDefinition]:
    """Compose discovered schemas with exact request-scoped registered calls."""
    constrained: list[McpToolDefinition] = []
    for definition in definitions:
        contract = contracts.get(definition.name)
        input_schema = copy.deepcopy(definition.input_schema)
        if contract is not None:
            exact_call_schema = {
                "oneOf": [
                    {
                        "type": "object",
                        "required": sorted(call),
                        "additionalProperties": False,
                        "properties": {
                            key: {"const": copy.deepcopy(value)}
                            for key, value in call.items()
                        },
                    }
                    for call in contract.registered_calls
                ]
            }
            input_schema = {"allOf": [input_schema, exact_call_schema]}
        constrained.append(
            McpToolDefinition(
                name=definition.name,
                description=definition.description,
                input_schema=input_schema,
                server_name=definition.server_name,
            )
        )
    return constrained


def tool_step_fingerprint(tool_name: str, arguments: Mapping[str, object]) -> str:
    """Return the canonical duplicate key for one effective tool call."""
    return json.dumps(
        {"tool": tool_name, "arguments": dict(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_arguments(arguments: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Tool argument contract values must be JSON serializable.") from exc
