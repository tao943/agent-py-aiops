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

_INVESTIGATOR_ORDER: tuple[InvestigatorType, ...] = (
    "knowledge",
    "runtime",
    "log",
    "change",
)
_PARALLEL_INVESTIGATOR_ORDER: tuple[InvestigatorType, ...] = ("runtime", "log")
_PRIVATE_ROUTING_TOKENS = (
    "ground_truth",
    "oracle",
    "primary_cause",
    "scorerules",
    "scenarioid",
    "runid",
)


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


@dataclass(frozen=True, slots=True)
class InvestigationRouterPolicy:
    version: str = "investigation-router-v1"
    escalation_watch_threshold: int = 4
    multi_agent_threshold: int = 6
    single_agent_max_initial_steps: int = 2
    maximum_investigation_waves: int = 2
    aggregation_reserve_ms: int = 5_000
    investigator_deadline_ms: int = 30_000
    mandatory_model_call_reserve: int = 2
    maximum_optional_model_calls_per_investigator: int = 1
    multi_agent_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Investigation router policy requires a version.")
        if not 0 <= self.escalation_watch_threshold < self.multi_agent_threshold:
            raise ValueError("Investigation router thresholds are invalid.")
        positive = (
            self.single_agent_max_initial_steps,
            self.maximum_investigation_waves,
            self.aggregation_reserve_ms,
            self.investigator_deadline_ms,
            self.mandatory_model_call_reserve,
            self.maximum_optional_model_calls_per_investigator,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Investigation router policy values must be positive.")


@dataclass(frozen=True, slots=True)
class InvestigationRoutingInput:
    required_domains: frozenset[InvestigatorType]
    unresolved_hypothesis_count: int
    causal_component_count: int
    missing_causal_roles: frozenset[str]
    high_quality_conflict: bool
    severity: str
    trusted_pattern_matched: bool
    decision_ready: bool
    valid_tool_calls_without_gain: int
    knowledge_hit: bool
    remaining_time_ms: int
    remaining_model_calls: int
    completed_dispatch_keys: frozenset[str]
    evidence_snapshot_hash: str
    wave: int

    def __post_init__(self) -> None:
        if not self.required_domains <= set(_INVESTIGATOR_ORDER):
            raise ValueError("Investigation routing input has an invalid required domain.")
        if not self.missing_causal_roles <= {"trigger", "mechanism", "impact"}:
            raise ValueError("Investigation routing input has an invalid causal role.")
        counts = (
            self.unresolved_hypothesis_count,
            self.causal_component_count,
            self.valid_tool_calls_without_gain,
            self.remaining_time_ms,
            self.remaining_model_calls,
            self.wave,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Investigation routing counts cannot be negative.")
        if (
            len(self.evidence_snapshot_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.evidence_snapshot_hash)
        ):
            raise ValueError("Investigation routing requires a hexadecimal snapshot hash.")
        public_strings = (
            self.severity,
            self.evidence_snapshot_hash,
            *sorted(self.completed_dispatch_keys),
        )
        if any(
            token in value.casefold()
            for value in public_strings
            for token in _PRIVATE_ROUTING_TOKENS
        ):
            raise ValueError("Investigation routing input contains evaluator-private data.")


@dataclass(frozen=True, slots=True)
class InvestigationRoute:
    strategy: InvestigationStrategy
    score: int
    escalation_watch: bool
    selected_investigators: tuple[InvestigatorType, ...]
    rejected_investigators: Mapping[InvestigatorType, str]
    reason_codes: tuple[str, ...]
    policy_version: str


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


def route_investigation(
    routing_input: InvestigationRoutingInput,
    *,
    capabilities: Mapping[InvestigatorType, InvestigatorCapability],
    policy: InvestigationRouterPolicy,
    mode: StrategyMode = "auto",
) -> InvestigationRoute:
    """Choose one auditable strategy from bounded public routing features."""
    if mode not in {"auto", "single", "multi"}:
        raise ValueError("Unknown investigation strategy mode.")
    score, score_reasons = _routing_score(routing_input)
    rejected = _base_rejected_investigators(capabilities)

    if routing_input.trusted_pattern_matched or routing_input.decision_ready:
        reason = (
            "trusted_pattern_matched"
            if routing_input.trusted_pattern_matched
            else "decision_ready"
        )
        for domain in _PARALLEL_INVESTIGATOR_ORDER:
            rejected[domain] = "deterministic_fast_path"
        return _route(
            "deterministic_fast_path",
            score=score,
            escalation_watch=False,
            selected=(),
            rejected=rejected,
            reasons=(*score_reasons, reason),
            policy=policy,
        )

    candidates: list[InvestigatorType] = []
    gate_reasons: list[str] = []
    completed_domain_seen = False
    unavailable_domain_seen = False
    for domain in _PARALLEL_INVESTIGATOR_ORDER:
        capability = capabilities.get(domain)
        if domain not in routing_input.required_domains:
            rejected[domain] = "not_required"
            continue
        completion_key = f"{domain}:{routing_input.evidence_snapshot_hash}"
        if completion_key in routing_input.completed_dispatch_keys:
            rejected[domain] = "already_completed"
            completed_domain_seen = True
            continue
        if capability is None or not capability.available:
            rejected[domain] = (
                capability.reason_code
                if capability is not None and capability.reason_code is not None
                else "source_unavailable"
            )
            unavailable_domain_seen = True
            continue
        candidates.append(domain)

    if not policy.multi_agent_enabled:
        gate_reasons.append("multi_agent_disabled")
    if routing_input.wave >= policy.maximum_investigation_waves:
        gate_reasons.append("maximum_investigation_waves_reached")
    minimum_time_ms = policy.investigator_deadline_ms + policy.aggregation_reserve_ms
    if routing_input.remaining_time_ms < minimum_time_ms:
        gate_reasons.append("insufficient_time_budget")
    required_model_calls = policy.mandatory_model_call_reserve + (
        len(candidates) * policy.maximum_optional_model_calls_per_investigator
    )
    if routing_input.remaining_model_calls < required_model_calls:
        gate_reasons.append("insufficient_model_budget")
    if completed_domain_seen:
        gate_reasons.append("dispatch_snapshot_already_completed")
    if unavailable_domain_seen:
        gate_reasons.append("capability_unavailable")
    if len(candidates) < 2:
        gate_reasons.append("insufficient_parallel_sources")

    should_attempt_multi = mode == "multi" or (
        mode == "auto" and score >= policy.multi_agent_threshold
    )
    if mode == "single":
        gate_reasons.append("forced_single_strategy")
        should_attempt_multi = False
    elif mode == "auto" and score < policy.multi_agent_threshold:
        gate_reasons.append("below_multi_agent_threshold")

    if should_attempt_multi and not gate_reasons:
        return _route(
            "multi_agent",
            score=score,
            escalation_watch=False,
            selected=tuple(candidates),
            rejected=rejected,
            reasons=score_reasons,
            policy=policy,
        )

    for domain in candidates:
        rejected[domain] = "single_agent_selected"
    escalation_watch = (
        mode == "auto"
        and policy.escalation_watch_threshold
        <= score
        < policy.multi_agent_threshold
        and not gate_reasons[:-1]
    )
    reasons: tuple[str, ...] = (*score_reasons, *gate_reasons)
    if escalation_watch:
        reasons = (*reasons, "escalation_watch")
    return _route(
        "single_agent",
        score=score,
        escalation_watch=escalation_watch,
        selected=(),
        rejected=rejected,
        reasons=reasons,
        policy=policy,
    )


def _routing_score(
    routing_input: InvestigationRoutingInput,
) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    domain_count = len(routing_input.required_domains)
    if domain_count >= 3:
        score += 3
        reasons.append("three_evidence_domains_required")
    elif domain_count == 2:
        score += 1
        reasons.append("two_evidence_domains_required")
    if routing_input.causal_component_count >= 2:
        score += 2
        reasons.append("cross_component_investigation")
    if routing_input.unresolved_hypothesis_count >= 3:
        score += 1
        reasons.append("root_cause_ambiguity")
    if routing_input.high_quality_conflict:
        score += 3
        reasons.append("high_quality_evidence_conflict")
    if routing_input.severity.casefold() in {"p0", "p1", "critical"}:
        score += 2
        reasons.append("high_severity_incident")
    if len(routing_input.missing_causal_roles) >= 2:
        score += 2
        reasons.append("multiple_causal_roles_missing")
    if routing_input.valid_tool_calls_without_gain >= 2:
        score += 3
        reasons.append("investigation_stagnated")
    if not routing_input.knowledge_hit:
        score += 1
        reasons.append("knowledge_match_absent")
    return score, tuple(reasons)


def _base_rejected_investigators(
    capabilities: Mapping[InvestigatorType, InvestigatorCapability],
) -> dict[InvestigatorType, str]:
    change = capabilities.get("change")
    return {
        "knowledge": "already_completed",
        "change": (
            change.reason_code
            if change is not None and change.reason_code is not None
            else "deployment_change_source_not_configured"
        ),
    }


def _route(
    strategy: InvestigationStrategy,
    *,
    score: int,
    escalation_watch: bool,
    selected: tuple[InvestigatorType, ...],
    rejected: Mapping[InvestigatorType, str],
    reasons: tuple[str, ...],
    policy: InvestigationRouterPolicy,
) -> InvestigationRoute:
    return InvestigationRoute(
        strategy=strategy,
        score=score,
        escalation_watch=escalation_watch,
        selected_investigators=selected,
        rejected_investigators=MappingProxyType(dict(rejected)),
        reason_codes=tuple(dict.fromkeys(reasons)),
        policy_version=policy.version,
    )


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
