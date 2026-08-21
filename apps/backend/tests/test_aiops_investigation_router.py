from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from super_ai.aiops.investigation import (
    TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    InvestigationRouterPolicy,
    InvestigationRoutingInput,
    TrustedToolCapability,
    build_investigator_capabilities,
    normalize_plan_source_domains,
    route_investigation,
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


def test_project_owned_postgres_live_server_is_a_trusted_runtime_source() -> None:
    capabilities = build_investigator_capabilities(
        discovered_tools=(
            _tool("InspectPostgresSessions", server="docker-live-postgres"),
            _tool("SearchLog", server="cls"),
        ),
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
        tool_policies={},
        retrieval_available=True,
        cls_available=True,
    )

    assert capabilities["runtime"].allowed_tools == frozenset(
        {"InspectPostgresSessions"}
    )
    assert capabilities["log"].allowed_tools == frozenset({"SearchLog"})


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


def _routing_capabilities(*, include_log: bool = True):  # type: ignore[no-untyped-def]
    discovered = [_tool("InspectPostgresSessions")]
    trusted = {
        "InspectPostgresSessions": _trusted("InspectPostgresSessions", "runtime")
    }
    if include_log:
        discovered.append(_tool("SearchLog"))
        trusted["SearchLog"] = _trusted("SearchLog", "log")
    return build_investigator_capabilities(
        discovered_tools=tuple(discovered),
        trusted_tool_capabilities=trusted,
        tool_policies={},
        retrieval_available=True,
        cls_available=True,
    )


def _routing_input(**overrides: object) -> InvestigationRoutingInput:
    values: dict[str, object] = {
        "required_domains": frozenset({"runtime", "log"}),
        "unresolved_hypothesis_count": 1,
        "causal_component_count": 1,
        "missing_causal_roles": frozenset(),
        "high_quality_conflict": False,
        "severity": "warning",
        "trusted_pattern_matched": False,
        "decision_ready": False,
        "valid_tool_calls_without_gain": 0,
        "knowledge_hit": True,
        "remaining_time_ms": 300_000,
        "remaining_model_calls": 8,
        "cross_source_temporal_chain_required": False,
        "single_evidence_domain_sufficient": False,
        "completed_dispatch_keys": frozenset(),
        "evidence_snapshot_hash": "a" * 64,
        "wave": 0,
    }
    values.update(overrides)
    return InvestigationRoutingInput(**cast(Any, values))


@pytest.mark.parametrize(
    ("routing_input", "expected_score", "release_mode"),
    (
        (_routing_input(causal_component_count=2), 5, "shadow"),
        (
            _routing_input(causal_component_count=2, knowledge_hit=False),
            5,
            "shadow",
        ),
        (
            _routing_input(
                required_domains=frozenset({"knowledge", "runtime", "log"}),
                causal_component_count=2,
                unresolved_hypothesis_count=3,
            ),
            7,
            "shadow",
        ),
    ),
)
def test_router_applies_thresholds(
    routing_input: InvestigationRoutingInput,
    expected_score: int,
    release_mode: str,
) -> None:
    route = route_investigation(
        routing_input,
        capabilities=_routing_capabilities(),
        policy=InvestigationRouterPolicy(multi_agent_enabled=True),
    )

    assert route.score == expected_score
    assert route.strategy == "single_agent"
    assert route.effective_strategy == "single_agent"
    assert route.requested_strategy == "auto"
    assert route.release_mode == release_mode
    assert route.selected_investigators == ()
    assert "shadow_multi_candidate" in route.reason_codes


@pytest.mark.parametrize(
    ("overrides", "expected_score", "reason_code"),
    (
        (
            {"required_domains": frozenset({"runtime", "log"})},
            3,
            "runtime_cls_required",
        ),
        (
            {
                "required_domains": frozenset(
                    {"knowledge", "runtime", "log"}
                )
            },
            3,
            "runtime_cls_required",
        ),
        ({"causal_component_count": 2}, 5, "component_mechanism_span"),
        ({"unresolved_hypothesis_count": 3}, 5, "three_public_candidates"),
        (
            {"cross_source_temporal_chain_required": True},
            5,
            "cross_source_temporal_chain_required",
        ),
        ({"decision_ready": True}, 0, "deterministic_decision_ready"),
        (
            {"single_evidence_domain_sufficient": True},
            0,
            "single_evidence_domain_sufficient",
        ),
        ({"remaining_time_ms": 34_999}, -1, "insufficient_deadline"),
        ({"remaining_model_calls": 5}, -1, "insufficient_model_budget"),
    ),
)
def test_router_scores_each_public_feature(
    overrides: dict[str, object], expected_score: int, reason_code: str
) -> None:
    route = route_investigation(
        _routing_input(**overrides),
        capabilities=_routing_capabilities(),
        policy=InvestigationRouterPolicy(multi_agent_enabled=True),
    )

    assert route.score == expected_score
    assert reason_code in route.reason_codes
    assert reason_code in route.matched_features
    assert "high_severity_incident" not in " ".join(route.reason_codes)


def test_score_five_is_shadow_candidate_and_forced_multi_is_explicit() -> None:
    routing_input = _routing_input(unresolved_hypothesis_count=3)
    policy = InvestigationRouterPolicy(multi_agent_enabled=True)

    shadow = route_investigation(
        routing_input,
        capabilities=_routing_capabilities(),
        policy=policy,
        mode="auto",
    )
    forced = route_investigation(
        routing_input,
        capabilities=_routing_capabilities(),
        policy=policy,
        mode="multi",
    )

    assert shadow.score == 5
    assert shadow.strategy == "single_agent"
    assert shadow.release_mode == "shadow"
    assert shadow.downgrade_reason == "shadow_multi_candidate"
    assert forced.strategy == "multi_agent"
    assert forced.release_mode == "forced_benchmark"
    assert forced.selected_investigators == ("runtime", "log")


def test_router_policy_uses_confirmed_specialist_budgets() -> None:
    policy = InvestigationRouterPolicy()

    assert policy.multi_agent_threshold == 5
    assert policy.maximum_optional_model_calls_per_investigator == 2
    assert policy.specialist_soft_timeout_ms == 120_000
    assert policy.specialist_hard_timeout_ms == 180_000
    assert policy.global_soft_timeout_ms == 240_000
    assert policy.global_hard_timeout_ms == 360_000


def test_router_hard_gates_override_a_high_score_and_forced_multi() -> None:
    high_score = _routing_input(
        required_domains=frozenset({"knowledge", "runtime", "log"}),
        causal_component_count=2,
        unresolved_hypothesis_count=3,
        high_quality_conflict=True,
    )
    policy = InvestigationRouterPolicy(multi_agent_enabled=True)

    cases = (
        (replace(high_score, trusted_pattern_matched=True), "trusted_pattern_matched"),
        (replace(high_score, decision_ready=True), "decision_ready"),
        (
            replace(
                high_score,
                completed_dispatch_keys=frozenset(
                    {f"runtime:{high_score.evidence_snapshot_hash}"}
                ),
            ),
            "insufficient_parallel_sources",
        ),
        (replace(high_score, remaining_time_ms=124_999), "insufficient_deadline"),
        (replace(high_score, remaining_model_calls=3), "insufficient_model_budget"),
        (replace(high_score, wave=2), "maximum_investigation_waves_reached"),
    )
    for gated_input, reason in cases:
        route = route_investigation(
            gated_input,
            capabilities=_routing_capabilities(),
            policy=policy,
            mode="multi",
        )
        expected = (
            "deterministic_fast_path"
            if reason in {"trusted_pattern_matched", "decision_ready"}
            else "single_agent"
        )
        assert route.strategy == expected
        assert reason in route.reason_codes
        assert route.selected_investigators == ()

    unavailable = route_investigation(
        high_score,
        capabilities=_routing_capabilities(include_log=False),
        policy=policy,
        mode="multi",
    )
    assert unavailable.strategy == "single_agent"
    assert "insufficient_parallel_sources" in unavailable.reason_codes

    disabled = route_investigation(
        high_score,
        capabilities=_routing_capabilities(),
        policy=InvestigationRouterPolicy(multi_agent_enabled=False),
        mode="multi",
    )
    assert disabled.strategy == "single_agent"
    assert "multi_agent_disabled" in disabled.reason_codes


def test_forced_modes_cannot_bypass_fast_path_but_multi_can_bypass_score() -> None:
    policy = InvestigationRouterPolicy(multi_agent_enabled=True)
    low_score = _routing_input()
    forced_multi = route_investigation(
        low_score,
        capabilities=_routing_capabilities(),
        policy=policy,
        mode="multi",
    )
    forced_single = route_investigation(
        replace(low_score, high_quality_conflict=True),
        capabilities=_routing_capabilities(),
        policy=policy,
        mode="single",
    )
    forced_fast = route_investigation(
        replace(low_score, decision_ready=True),
        capabilities=_routing_capabilities(),
        policy=policy,
        mode="single",
    )

    assert forced_multi.strategy == "multi_agent"
    assert forced_single.strategy == "single_agent"
    assert forced_fast.strategy == "deterministic_fast_path"


def test_router_rejects_evaluator_private_or_invalid_shaped_input() -> None:
    safe_values: dict[str, object] = {
        "required_domains": frozenset({"runtime", "log"}),
        "unresolved_hypothesis_count": 1,
        "causal_component_count": 1,
        "missing_causal_roles": frozenset(),
        "high_quality_conflict": False,
        "severity": "warning",
        "trusted_pattern_matched": False,
        "decision_ready": False,
        "valid_tool_calls_without_gain": 0,
        "knowledge_hit": True,
        "remaining_time_ms": 90_000,
        "remaining_model_calls": 8,
        "completed_dispatch_keys": frozenset(),
        "evidence_snapshot_hash": "b" * 64,
        "wave": 0,
    }
    for private_key in (
        "scenarioId",
        "runId",
        "ground_truth",
        "oracle",
        "primary_cause",
        "scoreRules",
    ):
        with pytest.raises(TypeError):
            InvestigationRoutingInput(
                **cast(Any, {**safe_values, private_key: {"nested": "secret"}})
            )
    with pytest.raises(ValueError, match="required domain"):
        _routing_input(required_domains=frozenset({"runtime", "oracle"}))
    with pytest.raises(ValueError, match="snapshot hash"):
        _routing_input(evidence_snapshot_hash="ground_truth.yaml")


def test_router_is_deterministic_for_identical_public_input() -> None:
    routing_input = _routing_input(
        required_domains=frozenset({"knowledge", "runtime", "log"}),
        causal_component_count=2,
        unresolved_hypothesis_count=3,
    )
    policy = InvestigationRouterPolicy(multi_agent_enabled=True)

    first = route_investigation(
        routing_input, capabilities=_routing_capabilities(), policy=policy
    )
    second = route_investigation(
        routing_input, capabilities=_routing_capabilities(), policy=policy
    )

    assert first == second
