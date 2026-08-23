"""Strictly compile model-visible tools from an execution policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from langchain_core.tools import StructuredTool

from super_ai.chat.execution_policy import ChatExecutionPolicy


class RequiredToolUnavailable(RuntimeError):
    """A policy-required tool is absent from the runtime registry."""


@dataclass(frozen=True, slots=True)
class CompiledToolCatalog:
    tools: tuple[StructuredTool, ...]
    names: tuple[str, ...]
    catalog_version: str


class ToolCatalog:
    """Apply the policy allowlist and reject silently degraded required capabilities."""

    def compile(
        self,
        *,
        policy: ChatExecutionPolicy,
        registry: Mapping[str, StructuredTool],
    ) -> CompiledToolCatalog:
        missing = tuple(sorted(policy.required_tools - registry.keys()))
        if missing:
            raise RequiredToolUnavailable(
                f"Required chat tools are unavailable: {', '.join(missing)}"
            )

        names = tuple(sorted(policy.allowed_tools & registry.keys()))
        tools = tuple(registry[name] for name in names)
        version_input = json.dumps(
            {
                "mode": policy.mode,
                "capability": policy.required_capability,
                "names": names,
                "postcondition": policy.postcondition,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return CompiledToolCatalog(
            tools=tools,
            names=names,
            catalog_version=sha256(version_input.encode("utf-8")).hexdigest()[:16],
        )
