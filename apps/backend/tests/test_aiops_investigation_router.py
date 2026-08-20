from __future__ import annotations

from typing import Any, cast

import pytest

from super_ai.aiops.investigation import (
    TrustedToolCapability,
    build_investigator_capabilities,
    normalize_plan_source_domains,
    source_domain_for_tool,
)
from super_ai.mcp_client import McpToolDefinition


def _tool(name: str, *, server: str = "test") -> McpToolDefinition:
    return McpToolDefinition(name, f"{name} description", {"type": "object"}, server)


def _trusted(
    name: str,
    domain: str,
    *,
    maximum_calls: int = 2,
    servers: frozenset[str] = frozenset({"test"}),
) -> TrustedToolCapability:
    return TrustedToolCapability(
        tool_name=name,
        source_domain=cast(Any, domain),
        read_only=True,
        maximum_calls_per_dispatch=maximum_calls,
        allowed_server_names=servers,
    )


def test_capability_registry_exposes_only_discovered_explicit_read_only_tools() -> None:
    discovered = (
        _tool("InspectPostgresSessions"),
        _tool("SearchLog", server="cls"),
        _tool("UserDefinedReadDatabase"),
    )
    trusted = {
        "InspectPostgresSessions": _trusted("InspectPostgresSessions", "runtime"),
        "SearchLog": _trusted(
            "SearchLog", "log", servers=frozenset({"cls"})
        ),
    }

    capabilities = build_investigator_capabilities(
        discovered_tools=discovered,
        trusted_tool_capabilities=trusted,
        tool_policies={},
        retrieval_available=True,
        cls_available=True,
    )

    assert capabilities["knowledge"].allowed_tools == frozenset({"knowledge_retrieval"})
    assert capabilities["runtime"].allowed_tools == frozenset(
        {"InspectPostgresSessions"}
    )
    assert capabilities["log"].allowed_tools == frozenset({"SearchLog"})
    assert source_domain_for_tool("UserDefinedReadDatabase", capabilities) is None
    assert capabilities["change"].available is False
    assert (
        capabilities["change"].reason_code
        == "deployment_change_source_not_configured"
    )


def test_capability_registry_fails_closed_for_policy_tools_and_missing_sources() -> None:
    discovered = (_tool("ProposeNginxTimeoutMitigation"), _tool("SearchLog"))
    trusted = {
        "ProposeNginxTimeoutMitigation": _trusted(
            "ProposeNginxTimeoutMitigation", "runtime"
        ),
        "SearchLog": _trusted("SearchLog", "log"),
    }

    capabilities = build_investigator_capabilities(
        discovered_tools=discovered,
        trusted_tool_capabilities=trusted,
        tool_policies={"ProposeNginxTimeoutMitigation": "proposal_only"},
        retrieval_available=False,
        cls_available=False,
    )

    assert capabilities["runtime"].available is False
    assert capabilities["runtime"].allowed_tools == frozenset()
    assert capabilities["knowledge"].reason_code == "knowledge_source_not_configured"
    assert capabilities["log"].reason_code == "log_source_not_configured"


def test_trusted_capability_rejects_non_read_only_or_invalid_limits() -> None:
    with pytest.raises(ValueError, match="read-only"):
        TrustedToolCapability(
            tool_name="DangerousTool",
            source_domain="runtime",
            read_only=cast(Any, False),
            maximum_calls_per_dispatch=1,
            allowed_server_names=frozenset({"test"}),
        )
    with pytest.raises(ValueError, match="call limit"):
        TrustedToolCapability(
            tool_name="InspectPostgres",
            source_domain="runtime",
            read_only=True,
            maximum_calls_per_dispatch=0,
            allowed_server_names=frozenset({"test"}),
        )


def test_capability_registry_rejects_a_trusted_name_from_an_untrusted_server() -> None:
    capabilities = build_investigator_capabilities(
        discovered_tools=(_tool("InspectPostgresSessions", server="user-mcp"),),
        trusted_tool_capabilities={
            "InspectPostgresSessions": _trusted(
                "InspectPostgresSessions",
                "runtime",
                servers=frozenset({"live-eval-local"}),
            )
        },
        tool_policies={},
        retrieval_available=False,
        cls_available=False,
    )

    assert capabilities["runtime"].available is False
    assert source_domain_for_tool("InspectPostgresSessions", capabilities) is None


def test_normalize_plan_domains_uses_registry_without_mutating_model_output() -> None:
    capabilities = build_investigator_capabilities(
        discovered_tools=(
            _tool("InspectPostgresSessions"),
            _tool("SearchLog", server="cls"),
        ),
        trusted_tool_capabilities={
            "InspectPostgresSessions": _trusted(
                "InspectPostgresSessions", "runtime"
            ),
            "SearchLog": _trusted(
                "SearchLog", "log", servers=frozenset({"cls"})
            ),
        },
        tool_policies={},
        retrieval_available=True,
        cls_available=True,
    )
    model_plan: list[dict[str, object]] = [
        {
            "id": "runtime-step",
            "tool": "InspectPostgresSessions",
            "sourceDomain": "change",
        },
        {"id": "log-step", "tool": "SearchLog", "sourceDomain": "runtime"},
        {"id": "unknown-step", "tool": "UserDefinedReadDatabase", "sourceDomain": "log"},
        {"id": "knowledge-step", "tool": "knowledge_retrieval"},
    ]

    normalized = normalize_plan_source_domains(model_plan, capabilities)

    assert model_plan[0]["sourceDomain"] == "change"
    assert normalized[0]["sourceDomain"] == "runtime"
    assert normalized[1]["sourceDomain"] == "log"
    assert "sourceDomain" not in normalized[2]
    assert normalized[2]["sourceDomainStatus"] == "unmapped"
    assert normalized[3]["sourceDomain"] == "knowledge"
    assert all(
        item["sourceDomainStatus"] == "trusted_registry"
        for item in (normalized[0], normalized[1], normalized[3])
    )


def test_source_domain_requires_an_available_capability() -> None:
    capabilities = build_investigator_capabilities(
        discovered_tools=(_tool("SearchLog"),),
        trusted_tool_capabilities={"SearchLog": _trusted("SearchLog", "log")},
        tool_policies={},
        retrieval_available=False,
        cls_available=False,
    )

    assert source_domain_for_tool("SearchLog", capabilities) is None
    assert source_domain_for_tool("knowledge_retrieval", capabilities) is None
