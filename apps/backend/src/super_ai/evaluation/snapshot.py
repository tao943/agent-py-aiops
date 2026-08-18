"""Deterministic, answer-free MCP replay for Snapshot benchmark scenarios."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from super_ai.mcp.tool_arguments import ToolArgumentContract
from super_ai.mcp_client import McpClientError, McpToolDefinition


@dataclass(frozen=True, slots=True)
class SnapshotToolObservation:
    """One ordered observation returned by the frozen tool runtime."""

    sequence: int
    tool_name: str
    arguments: dict[str, object]
    evidence_id: str
    result: dict[str, object]


@dataclass(frozen=True, slots=True)
class _SnapshotCall:
    evidence_id: str
    result: dict[str, object]


class SnapshotMcpClient:
    """Replay only explicitly registered tool calls from one in-memory snapshot."""

    def __init__(
        self,
        *,
        definitions: Sequence[McpToolDefinition],
        calls: Mapping[tuple[str, str], _SnapshotCall],
        registered_arguments: Mapping[str, Sequence[Mapping[str, object]]],
    ) -> None:
        self._definitions = tuple(copy.deepcopy(definitions))
        self._calls = dict(copy.deepcopy(calls))
        self._registered_arguments = {
            name: tuple(copy.deepcopy(dict(arguments)) for arguments in calls_for_tool)
            for name, calls_for_tool in registered_arguments.items()
        }
        self._observations: list[SnapshotToolObservation] = []

    @classmethod
    def from_yaml(cls, path: Path) -> SnapshotMcpClient:
        """Parse and validate a snapshot once; runtime calls never reopen the file."""
        payload = _load_yaml_mapping(path)
        definitions: list[McpToolDefinition] = []
        calls: dict[tuple[str, str], _SnapshotCall] = {}
        registered_arguments: dict[str, list[dict[str, object]]] = {}
        names: set[str] = set()
        for raw_tool in _required_sequence(payload, "tools"):
            tool = _as_mapping(raw_tool, "snapshot tool")
            name = _required_str(tool, "name")
            if name in names:
                raise ValueError(f"Duplicate snapshot tool name: {name}.")
            names.add(name)
            if _normalize_tool_name(name) == "readgroundtruth":
                continue
            input_schema = dict(_required_mapping(tool, "input_schema"))
            try:
                validator_class = validator_for(input_schema)
                validator_class.check_schema(input_schema)
                validator = validator_class(input_schema)
            except SchemaError as exc:
                raise ValueError(
                    f"Snapshot tool {name} declares an invalid input schema."
                ) from exc
            definitions.append(
                McpToolDefinition(
                    name=name,
                    description=_required_str(tool, "description"),
                    input_schema=cast(dict[str, Any], input_schema),
                    server_name="snapshot",
                )
            )
            raw_calls = _required_sequence(tool, "calls")
            if not raw_calls:
                raise ValueError(f"Snapshot tool {name} must register at least one call.")
            registered_arguments[name] = []
            for raw_call in raw_calls:
                call = _as_mapping(raw_call, f"{name} call")
                arguments = dict(_required_mapping(call, "arguments"))
                try:
                    validator.validate(cast(Any, arguments))
                except ValidationError as exc:
                    raise ValueError(
                        f"Snapshot call for tool {name} violates its input schema."
                    ) from exc
                key = (name, _canonical_arguments(arguments))
                if key in calls:
                    raise ValueError(f"Duplicate snapshot call for tool {name}.")
                calls[key] = _SnapshotCall(
                    evidence_id=_required_str(call, "evidence_id"),
                    result=dict(_required_mapping(call, "result")),
                )
                registered_arguments[name].append(copy.deepcopy(arguments))
        if not definitions:
            raise ValueError("Snapshot must define at least one tool.")
        return cls(
            definitions=definitions,
            calls=calls,
            registered_arguments=registered_arguments,
        )

    @property
    def tool_argument_contracts(self) -> Mapping[str, ToolArgumentContract]:
        """Return answer-free exact call contracts as defensive immutable values."""
        return MappingProxyType(
            {
                name: ToolArgumentContract(
                    tool_name=name,
                    registered_calls=tuple(copy.deepcopy(calls_for_tool)),
                )
                for name, calls_for_tool in self._registered_arguments.items()
            }
        )

    @property
    def observations(self) -> tuple[SnapshotToolObservation, ...]:
        """Return defensive copies of the ordered audit observations."""
        return tuple(copy.deepcopy(self._observations))

    async def discover_tools(self) -> list[McpToolDefinition]:
        """Return defensive copies of the frozen tool definitions."""
        return list(copy.deepcopy(self._definitions))

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        """Replay an exact registered call or fail closed."""
        if not any(definition.name == name for definition in self._definitions):
            raise McpClientError(f"Snapshot MCP tool is not available: {name}.")
        key = (name, _canonical_arguments(arguments))
        call = self._calls.get(key)
        if call is None:
            raise McpClientError(f"Snapshot arguments are not registered for tool {name}.")
        result = copy.deepcopy(call.result)
        result["benchmarkEvidenceId"] = call.evidence_id
        self._observations.append(
            SnapshotToolObservation(
                sequence=len(self._observations) + 1,
                tool_name=name,
                arguments=copy.deepcopy(dict(arguments)),
                evidence_id=call.evidence_id,
                result=copy.deepcopy(result),
            )
        )
        return result

    async def get_langchain_tools(self) -> list[Any]:
        """Build executable StructuredTools from frozen definitions."""
        from langchain_core.tools import StructuredTool

        tools: list[Any] = []
        for definition in self._definitions:

            async def invoke(
                _tool_name: str = definition.name,
                **arguments: object,
            ) -> object:
                return await self.call_tool(_tool_name, arguments)

            tools.append(
                StructuredTool(
                    name=definition.name,
                    description=definition.description,
                    args_schema=copy.deepcopy(definition.input_schema),
                    coroutine=invoke,
                )
            )
        return tools


def _canonical_arguments(arguments: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(arguments),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise McpClientError("Snapshot tool arguments must be JSON serializable.") from exc


def _load_yaml_mapping(path: Path) -> Mapping[str, object]:
    try:
        parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Snapshot file does not exist: {path}.") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Snapshot YAML is invalid: {path}.") from exc
    return _as_mapping(parsed, path.name)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    return cast(Mapping[str, object], mapping)


def _required_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    if key not in payload:
        raise ValueError(f"Snapshot field '{key}' is required.")
    return _as_mapping(payload[key], key)


def _required_sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = payload.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"Snapshot field '{key}' must be a sequence.")
    return cast(Sequence[object], value)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Snapshot field '{key}' must be a non-empty string.")
    return value.strip()


def _normalize_tool_name(name: str) -> str:
    return "".join(character for character in name.lower() if character.isalnum())
