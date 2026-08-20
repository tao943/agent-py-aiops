"""Deterministic, fail-closed capability contracts for AIOps investigation routing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from super_ai.mcp_client import McpToolDefinition

InvestigatorType = Literal["knowledge", "runtime", "log", "change"]
InvestigationStrategy = Literal[
    "deterministic_fast_path", "single_agent", "multi_agent"
]
StrategyMode = Literal["auto", "single", "multi"]
SourceDomain = Literal["runtime", "log"]


@dataclass(frozen=True, slots=True)
class InvestigatorCapability:
    investigator_type: InvestigatorType
    available: bool
    allowed_tools: frozenset[str]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.available and not self.allowed_tools:
            raise ValueError("An available Investigator capability requires tools.")
        if self.available and self.reason_code is not None:
            raise ValueError("An available Investigator capability cannot have a reason code.")
        if not self.available and self.reason_code is None:
            raise ValueError("An unavailable Investigator capability requires a reason code.")


@dataclass(frozen=True, slots=True)
class TrustedToolCapability:
    tool_name: str
    source_domain: SourceDomain
    read_only: Literal[True]
    maximum_calls_per_dispatch: int
    allowed_server_names: frozenset[str]

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("A trusted tool capability requires a tool name.")
        if self.source_domain not in {"runtime", "log"}:
            raise ValueError("A trusted tool capability has an invalid source domain.")
        if self.read_only is not True:
            raise ValueError("Investigator tools must be explicitly read-only.")
        if self.maximum_calls_per_dispatch <= 0:
            raise ValueError("A trusted tool capability requires a positive call limit.")
        if not self.allowed_server_names or any(
            not name.strip() for name in self.allowed_server_names
        ):
            raise ValueError("A trusted tool capability requires trusted server names.")


_RUNTIME_SERVER_NAMES = frozenset(
    {
        "default",
        "live-eval-local",
        "nginx-timeout-live",
        "postgres-deadlock-live",
        "redis-maxclients-live",
        "snapshot",
    }
)
_LOG_SERVER_NAMES = frozenset({"cls"})


def _runtime(tool_name: str, maximum_calls: int = 3) -> TrustedToolCapability:
    return TrustedToolCapability(
        tool_name,
        "runtime",
        True,
        maximum_calls,
        _RUNTIME_SERVER_NAMES,
    )


def _log(tool_name: str, maximum_calls: int = 2) -> TrustedToolCapability:
    return TrustedToolCapability(
        tool_name,
        "log",
        True,
        maximum_calls,
        _LOG_SERVER_NAMES,
    )


_RUNTIME_TOOL_NAMES = (
    "GetDatabaseMetrics",
    "GetGatewayMetrics",
    "GetRedisConnectionMetrics",
    "GetServiceMetrics",
    "GetServiceTopology",
    "InspectClientRetryPolicy",
    "InspectContainer",
    "InspectDatabasePool",
    "InspectGatewayErrors",
    "InspectGatewayRequestTimeline",
    "InspectHostLimits",
    "InspectHttpAttempts",
    "InspectNginx",
    "InspectNginxRequestTimeline",
    "InspectPostgres",
    "InspectPostgresDeadlockAudit",
    "InspectPostgresErrors",
    "InspectPostgresLockGraph",
    "InspectPostgresSessions",
    "InspectPostgresTransactionResult",
    "InspectPostgresWaitGraph",
    "InspectRateLimitTimeline",
    "InspectRedis",
    "InspectRedisClientPool",
    "InspectRedisServer",
    "InspectRedisServerInfo",
    "InspectTrafficAndDependencyHealth",
    "InspectTransactionResourceOrder",
    "ListRedisClients",
    "ProbeLiveEvalUpstream",
    "ProbeUpstreamHealth",
    "QueryMetrics",
    "QueryTrace",
    "ReadNginxTimeoutSummary",
    "VerifyServiceHealth",
)

TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES: Mapping[str, TrustedToolCapability] = (
    MappingProxyType(
        {
            **{name: _runtime(name) for name in _RUNTIME_TOOL_NAMES},
            "SearchLog": _log("SearchLog"),
            "SearchLogs": _log("SearchLogs"),
        }
    )
)


def build_investigator_capabilities(
    *,
    discovered_tools: Sequence[McpToolDefinition],
    trusted_tool_capabilities: Mapping[str, TrustedToolCapability],
    tool_policies: Mapping[str, str],
    retrieval_available: bool,
    cls_available: bool,
) -> Mapping[InvestigatorType, InvestigatorCapability]:
    """Build source-scoped capabilities without inferring safety from discovery."""
    allowed_by_domain: dict[SourceDomain, set[str]] = {
        "runtime": set(),
        "log": set(),
    }
    for definition in sorted(
        discovered_tools, key=lambda item: (item.name, item.server_name)
    ):
        tool_name = definition.name
        trusted = trusted_tool_capabilities.get(tool_name)
        if trusted is None or trusted.read_only is not True or tool_name in tool_policies:
            continue
        if trusted.tool_name != tool_name:
            continue
        if definition.server_name not in trusted.allowed_server_names:
            continue
        if trusted.source_domain == "log" and not cls_available:
            continue
        allowed_by_domain[trusted.source_domain].add(tool_name)

    capabilities: dict[InvestigatorType, InvestigatorCapability] = {
        "knowledge": _capability(
            "knowledge",
            {"knowledge_retrieval"} if retrieval_available else set(),
            "knowledge_source_not_configured",
        ),
        "runtime": _capability(
            "runtime",
            allowed_by_domain["runtime"],
            "runtime_source_not_configured",
        ),
        "log": _capability(
            "log",
            allowed_by_domain["log"],
            "log_source_not_configured",
        ),
        "change": InvestigatorCapability(
            investigator_type="change",
            available=False,
            allowed_tools=frozenset(),
            reason_code="deployment_change_source_not_configured",
        ),
    }
    return MappingProxyType(capabilities)


def source_domain_for_tool(
    tool_name: str,
    capabilities: Mapping[InvestigatorType, InvestigatorCapability],
) -> InvestigatorType | None:
    """Return a domain only when its capability is currently available."""
    for domain in ("knowledge", "runtime", "log", "change"):
        capability = capabilities.get(domain)
        if (
            capability is not None
            and capability.available
            and tool_name in capability.allowed_tools
        ):
            return domain
    return None


def normalize_plan_source_domains(
    plan: Sequence[Mapping[str, object]],
    capabilities: Mapping[InvestigatorType, InvestigatorCapability],
) -> list[dict[str, object]]:
    """Replace model-provided domains with the code-owned capability result."""
    normalized: list[dict[str, object]] = []
    for source_step in plan:
        step = dict(source_step)
        domain = source_domain_for_tool(str(step.get("tool") or ""), capabilities)
        if domain is None:
            step.pop("sourceDomain", None)
            step["sourceDomainStatus"] = "unmapped"
        else:
            step["sourceDomain"] = domain
            step["sourceDomainStatus"] = "trusted_registry"
        normalized.append(step)
    return normalized


def _capability(
    investigator_type: InvestigatorType,
    allowed_tools: set[str],
    unavailable_reason: str,
) -> InvestigatorCapability:
    if allowed_tools:
        return InvestigatorCapability(
            investigator_type=investigator_type,
            available=True,
            allowed_tools=frozenset(allowed_tools),
        )
    return InvestigatorCapability(
        investigator_type=investigator_type,
        available=False,
        allowed_tools=frozenset(),
        reason_code=unavailable_reason,
    )
