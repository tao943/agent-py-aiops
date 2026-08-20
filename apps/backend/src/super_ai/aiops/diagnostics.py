"""Evidence-based LangGraph workflow for AIOps diagnostics."""
# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportTypedDictNotRequiredAccess=false, reportUnnecessaryCast=false

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from operator import add
from time import monotonic
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypedDict, cast
from uuid import uuid4

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from super_ai.aiops.adjudication import (
    DiagnosticFact,
    HypothesisAssessment,
    HypothesisEvidenceRule,
    HypothesisTransition,
    assess_sufficiency,
    instantiate_trusted_evidence_rule,
    reduce_hypotheses,
    trusted_evidence_rule_catalog,
    trusted_reason_causal_role,
)
from super_ai.aiops.cases import DiagnosisCasePersistor
from super_ai.aiops.causal_intents import (
    allowed_causal_intents,
    next_causal_refinement_index,
    repair_plan_causal_coverage,
    supported_causal_coverage,
)
from super_ai.aiops.checkpointing import PostgresDiagnosticCheckpointSaver
from super_ai.aiops.decision_validation import (
    can_replan_deterministic_gap,
    deterministic_checks_payload,
    invoke_structured_root_cause_decision,
    invoke_structured_root_cause_validation,
    validate_grounded_assessments,
    validate_grounded_candidate,
)
from super_ai.aiops.evidence_aggregation import (
    AggregationContext,
    aggregate_evidence_packets,
)
from super_ai.aiops.execution import ExecutionCoordinator, ExecutionIdentity
from super_ai.aiops.facts import PublicToolObservation, extract_public_facts
from super_ai.aiops.investigation import (
    TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    EvidenceClaim,
    EvidencePacket,
    InvestigationRouterPolicy,
    InvestigationRoutingInput,
    StrategyMode,
    build_investigator_capabilities,
    normalize_plan_source_domains,
    route_investigation,
)
from super_ai.aiops.investigation_runtime import (
    DiagnosticToolExecutionRequest,
    DiagnosticToolExecutionResult,
    InvestigationDispatch,
    PreparedDiagnosticToolExecution,
    build_investigation_dispatches,
    execute_diagnostic_tool,
)
from super_ai.aiops.model_budget import (
    ROLE_TIMEOUT_SECONDS,
    ExecutionDeadlines,
    ModelCallBudget,
    ModelCallBudgetExceeded,
    ModelRole,
)
from super_ai.aiops.reasoning import (
    CausalRole,
    DiagnosticPlanStep,
    EvidenceSufficiencyDecision,
    HypothesisState,
    ObservationDecision,
    RecoveryPlan,
    RecoveryPolicyDecision,
    RootCauseDecision,
    RootCauseValidationDecision,
    normalize_root_cause_decision,
    parse_evidence_sufficiency,
    parse_observation_decision,
    parse_plan,
    parse_recovery_plan,
    parse_root_cause_validation,
    project_hypothesis_assessment,
)
from super_ai.aiops.trusted_patterns import resolve_trusted_patterns
from super_ai.aiops.validator_routing import (
    RiskTier,
    ValidatorRiskContext,
    requires_llm_validation,
)
from super_ai.error_catalog import ERROR_DEFINITIONS
from super_ai.llm import ChatModel, LlmProvider
from super_ai.llm.config import StructuredOutputMethod
from super_ai.mcp.cached_client import RuntimeMcpClient
from super_ai.mcp.tool_arguments import (
    ToolArgumentContract,
    ToolArgumentContractError,
    constrain_tool_definitions,
    normalize_tool_arguments,
    tool_step_fingerprint,
)
from super_ai.mcp_client import McpClientError, McpToolDefinition
from super_ai.mcp_connections import McpConnectionService
from super_ai.memory.repositories import (
    DiagnosticReportRecord,
    DiagnosticStepRecord,
    DiagnosticTaskRecord,
    JsonDict,
    MemoryRepositories,
)
from super_ai.observability import elapsed_ms, emit_event
from super_ai.retrieval import (
    KnowledgeRetrievalCitationSource,
    KnowledgeRetrievalError,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolRunner,
)


class AiopsDiagnosticState(TypedDict, total=False):
    workflow_version: str
    graph_version: str
    owner_user_id: str
    task_id: str
    query: str
    alert: JsonDict
    accessible_knowledge_base_ids: tuple[str, ...]
    knowledge_context: JsonDict
    knowledge_evidence_ids: list[str]
    knowledge_completed: bool
    investigation_strategy_mode: StrategyMode
    investigation_route: JsonDict
    investigation_dispatches: list[JsonDict]
    investigation_dispatch: JsonDict
    investigation_packets: Annotated[list[JsonDict], add]
    aggregated_facts: list[JsonDict]
    investigation_aggregation: JsonDict
    investigation_wave: int
    sop_hits: list[JsonDict]
    no_sop_matched: bool
    plan: list[JsonDict]
    plan_origin: str
    plan_index: int
    tool_definitions: tuple[McpToolDefinition, ...]
    public_hypotheses: list[JsonDict]
    decision_vocabulary: JsonDict
    hypothesis_states: list[JsonDict]
    hypothesis_assessments: list[JsonDict]
    diagnostic_facts: list[JsonDict]
    current_tool_output: object
    adjudication_count: int
    adjudicated_fact_count: int
    used_llm_adjudication: bool
    model_call_count: int
    model_call_audits: Annotated[list[JsonDict], add]
    started_at: str
    soft_deadline_at: str
    hard_deadline_at: str
    validator_routing: JsonDict
    observation_decisions: Annotated[list[JsonDict], add]
    root_cause_decision: JsonDict | None
    decision_validation: JsonDict
    recovery_plan: JsonDict
    recovery_policy: JsonDict
    current_evidence_id: str
    current_evidence_summary: str
    current_plan_step: JsonDict
    evidence_sufficiency: JsonDict
    next_route: Literal[
        "executor",
        "replanner",
        "hypothesis_adjudicator",
        "decision",
        "manual_review",
        "report",
        "recovery_planner",
        "llm_validator",
        "policy_gate",
    ]
    replan_count: int
    max_replans: int
    max_total_steps: int
    executor_attempt_count: int
    executed_step_fingerprints: Annotated[list[str], add]
    termination_reason: str
    execution_failed: bool
    report_id: str
    events: Annotated[list[dict[str, object]], add]
    evidence: Annotated[list[JsonDict], add]
    evidence_ids: Annotated[list[str], add]


logger = logging.getLogger(__name__)


class TransientDiagnosticInfrastructureError(RuntimeError):
    """Signal a retryable transport/storage failure to the durable job runtime."""


@dataclass(slots=True)
class _ModelRuntime:
    budget: ModelCallBudget
    deadlines: ExecutionDeadlines
    audits: list[JsonDict]

AIOPS_REPORT_TITLE = "告警分析报告"
AIOPS_GRAPH_VERSION = "aiops-diagnostic-v3"
AIOPS_LEGACY_GRAPH_VERSION = "aiops-diagnostic-v2"
AIOPS_REPORT_REQUIRED_HEADINGS = (
    "# 告警分析报告",
    "## 📋 活跃告警清单",
    "## 📊 结论",
)


def _graph_version_for_task(input_payload: Mapping[str, object]) -> str:
    """Keep unmarked historical v4 tasks on their original topology."""
    requested = input_payload.get("graphVersion")
    if requested == AIOPS_GRAPH_VERSION:
        return AIOPS_GRAPH_VERSION
    return AIOPS_LEGACY_GRAPH_VERSION


def _provider_structured_output_method(
    provider: LlmProvider,
) -> StructuredOutputMethod:
    value = getattr(provider, "structured_output_method", "function_calling")
    if value not in {"function_calling", "json_mode", "json_schema"}:
        raise ValueError("Unsupported structured-output method configured by provider.")
    return cast(StructuredOutputMethod, value)


def _validator_chat_model(provider: LlmProvider) -> ChatModel:
    factory = getattr(provider, "create_validator_model", None)
    if callable(factory):
        return cast(ChatModel, factory())
    return provider.create_chat_model()


def _validator_model_name(provider: LlmProvider) -> str:
    value = getattr(provider, "validator_model_name", None)
    if callable(value):
        value = value()
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,120}", value):
        return value
    return "legacy-main-model"


def _validator_structured_output_method(
    provider: LlmProvider,
) -> StructuredOutputMethod:
    value = getattr(provider, "validator_structured_output_method", None)
    if callable(value):
        value = value()
    if value is None:
        return _provider_structured_output_method(provider)
    if value not in {"function_calling", "json_mode", "json_schema"}:
        raise ValueError("Unsupported Validator structured-output method configured by provider.")
    return cast(StructuredOutputMethod, value)


def build_generic_live_plan(
    *,
    available_tools: Sequence[str],
    known_hypotheses: Sequence[str],
) -> list[JsonDict]:
    """Build a safe evidence-gathering fallback from public Live contracts."""
    available = set(available_tools)
    known = set(known_hypotheses)
    definitions: tuple[
        tuple[str, str, JsonDict, tuple[str, ...], str], ...
    ] = (
        (
            "VerifyServiceHealth",
            "Check database reachability and service health.",
            {"target": "postgres_cluster", "check_connection_pool": True},
            ("postgres_connectivity_failure",),
            "impact",
        ),
        (
            "InspectPostgresSessions",
            "Inspect session states and wait events.",
            {
                "state_filter": ["active", "idle in transaction"],
                "include_wait_events": True,
            },
            ("postgres_slow_query_without_lock", "postgres_lock_blocking"),
            "mechanism",
        ),
        (
            "InspectPostgresLockGraph",
            "Inspect current blocking chains and deadlock signals.",
            {"detect_deadlocks": True, "analyze_blocking_chains": True},
            ("postgres_lock_blocking",),
            "trigger",
        ),
    )
    return [
        {
            "id": f"live-evidence-{index}",
            "tool": tool,
            "arguments": dict(arguments),
            "purpose": purpose,
            "testsHypotheses": [item for item in hypotheses if item in known],
            "causalIntent": causal_intent,
            "causalIntentOrigin": "generic",
            "evidenceRules": [],
        }
        for index, (tool, purpose, arguments, hypotheses, causal_intent) in enumerate(
            definitions, start=1
        )
        if tool in available
    ]


def bind_trusted_tool_arguments(
    plan: Sequence[JsonDict],
    trusted: Mapping[str, Mapping[str, object]],
) -> list[JsonDict]:
    """Copy a plan while binding execution-owned arguments for selected tools."""
    bound: list[JsonDict] = []
    for source in plan:
        step = dict(source)
        tool_name = step.get("tool")
        if isinstance(tool_name, str) and tool_name in trusted:
            step["arguments"] = dict(trusted[tool_name])
        bound.append(step)
    return bound


def plan_matches_tool_contracts(
    plan: Sequence[JsonDict],
    tool_definitions: Sequence[McpToolDefinition],
) -> bool:
    """Validate every planned argument object against its discovered MCP schema."""
    definitions = {item.name: item for item in tool_definitions}
    for step in plan:
        tool_name = step.get("tool")
        arguments = step.get("arguments")
        if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
            return False
        definition = definitions.get(tool_name)
        if definition is None:
            return False
        schema = definition.input_schema
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            validator_class(schema).validate(dict(arguments))
        except (SchemaError, ValidationError):
            return False
    return bool(plan)


def normalize_tool_plan_steps(
    plan: Sequence[JsonDict],
    *,
    trusted_tool_arguments: Mapping[str, Mapping[str, object]],
    tool_argument_contracts: Mapping[str, ToolArgumentContract],
    tool_definitions: Sequence[McpToolDefinition],
) -> tuple[list[JsonDict], list[ToolArgumentContractError]]:
    """Bind, validate, and deduplicate effective planned MCP calls."""
    bound = bind_trusted_tool_arguments(plan, trusted_tool_arguments)
    accepted: list[JsonDict] = []
    errors: list[ToolArgumentContractError] = []
    fingerprints: set[str] = set()
    for source in bound:
        step = dict(source)
        tool_name = step.get("tool")
        arguments = step.get("arguments")
        if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
            errors.append(
                ToolArgumentContractError(
                    code="schema_mismatch",
                    tool_name=str(tool_name or "unknown"),
                )
            )
            continue
        try:
            effective_arguments = normalize_tool_arguments(
                tool_name,
                arguments,
                tool_argument_contracts,
            )
        except ToolArgumentContractError as exc:
            errors.append(exc)
            continue
        step["arguments"] = effective_arguments
        if not plan_matches_tool_contracts([step], tool_definitions):
            errors.append(
                ToolArgumentContractError(
                    code="schema_mismatch",
                    tool_name=tool_name,
                )
            )
            continue
        fingerprint = tool_step_fingerprint(tool_name, effective_arguments)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        accepted.append(step)
    return accepted, errors


def build_grounded_fallback_decision(
    *,
    public_hypotheses: Sequence[JsonDict],
    hypothesis_states: Sequence[JsonDict],
    observation_decisions: Sequence[JsonDict],
    decision_vocabulary: JsonDict,
) -> RootCauseDecision | None:
    """Build a bounded decision from one strongly supported public hypothesis."""
    candidates: list[tuple[str, tuple[str, ...], float]] = []
    for state in hypothesis_states:
        confidence = state.get("confidence")
        if (
            state.get("status") != "supported"
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or float(confidence) < 0.9
        ):
            continue
        evidence_ids = _unique_strings(
            [
                item
                for item in cast(list[object], state.get("evidenceIds") or [])
                if isinstance(item, str)
            ]
        )
        hypothesis_id = state.get("id")
        if isinstance(hypothesis_id, str) and evidence_ids:
            candidates.append((hypothesis_id, tuple(evidence_ids), float(confidence)))
    if len(candidates) != 1:
        return None

    hypothesis_id, direct_evidence_ids, confidence = candidates[0]
    evidence_ids = _supporting_observation_evidence_ids(
        observation_decisions,
        hypothesis_id=hypothesis_id,
    )
    if (
        len(evidence_ids) < 2
        or not set(direct_evidence_ids).issubset(evidence_ids)
    ):
        return None
    public_hypothesis = next(
        (item for item in public_hypotheses if item.get("id") == hypothesis_id),
        None,
    )
    if public_hypothesis is None:
        return None
    labels = _json_dict(
        _json_dict(decision_vocabulary.get("labelsByHypothesis")).get(hypothesis_id)
    )
    component = labels.get("component")
    mechanism = labels.get("mechanism")
    if not all(
        isinstance(item, str) and item.strip()
        for item in (component, mechanism)
    ):
        return None

    evidence_set = set(evidence_ids)
    grounded_observations = _ordered_grounded_observations(
        observation_decisions,
        hypothesis_id=hypothesis_id,
        evidence_ids=evidence_set,
    )
    trigger = _grounded_trigger(grounded_observations)
    causal_chain = tuple(
        cast(str, observation["summary"]).strip()
        for observation in grounded_observations
    )
    if trigger is None or not 2 <= len(causal_chain) <= _MAX_CAUSAL_CHAIN_ITEMS:
        return None
    return RootCauseDecision(
        component=cast(str, component).strip(),
        mechanism=cast(str, mechanism).strip(),
        trigger=trigger,
        causal_chain=causal_chain,
        evidence_ids=evidence_ids,
        confidence=confidence,
    )


def _supporting_observation_evidence_ids(
    observations: Sequence[JsonDict],
    *,
    hypothesis_id: str,
) -> tuple[str, ...]:
    return tuple(
        _unique_strings(
            [
                evidence_id
                for observation in observations
                if hypothesis_id
                in cast(list[object], observation.get("supports") or [])
                for evidence_id in cast(
                    list[object], observation.get("evidenceIds") or []
                )
                if isinstance(evidence_id, str)
            ]
        )
    )


def _project_adjudicated_observations(
    *,
    observations: Sequence[JsonDict],
    assessments: Sequence[HypothesisAssessment],
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Project accepted public citations back onto their source observations."""
    supported = [item for item in assessments if item.disposition == "supported"]
    closed = [
        item
        for item in assessments
        if item.disposition in {"refuted", "causally_inactive"}
    ]
    projected: list[JsonDict] = []
    for source in observations:
        item = dict(source)
        evidence_ids = set(
            _unique_strings(
                [
                    value
                    for value in cast(list[object], item.get("evidenceIds") or [])
                    if isinstance(value, str)
                ]
            )
        )
        supports = _unique_strings(
            [
                *[
                    value
                    for value in cast(list[object], item.get("supports") or [])
                    if isinstance(value, str)
                ],
                *[
                    assessment.hypothesis_id
                    for assessment in supported
                    if evidence_ids.intersection(assessment.evidence_ids)
                ],
            ]
        )
        refutes = _unique_strings(
            [
                *[
                    value
                    for value in cast(list[object], item.get("refutes") or [])
                    if isinstance(value, str)
                ],
                *[
                    assessment.hypothesis_id
                    for assessment in closed
                    if evidence_ids.intersection(assessment.evidence_ids)
                ],
            ]
        )
        item["supports"] = supports
        item["refutes"] = refutes
        if supports or refutes:
            item["assessmentSource"] = "llm_adjudicated"
        projected.append(item)

    if len(supported) != 1:
        return projected
    projected.extend(
        _derive_nginx_port_mismatch_observations(
            assessment=supported[0],
            facts=facts,
        )
    )
    projected.extend(
        _derive_nginx_timeout_observations(
            assessment=supported[0],
            facts=facts,
        )
    )
    projected.extend(
        _derive_redis_pool_recovery_observations(
            assessment=supported[0],
            facts=facts,
        )
    )
    projected.extend(
        _derive_redis_maxclients_observations(
            assessment=supported[0],
            facts=facts,
        )
    )
    projected = _normalize_postgres_lock_observations(
        projected,
        assessment=supported[0],
        facts=facts,
    )
    deadlock_observations = _derive_postgres_deadlock_observations(
        assessment=supported[0],
        facts=facts,
    )
    if deadlock_observations:
        trigger_evidence_ids = set(
            cast(list[str], deadlock_observations[0]["evidenceIds"])
        )
        for observation in projected:
            observation_evidence_ids = {
                item
                for item in cast(list[object], observation.get("evidenceIds") or [])
                if isinstance(item, str)
            }
            if (
                observation.get("causalRole") == "trigger"
                and supported[0].hypothesis_id
                in cast(list[object], observation.get("supports") or [])
                and trigger_evidence_ids.intersection(observation_evidence_ids)
            ):
                observation["causalRole"] = "context"
                observation["causalRoleOrigin"] = "fact_projection_context"
        projected.extend(deadlock_observations)
    supported_id = supported[0].hypothesis_id
    supporting_indexes = [
        index
        for index, item in enumerate(projected)
        if supported_id in cast(list[object], item.get("supports") or [])
    ]
    if any(
        projected[index].get("causalRole") == "trigger"
        for index in supporting_indexes
    ):
        return projected
    source_tools_by_evidence: dict[str, set[str]] = {}
    for fact in facts:
        source_tools_by_evidence.setdefault(fact.evidence_id, set()).add(
            fact.source_tool
        )
    for index in supporting_indexes:
        evidence_ids = [
            value
            for value in cast(
                list[object], projected[index].get("evidenceIds") or []
            )
            if isinstance(value, str)
        ]
        source_tools = {
            tool
            for evidence_id in evidence_ids
            for tool in source_tools_by_evidence.get(evidence_id, set())
        }
        if not any(
            "trigger" in allowed_causal_intents(tool) for tool in source_tools
        ):
            continue
        projected[index]["causalRole"] = "trigger"
        projected[index]["causalRoleOrigin"] = "coverage_repair"
        break
    return projected


def _normalize_postgres_lock_observations(
    observations: Sequence[JsonDict],
    *,
    assessment: HypothesisAssessment,
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Render a PostgreSQL lock chain from cited, normalized public facts."""
    projected = [dict(item) for item in observations]
    if (
        assessment.hypothesis_id != "postgres_lock_blocking"
        or assessment.disposition != "supported"
    ):
        return projected
    public_facts = [fact for fact in facts if fact.public]
    cited = [
        fact for fact in public_facts if fact.evidence_id in assessment.evidence_ids
    ]

    def matching_fact(
        source: Sequence[DiagnosticFact], key: str, expected: object
    ) -> DiagnosticFact | None:
        return next(
            (fact for fact in source if fact.key == key and fact.value == expected),
            None,
        )

    blocker = matching_fact(
        cited, "InspectPostgresLockGraph.blockerRole", "transaction"
    )
    resource = matching_fact(
        cited, "InspectPostgresLockGraph.lockedResource", "order_row"
    )
    operation = matching_fact(
        cited,
        "InspectPostgresSessions.waitingOperation", "order_status_update"
    )
    wait_event = matching_fact(cited, "InspectPostgresSessions.waitEventType", "Lock")
    timed_out = matching_fact(
        public_facts, "VerifyServiceHealth.businessProbeTimedOut", True
    )
    reachable = matching_fact(
        public_facts, "VerifyServiceHealth.databaseReachable", True
    )
    cls_contention = next(
        (
            fact
            for fact in public_facts
            if fact.key == "SearchLog.records.event"
            and isinstance(fact.value, Sequence)
            and not isinstance(fact.value, (str, bytes))
            and "database_contention" in fact.value
        ),
        None,
    )
    if any(
        item is None
        for item in (
            blocker,
            resource,
            operation,
            wait_event,
            timed_out,
            reachable,
            cls_contention,
        )
    ):
        return projected
    assert blocker is not None
    assert resource is not None
    assert operation is not None
    assert wait_event is not None
    assert timed_out is not None
    assert reachable is not None
    assert cls_contention is not None
    if (
        blocker.evidence_id != resource.evidence_id
        or operation.evidence_id != wait_event.evidence_id
        or timed_out.evidence_id != reachable.evidence_id
    ):
        return projected
    replacements: dict[str, tuple[str, str, list[str]]] = {
        blocker.evidence_id: (
            "A blocker transaction holds the PostgreSQL row lock required by the order "
            "status update.",
            "trigger",
            [blocker.evidence_id],
        ),
        operation.evidence_id: (
            "The order status update waits on the held PostgreSQL row lock.",
            "mechanism",
            [operation.evidence_id],
        ),
        timed_out.evidence_id: (
            "The blocked business probe times out while PostgreSQL remains reachable.",
            "impact",
            [timed_out.evidence_id],
        ),
        cls_contention.evidence_id: (
            "Database contention causes the blocked business probe to time out with a "
            "request timeout.",
            "impact",
            sorted({cls_contention.evidence_id, timed_out.evidence_id}),
        ),
    }
    for observation in projected:
        evidence_ids = {
            item
            for item in cast(list[object], observation.get("evidenceIds") or [])
            if isinstance(item, str)
        }
        matched = [
            replacement
            for evidence_id, replacement in replacements.items()
            if evidence_id in evidence_ids
        ]
        if len(matched) != 1:
            continue
        summary, causal_role, replacement_evidence_ids = matched[0]
        observation["summary"] = summary
        observation["causalRole"] = causal_role
        observation["evidenceIds"] = replacement_evidence_ids
        observation["supports"] = _unique_strings(
            [
                *[
                    item
                    for item in cast(list[object], observation.get("supports") or [])
                    if isinstance(item, str)
                ],
                assessment.hypothesis_id,
            ]
        )
        observation["causalRoleOrigin"] = "coverage_repair"
    return projected


def _derive_postgres_deadlock_observations(
    *,
    assessment: HypothesisAssessment,
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Derive a deadlock chain from a current-run public transaction audit."""
    if (
        assessment.hypothesis_id != "postgres_deadlock"
        or assessment.disposition != "supported"
    ):
        return []
    cited = [
        fact
        for fact in facts
        if fact.public and fact.evidence_id in assessment.evidence_ids
    ]

    def matching_fact(key: str, expected: object | None = None) -> DiagnosticFact | None:
        return next(
            (
                fact
                for fact in cited
                if fact.key == key and (expected is None or fact.value == expected)
            ),
            None,
        )

    audit_facts = (
        matching_fact("InspectPostgresDeadlockAudit.transactionAFirstResource"),
        matching_fact("InspectPostgresDeadlockAudit.transactionASecondResource"),
        matching_fact("InspectPostgresDeadlockAudit.transactionBFirstResource"),
        matching_fact("InspectPostgresDeadlockAudit.transactionBSecondResource"),
        matching_fact("InspectPostgresDeadlockAudit.cycleDetected", True),
        matching_fact("InspectPostgresDeadlockAudit.sqlstate", "40P01"),
    )
    aborted = matching_fact("InspectPostgresTransactionResult.aborted", True)
    if any(item is None for item in audit_facts) or aborted is None:
        return []
    first_a, second_a, first_b, second_b, cycle, sqlstate = cast(
        tuple[DiagnosticFact, ...], audit_facts
    )
    audit_evidence_ids = {
        item.evidence_id
        for item in (first_a, second_a, first_b, second_b, cycle, sqlstate)
    }
    if (
        len(audit_evidence_ids) != 1
        or first_a.value == second_a.value
        or first_a.value != second_b.value
        or second_a.value != first_b.value
    ):
        return []
    audit_evidence_id = next(iter(audit_evidence_ids))
    common: JsonDict = {
        "purpose": "Project the public current-run deadlock audit into a causal chain.",
        "supports": [assessment.hypothesis_id],
        "refutes": [],
        "assessmentSource": assessment.assessment_source,
        "causalRoleOrigin": "fact_projection",
    }
    return [
        {
            **common,
            "summary": (
                "Two concurrent transactions acquire the same order rows in reverse order: "
                "transaction A acquires order row 1 then order row 2, while transaction B "
                "acquires order row 2 then order row 1."
            ),
            "evidenceIds": [audit_evidence_id],
            "causalRole": "trigger",
        },
        {
            **common,
            "summary": (
                "The reverse resource order causes a cyclic wait relationship and a "
                "deadlock cycle."
            ),
            "evidenceIds": [audit_evidence_id],
            "causalRole": "mechanism",
        },
        {
            **common,
            "summary": (
                "PostgreSQL records an aborted transaction, the victim transaction, with "
                "deadlock error SQLSTATE 40P01."
            ),
            "evidenceIds": sorted({audit_evidence_id, aborted.evidence_id}),
            "causalRole": "impact",
        },
    ]


def _derive_nginx_port_mismatch_observations(
    *,
    assessment: HypothesisAssessment,
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Split one cited Nginx mismatch observation into grounded causal roles."""
    if assessment.hypothesis_id != "upstream_port_mismatch":
        return []
    cited = [
        fact
        for fact in facts
        if fact.public and fact.evidence_id in assessment.evidence_ids
    ]
    upstream = next(
        (fact for fact in cited if fact.key == "InspectNginx.upstreamPort"),
        None,
    )
    container = next(
        (
            fact
            for key in (
                "InspectContainer.listeningPorts",
                "InspectContainer.configuredPorts",
            )
            for fact in cited
            if fact.key == key
        ),
        None,
    )
    connection_error = next(
        (
            fact
            for fact in cited
            if fact.key == "InspectNginx.error"
            and isinstance(fact.value, str)
            and (
                "connection refused" in fact.value.casefold()
                or "connect() failed" in fact.value.casefold()
            )
        ),
        None,
    )
    response_status = next(
        (
            fact
            for fact in cited
            if fact.key == "InspectNginx.responseStatus"
            and isinstance(fact.value, int)
            and not isinstance(fact.value, bool)
            and 500 <= fact.value < 600
        ),
        None,
    )
    if (
        upstream is None
        or container is None
        or connection_error is None
        or response_status is None
        or isinstance(container.value, (str, bytes))
        or not isinstance(container.value, Sequence)
        or upstream.value in cast(Sequence[object], container.value)
    ):
        return []
    mismatch_evidence_ids = sorted(
        {upstream.evidence_id, container.evidence_id}
    )
    common: JsonDict = {
        "supports": [assessment.hypothesis_id],
        "refutes": [],
        "causalRoleOrigin": "coverage_repair",
        "assessmentSource": "llm_adjudicated",
    }
    return [
        {
            **common,
            "purpose": "Compare the configured Nginx upstream with container ports.",
            "summary": (
                f"Nginx upstream port {upstream.value} differs from the container "
                f"listening ports {json.dumps(container.value)}."
            ),
            "evidenceIds": mismatch_evidence_ids,
            "causalRole": "trigger",
        },
        {
            **common,
            "purpose": "Establish the upstream connection failure mechanism.",
            "summary": "Nginx failed to connect to the configured upstream endpoint.",
            "evidenceIds": [connection_error.evidence_id],
            "causalRole": "mechanism",
        },
        {
            **common,
            "purpose": "Establish the user-visible gateway impact.",
            "summary": (
                f"The Nginx upstream request returned HTTP {response_status.value}."
            ),
            "evidenceIds": [response_status.evidence_id],
            "causalRole": "impact",
        },
    ]


def _derive_redis_pool_recovery_observations(
    *,
    assessment: HypothesisAssessment,
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Derive a bounded pool-recovery trigger from cited public Redis facts."""
    if assessment.hypothesis_id != "redis_client_connection_lifecycle":
        return []
    cited = [
        fact
        for fact in facts
        if fact.public and fact.evidence_id in assessment.evidence_ids
    ]
    stale = next(
        (
            fact
            for fact in cited
            if fact.key == "InspectRedisClientPool.staleConnections"
            and isinstance(fact.value, int)
            and not isinstance(fact.value, bool)
            and fact.value > 0
        ),
        None,
    )
    waiting = next(
        (
            fact
            for fact in cited
            if fact.key == "InspectRedisClientPool.waitingRequests"
            and isinstance(fact.value, int)
            and not isinstance(fact.value, bool)
            and fact.value > 0
        ),
        None,
    )
    generation = next(
        (
            fact
            for fact in cited
            if fact.key
            == "InspectRedisClientPool.poolGenerationChangedAfterRecovery"
            and fact.value is False
        ),
        None,
    )
    direct_ping = next(
        (
            fact
            for fact in cited
            if fact.key == "InspectRedisClientPool.directNewConnectionPing"
            and isinstance(fact.value, str)
            and fact.value.casefold() == "pong"
        ),
        None,
    )
    if any(item is None for item in (stale, waiting, generation, direct_ping)):
        return []
    evidence_ids = sorted(
        {
            cast(DiagnosticFact, item).evidence_id
            for item in (stale, waiting, generation, direct_ping)
        }
    )
    return [
        {
            "purpose": "Establish whether the client pool rotated after recovery.",
            "supports": [assessment.hypothesis_id],
            "refutes": [],
            "summary": (
                "The Redis client pool generation did not change after recovery while stale "
                "connections and waiting requests remained, although a new connection "
                "succeeded."
            ),
            "evidenceIds": evidence_ids,
            "causalRole": "trigger",
            "causalRoleOrigin": "coverage_repair",
            "assessmentSource": "llm_adjudicated",
        }
    ]


def _derive_redis_maxclients_observations(
    *,
    assessment: HypothesisAssessment,
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Derive Redis capacity trigger and rejection impact from cited counters."""
    if assessment.hypothesis_id != "redis_maxclients":
        return []
    public_facts = [fact for fact in facts if fact.public]
    cited = [
        fact
        for fact in public_facts
        if fact.evidence_id in assessment.evidence_ids
    ]

    def positive_integer(
        source: Sequence[DiagnosticFact], *keys: str
    ) -> DiagnosticFact | None:
        return next(
            (
                fact
                for fact in source
                if fact.key in keys
                and isinstance(fact.value, int)
                and not isinstance(fact.value, bool)
                and fact.value > 0
            ),
            None,
        )

    connected = positive_integer(
        cited,
        "InspectRedisServerInfo.connectedClients",
        "InspectRedisServer.connectedClients",
    )
    maximum = positive_integer(
        cited,
        "InspectRedisServerInfo.maxclients",
        "InspectRedisServer.maxclients",
    )
    rejected = positive_integer(
        cited,
        "InspectRedisServerInfo.rejectedConnectionsDelta",
        "GetRedisConnectionMetrics.rejectedConnectionsDelta",
    )
    if (
        connected is None
        or maximum is None
        or connected.value != maximum.value
        or rejected is None
    ):
        return []
    common: JsonDict = {
        "supports": [assessment.hypothesis_id],
        "refutes": [],
        "causalRoleOrigin": "coverage_repair",
        "assessmentSource": "llm_adjudicated",
    }
    scoped_clients = positive_integer(
        public_facts,
        "ListBenchmarkRedisClients.currentRunClientCount",
    )
    established_healthy = next(
        (
            fact
            for fact in public_facts
            if fact.key == "VerifyRedisPing.establishedConnectionHealthy"
            and fact.value is True
        ),
        None,
    )
    trigger_summary = (
        "Current-run benchmark clients filled Redis connection capacity, and connected "
        f"clients reached the configured maxclients limit of {maximum.value}."
        if scoped_clients is not None
        else (
            "Redis connected clients reached the configured maxclients limit of "
            f"{maximum.value}."
        )
    )
    observations: list[JsonDict] = [
        {
            **common,
            "purpose": "Establish whether Redis reached its client capacity.",
            "summary": trigger_summary,
            "evidenceIds": _unique_strings(
                [
                    connected.evidence_id,
                    *(
                        [scoped_clients.evidence_id]
                        if scoped_clients is not None
                        else []
                    ),
                ]
            ),
            "causalRole": "trigger",
        },
    ]
    if established_healthy is not None:
        observations.append(
            {
                **common,
                "purpose": "Establish whether existing Redis connections remained healthy.",
                "summary": (
                    f"At the maxclients capacity of {maximum.value}, ping succeeds on the "
                    "established Redis control connection."
                ),
                "evidenceIds": _unique_strings(
                    [maximum.evidence_id, established_healthy.evidence_id]
                ),
                "causalRole": "context",
            }
        )
    observations.append(
        {
            **common,
            "purpose": "Establish the impact on new Redis connections.",
            "summary": (
                f"Redis recorded rejected connections (count: {rejected.value}) because "
                "client capacity was saturated, causing new connections to fail."
            ),
            "evidenceIds": [rejected.evidence_id],
            "causalRole": "impact",
        }
    )
    return observations


def _derive_upstream_deadline_observations(
    *,
    hypothesis_id: str,
    facts: Sequence[DiagnosticFact],
    evidence_id: str,
) -> list[JsonDict]:
    """Derive a trigger when an upstream probe exceeds the gateway deadline."""
    if hypothesis_id != "nginx_upstream_response_timeout":
        return []
    current = [
        fact for fact in facts if fact.public and fact.evidence_id == evidence_id
    ]

    def positive_number(key: str) -> DiagnosticFact | None:
        return next(
            (
                fact
                for fact in current
                if fact.key == key
                and isinstance(fact.value, (int, float))
                and not isinstance(fact.value, bool)
                and fact.value > 0
            ),
            None,
        )

    first_byte = positive_number("ProbeUpstreamHealth.firstByteMs")
    deadline = positive_number("ProbeUpstreamHealth.gatewayReadDeadlineMs")
    connected = next(
        (
            fact
            for fact in current
            if fact.key == "ProbeUpstreamHealth.tcpConnect"
            and isinstance(fact.value, str)
            and fact.value.casefold() == "success"
        ),
        None,
    )
    if (
        first_byte is None
        or deadline is None
        or connected is None
        or cast(float, first_byte.value) <= cast(float, deadline.value)
    ):
        return []
    return [
        {
            "purpose": "Establish whether upstream response time exceeded the gateway limit.",
            "supports": [hypothesis_id],
            "refutes": [],
            "summary": (
                f"The upstream TCP connection succeeded, but first byte time "
                f"{first_byte.value} ms exceeded the gateway read deadline "
                f"{deadline.value} ms."
            ),
            "evidenceIds": [evidence_id],
            "causalRole": "trigger",
            "causalRoleOrigin": "coverage_repair",
            "assessmentSource": "deterministic",
        }
    ]


def _derive_nginx_timeout_observations(
    *,
    assessment: HypothesisAssessment,
    facts: Sequence[DiagnosticFact],
) -> list[JsonDict]:
    """Derive the Live Nginx timeout chain from bounded public facts."""
    if assessment.hypothesis_id != "nginx_upstream_response_timeout":
        return []
    public_facts = [fact for fact in facts if fact.public]

    def matching(key: str, expected: object) -> DiagnosticFact | None:
        return next(
            (
                fact
                for fact in public_facts
                if fact.key == key and fact.value == expected
            ),
            None,
        )

    duration = next(
        (
            fact
            for fact in public_facts
            if fact.key == "InspectNginxRequestTimeline.requestDurationMs"
            and isinstance(fact.value, (int, float))
            and not isinstance(fact.value, bool)
            and fact.value > 0
        ),
        None,
    )
    gateway_status = matching("InspectNginxRequestTimeline.gatewayStatus", 504)
    connected = matching(
        "InspectNginxRequestTimeline.upstreamConnectSucceeded", True
    )
    timeout_observed = matching(
        "ReadNginxTimeoutSummary.gatewayTimeoutObserved", True
    )
    deadline_elapsed = matching(
        "ReadNginxTimeoutSummary.readDeadlineElapsed", True
    )
    upstream_status = matching("ProbeLiveEvalUpstream.status", 200)
    upstream_healthy = matching("ProbeLiveEvalUpstream.healthy", True)
    if any(
        fact is None
        for fact in (
            duration,
            gateway_status,
            connected,
            timeout_observed,
            deadline_elapsed,
            upstream_status,
            upstream_healthy,
        )
    ):
        return []
    assert duration is not None
    assert gateway_status is not None
    assert connected is not None
    assert timeout_observed is not None
    assert upstream_status is not None
    common: JsonDict = {
        "supports": [assessment.hypothesis_id],
        "refutes": [],
        "causalRoleOrigin": "coverage_repair",
        "assessmentSource": "llm_adjudicated",
    }
    return [
        {
            **common,
            "purpose": "Establish whether the upstream response exceeded the gateway limit.",
            "summary": (
                f"The test upstream produced a slow response lasting {duration.value} ms, "
                "and the response delay exceeded the Nginx proxy read timeout."
            ),
            "evidenceIds": _unique_strings(
                [duration.evidence_id, timeout_observed.evidence_id]
            ),
            "causalRole": "trigger",
        },
        {
            **common,
            "purpose": "Establish whether the gateway connected to the upstream.",
            "summary": (
                "The Nginx gateway confirms that the connection established to the test "
                "upstream and the upstream connect succeeds before the response wait."
            ),
            "evidenceIds": [connected.evidence_id],
            "causalRole": "context",
        },
        {
            **common,
            "purpose": "Establish the gateway impact while upstream health remains available.",
            "summary": (
                "The exceeded response deadline causes Nginx to return HTTP 504 gateway "
                "timeout while the upstream health endpoint remains available."
            ),
            "evidenceIds": _unique_strings(
                [
                    gateway_status.evidence_id,
                    timeout_observed.evidence_id,
                    upstream_status.evidence_id,
                ]
            ),
            "causalRole": "impact",
        },
    ]


_MAX_CAUSAL_CHAIN_ITEMS = 6


def _ordered_grounded_observations(
    observations: Sequence[JsonDict],
    *,
    hypothesis_id: str,
    evidence_ids: set[str],
) -> tuple[JsonDict, ...]:
    supported: list[JsonDict] = []
    seen_summaries: set[str] = set()
    for observation in observations:
        if hypothesis_id not in cast(list[object], observation.get("supports") or []):
            continue
        linked_evidence = {
            item
            for item in cast(list[object], observation.get("evidenceIds") or [])
            if isinstance(item, str)
        }
        summary = observation.get("summary")
        if not evidence_ids.intersection(linked_evidence) or not isinstance(summary, str):
            continue
        normalized_summary = " ".join(summary.casefold().split())
        if not normalized_summary or normalized_summary in seen_summaries:
            continue
        seen_summaries.add(normalized_summary)
        supported.append(observation)

    triggers = [item for item in supported if item.get("causalRole") == "trigger"]
    mechanisms = [
        item for item in supported if item.get("causalRole") == "mechanism"
    ]
    impacts = [item for item in supported if item.get("causalRole") == "impact"]
    contexts = [item for item in supported if item.get("causalRole") == "context"]
    terminal = impacts[-1:] if impacts else []
    mechanism_limit = _MAX_CAUSAL_CHAIN_ITEMS - len(triggers) - len(terminal)
    selected = [*triggers, *mechanisms[:mechanism_limit]]
    context_limit = _MAX_CAUSAL_CHAIN_ITEMS - len(selected) - len(terminal)
    selected.extend(contexts[:context_limit])
    selected.extend(terminal)
    return tuple(selected)


def _grounded_trigger(observations: Sequence[JsonDict]) -> str | None:
    triggers = [
        cast(str, item["summary"]).strip()
        for item in observations
        if item.get("causalRole") == "trigger"
        and isinstance(item.get("summary"), str)
        and cast(str, item["summary"]).strip()
    ]
    return triggers[0] if len(triggers) == 1 else None


def _normalize_grounded_decision(
    decision: RootCauseDecision,
    *,
    available_evidence_ids: set[str],
    public_hypotheses: Sequence[JsonDict],
    hypothesis_states: Sequence[JsonDict],
    observation_decisions: Sequence[JsonDict],
    decision_vocabulary: JsonDict,
) -> RootCauseDecision | None:
    validation = validate_grounded_candidate(
        candidate=decision,
        available_evidence_ids=available_evidence_ids,
        hypothesis_states=hypothesis_states,
        observation_decisions=observation_decisions,
        decision_vocabulary=decision_vocabulary,
    )
    failed_codes = {
        check.code for check in validation.checks if not check.passed
    }
    expression_codes = {"trigger_present", "grounded_causal_chain"}
    if (
        not failed_codes
        or not failed_codes.issubset(expression_codes)
        or validation.supported_hypothesis_id is None
        or validation.supported_hypothesis_id
        not in {
            str(item.get("id"))
            for item in public_hypotheses
            if item.get("id")
        }
        or len(set(decision.evidence_ids)) < 2
    ):
        return None

    grounded_observations = _ordered_grounded_observations(
        observation_decisions,
        hypothesis_id=validation.supported_hypothesis_id,
        evidence_ids=set(decision.evidence_ids),
    )
    if len(grounded_observations) < 2:
        return None
    cited_evidence = set(decision.evidence_ids)
    normalized_evidence = {
        item
        for observation in grounded_observations
        for item in cast(list[object], observation.get("evidenceIds") or [])
        if isinstance(item, str)
    }
    if not normalized_evidence or not normalized_evidence.issubset(cited_evidence):
        return None
    trigger = _grounded_trigger(grounded_observations)
    if trigger is None:
        return None
    normalized = RootCauseDecision(
        component=decision.component,
        mechanism=decision.mechanism,
        trigger=trigger,
        causal_chain=tuple(
            cast(str, observation["summary"]).strip()
            for observation in grounded_observations
        ),
        evidence_ids=decision.evidence_ids,
        confidence=decision.confidence,
    )
    normalized_validation = validate_grounded_candidate(
        candidate=normalized,
        available_evidence_ids=available_evidence_ids,
        hypothesis_states=hypothesis_states,
        observation_decisions=observation_decisions,
        decision_vocabulary=decision_vocabulary,
    )
    if not normalized_validation.passed:
        return None
    return normalized


def _benchmark_strategy_mode(input_payload: Mapping[str, object]) -> StrategyMode:
    """Accept strategy overrides only from an internal benchmark task contract."""
    if input_payload.get("benchmarkMode") not in {"snapshot", "live"}:
        return "auto"
    requested = input_payload.get("investigationStrategyMode")
    if requested in {"auto", "single", "multi"}:
        return cast(StrategyMode, requested)
    return "auto"


class AiopsDiagnosticService:
    """Run a bounded Plan-Execute-Replan workflow for one owned task."""

    def __init__(
        self,
        *,
        repositories: MemoryRepositories,
        llm_provider: LlmProvider,
        retrieval_tool: KnowledgeRetrievalToolRunner,
        mcp_client: RuntimeMcpClient | None = None,
        mcp_client_provider: McpConnectionService | None = None,
        cls_region: str,
        cls_topic_id: str,
        trusted_tool_arguments: Mapping[str, Mapping[str, object]] | None = None,
        tool_argument_contracts: Mapping[str, ToolArgumentContract] | None = None,
        tool_policies: Mapping[str, Literal["proposal_only"]] | None = None,
        case_persistor: DiagnosisCasePersistor | None = None,
        investigation_router_policy: InvestigationRouterPolicy | None = None,
    ) -> None:
        self._repositories = repositories
        self._llm_provider = llm_provider
        self._retrieval_tool = retrieval_tool
        self._mcp_client = mcp_client
        self._mcp_client_provider = mcp_client_provider
        if mcp_client is None and mcp_client_provider is None:
            raise ValueError("An MCP client or provider is required.")
        self._cls_region = cls_region
        self._cls_topic_id = cls_topic_id
        self._trusted_tool_arguments = {
            name: dict(arguments)
            for name, arguments in (trusted_tool_arguments or {}).items()
        }
        self._tool_argument_contracts = MappingProxyType(
            dict(tool_argument_contracts or {})
        )
        copied_tool_policies = dict(tool_policies or {})
        unsupported_policies = {
            str(policy) for policy in copied_tool_policies.values() if policy != "proposal_only"
        }
        if unsupported_policies:
            raise ValueError(
                "Unsupported tool policy: " + ", ".join(sorted(unsupported_policies))
            )
        validated_tool_policies: dict[str, Literal["proposal_only"]] = {
            name: cast(Literal["proposal_only"], policy)
            for name, policy in copied_tool_policies.items()
        }
        self._tool_policies: Mapping[str, Literal["proposal_only"]] = MappingProxyType(
            validated_tool_policies
        )
        self._case_persistor = case_persistor
        self._step_sequence_lock = asyncio.Lock()
        self._investigation_router_policy = (
            investigation_router_policy or InvestigationRouterPolicy()
        )

    async def stream(
        self,
        *,
        task: DiagnosticTaskRecord,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> AsyncIterator[dict[str, object]]:
        """Execute a diagnostic and yield shared SSE payloads in graph order."""
        started_at = monotonic()
        emit_event(logger, "agent.aiops.started", diagnosticTaskId=task.id)
        await self._repositories.diagnostics.update_task(
            owner_user_id=task.owner_user_id,
            task_id=task.id,
            status="running",
        )
        requested_workflow_version = task.input_payload.get("workflowVersion")
        workflow_version = (
            "evidence-driven-v4"
            if requested_workflow_version == "evidence-driven-v4"
            else "evidence-driven-v3"
        )
        graph_version = (
            _graph_version_for_task(task.input_payload)
            if workflow_version == "evidence-driven-v4"
            else None
        )
        deadlines = ExecutionDeadlines.start(_now())
        checkpointer: PostgresDiagnosticCheckpointSaver | None = None
        graph_config: dict[str, object] | None = None
        if workflow_version == "evidence-driven-v4" and self._repositories.aiops_runtime:
            assert graph_version is not None
            checkpoint_repository = self._repositories.aiops_runtime.checkpoint_repository(
                owner_user_id=task.owner_user_id,
                task_id=task.id,
                graph_version=graph_version,
            )
            checkpointer = PostgresDiagnosticCheckpointSaver(
                checkpoint_repository,
                task_id=task.id,
                graph_version=graph_version,
            )
            graph_config = {
                "configurable": {
                    "thread_id": f"aiops:{task.id}:{graph_version}",
                    "checkpoint_ns": "",
                }
            }
        prior_checkpoint = (
            await checkpointer.aget_tuple(cast(Any, graph_config))
            if checkpointer is not None and graph_config is not None
            else None
        )
        alert = _json_dict(task.input_payload.get("alert"))
        initial_evidence_ids: list[str] = []
        if alert and prior_checkpoint is None:
            alert_evidence = await self._repositories.diagnostics.create_evidence(
                owner_user_id=task.owner_user_id,
                evidence_id=_stable_public_id("evidence", task.id, "alert"),
                task_id=task.id,
                kind="alert",
                source="diagnostic-input",
                summary="Original alert input for the diagnostic.",
                payload=alert,
            )
            initial_evidence_ids.append(alert_evidence.id)
        graph = self._build_graph(
            workflow_version=workflow_version,
            graph_version=graph_version,
            checkpointer=checkpointer,
        )
        public_hypotheses = _json_list(task.input_payload.get("hypotheses"))
        initial_state: AiopsDiagnosticState = {
            "workflow_version": workflow_version,
            "graph_version": graph_version or "",
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "query": task.query,
            "alert": alert,
            "public_hypotheses": public_hypotheses,
            "decision_vocabulary": _json_dict(
                task.input_payload.get("decisionVocabulary")
            ),
            "hypothesis_states": _initial_hypothesis_states(
                public_hypotheses
            ),
            "hypothesis_assessments": _initial_hypothesis_assessments(
                public_hypotheses
            ),
            "diagnostic_facts": [],
            "adjudication_count": 0,
            "adjudicated_fact_count": 0,
            "used_llm_adjudication": False,
            "model_call_count": 0,
            "model_call_audits": [],
            "started_at": deadlines.started_at.isoformat(),
            "soft_deadline_at": deadlines.soft_deadline_at.isoformat(),
            "hard_deadline_at": deadlines.hard_deadline_at.isoformat(),
            "observation_decisions": [],
            "accessible_knowledge_base_ids": tuple(accessible_knowledge_base_ids),
            "knowledge_context": {},
            "knowledge_evidence_ids": [],
            "knowledge_completed": False,
            "investigation_strategy_mode": _benchmark_strategy_mode(
                task.input_payload
            ),
            "investigation_route": {},
            "investigation_dispatches": [],
            "investigation_packets": [],
            "investigation_aggregation": {},
            "investigation_wave": 0,
            "plan_index": 0,
            "replan_count": 0,
            "max_replans": 1 if workflow_version == "evidence-driven-v4" else 2,
            "max_total_steps": 6,
            "executor_attempt_count": 0,
            "executed_step_fingerprints": [],
            "termination_reason": "",
            "execution_failed": False,
            "events": [],
            "evidence": [],
            "evidence_ids": initial_evidence_ids,
        }
        try:
            graph_input: AiopsDiagnosticState | None = initial_state
            if prior_checkpoint is not None:
                graph_input = None
            async for update in graph.astream(
                graph_input,
                config=cast(Any, graph_config),
                stream_mode="updates",
            ):
                for node_update in cast(Mapping[str, object], update).values():
                    if not isinstance(node_update, Mapping):
                        continue
                    events = node_update.get("events")
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        if isinstance(event, dict):
                            yield cast(dict[str, object], event)
        except Exception as exc:
            if _is_transient_infrastructure_error(exc):
                emit_event(
                    logger,
                    "agent.aiops.retryable_failure",
                    diagnosticTaskId=task.id,
                    errorCategory=exc.__class__.__name__,
                    durationMs=elapsed_ms(started_at),
                )
                raise TransientDiagnosticInfrastructureError(
                    "retryable_diagnostic_infrastructure_failure"
                ) from exc
            await self._repositories.diagnostics.update_task(
                owner_user_id=task.owner_user_id,
                task_id=task.id,
                status="failed",
                result_payload={
                    "failure": "Diagnostic execution failed before a report was produced."
                },
                completed_at=_now(),
            )
            emit_event(
                logger,
                "agent.aiops.failed",
                diagnosticTaskId=task.id,
                errorCategory=exc.__class__.__name__,
                durationMs=elapsed_ms(started_at),
            )
            yield _error_event("SYSTEM_INTERNAL_ERROR")
            return
        emit_event(
            logger,
            "agent.aiops.completed",
            diagnosticTaskId=task.id,
            durationMs=elapsed_ms(started_at),
        )

    async def _mcp_client_for(self, owner_user_id: str) -> RuntimeMcpClient:
        if self._mcp_client_provider is not None:
            return await self._mcp_client_provider.client_for_user(owner_user_id=owner_user_id)
        if self._mcp_client is None:
            raise McpClientError("MCP client is unavailable.")
        return self._mcp_client

    def _model_runtime(self, state: AiopsDiagnosticState) -> _ModelRuntime:
        return _ModelRuntime(
            budget=ModelCallBudget(used=int(state.get("model_call_count") or 0)),
            deadlines=_execution_deadlines_from_state(state),
            audits=[],
        )

    async def _invoke_v4_model(
        self,
        runtime: _ModelRuntime,
        *,
        role: ModelRole,
        prompt: str,
    ) -> object | None:
        started_at = monotonic()
        if runtime.deadlines.hard_expired():
            runtime.audits.append(
                _model_call_audit_payload(
                    role=role,
                    attempt=runtime.budget.used,
                    duration_ms=0,
                    safe_error_code="hard_deadline_exceeded",
                )
            )
            return None
        if role in {"replanner", "adjudicator"} and runtime.deadlines.soft_expired():
            runtime.audits.append(
                _model_call_audit_payload(
                    role=role,
                    attempt=runtime.budget.used,
                    duration_ms=0,
                    safe_error_code="soft_deadline_exceeded",
                )
            )
            return None
        try:
            attempt = runtime.budget.reserve(role)
        except ModelCallBudgetExceeded:
            runtime.audits.append(
                _model_call_audit_payload(
                    role=role,
                    attempt=runtime.budget.used,
                    duration_ms=0,
                    safe_error_code="model_call_budget_exhausted",
                )
            )
            return None
        remaining = max(
            0.001,
            (runtime.deadlines.hard_deadline_at - _now()).total_seconds(),
        )
        try:
            model = (
                _validator_chat_model(self._llm_provider)
                if role == "validator"
                else self._llm_provider.create_chat_model()
            )
            response = await asyncio.wait_for(
                model.ainvoke(prompt),
                timeout=min(float(ROLE_TIMEOUT_SECONDS[role]), remaining),
            )
        except Exception as exc:
            runtime.audits.append(
                _model_call_audit_payload(
                    role=role,
                    attempt=attempt,
                    duration_ms=int(round(elapsed_ms(started_at))),
                    safe_error_code=_safe_model_call_error_code(exc),
                )
            )
            return None
        runtime.audits.append(
            _model_call_audit_payload(
                role=role,
                attempt=attempt,
                duration_ms=int(round(elapsed_ms(started_at))),
                safe_error_code=None,
            )
        )
        return response

    def _build_graph(
        self,
        *,
        workflow_version: str = "evidence-driven-v3",
        graph_version: str | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Any:
        if workflow_version == "evidence-driven-v4":
            selected_graph_version = graph_version or AIOPS_GRAPH_VERSION
            if selected_graph_version not in {
                AIOPS_GRAPH_VERSION,
                AIOPS_LEGACY_GRAPH_VERSION,
            }:
                raise ValueError("Unsupported AIOps graph version.")
            return self._build_v4_graph(
                checkpointer=checkpointer,
                include_knowledge_investigator=(
                    selected_graph_version == AIOPS_GRAPH_VERSION
                ),
            )
        graph = StateGraph(AiopsDiagnosticState)
        graph.add_node("planner", self._planner)
        graph.add_node("executor", self._executor)
        graph.add_node("evidence_evaluator", self._evidence_evaluator)
        graph.add_node("sufficiency_gate", self._sufficiency_gate)
        graph.add_node("replanner", self._replanner)
        graph.add_node("decision", self._decision)
        graph.add_node("decision_validator", self._decision_validator)
        graph.add_node("recovery_planner", self._recovery_planner)
        graph.add_node("policy_gate", self._policy_gate)
        graph.add_node("report", self._report)
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "executor")
        graph.add_edge("executor", "evidence_evaluator")
        graph.add_edge("evidence_evaluator", "sufficiency_gate")
        graph.add_conditional_edges(
            "sufficiency_gate",
            self._route_after_sufficiency,
            {"executor": "executor", "replanner": "replanner", "decision": "decision"},
        )
        graph.add_conditional_edges(
            "replanner",
            self._route_after_replanner,
            {"executor": "executor", "decision": "decision"},
        )
        graph.add_edge("decision", "decision_validator")
        graph.add_conditional_edges(
            "decision_validator",
            self._route_after_decision_validation,
            {"replanner": "replanner", "recovery_planner": "recovery_planner"},
        )
        graph.add_edge("recovery_planner", "policy_gate")
        graph.add_edge("policy_gate", "report")
        graph.add_edge("report", END)
        return graph.compile()

    def _build_v4_graph(
        self,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        include_knowledge_investigator: bool = True,
    ) -> Any:
        graph = StateGraph(AiopsDiagnosticState)
        if include_knowledge_investigator:
            graph.add_node("knowledge_investigator", self._knowledge_investigator)
            graph.add_node("strategy_router", self._strategy_router)
            graph.add_node("investigator_dispatch", self._investigator_dispatch)
            graph.add_node("evidence_aggregator", self._evidence_aggregator)
        graph.add_node("planner", self._planner)
        graph.add_node("executor", self._executor)
        graph.add_node("fact_adapter", self._fact_adapter)
        graph.add_node("sufficiency_gate", self._sufficiency_gate_v4)
        graph.add_node("hypothesis_adjudicator", self._hypothesis_adjudicator)
        graph.add_node("replanner", self._replanner)
        graph.add_node("decision", self._decision_v4)
        graph.add_node("deterministic_validator", self._deterministic_validator_v4)
        graph.add_node("manual_review", self._manual_review_v4)
        graph.add_node("recovery_planner", self._recovery_planner)
        graph.add_node("validator_router", self._validator_router_v4)
        graph.add_node("llm_validator", self._llm_validator_v4)
        graph.add_node("policy_gate", self._policy_gate)
        graph.add_node("report", self._report)
        if include_knowledge_investigator:
            graph.add_edge(START, "knowledge_investigator")
            graph.add_edge("knowledge_investigator", "planner")
            graph.add_edge("planner", "strategy_router")
            graph.add_conditional_edges("strategy_router", self._route_after_strategy)
            graph.add_edge("investigator_dispatch", "evidence_aggregator")
            graph.add_conditional_edges(
                "evidence_aggregator",
                self._route_after_aggregation,
                {
                    "fact_adapter": "fact_adapter",
                    "executor": "executor",
                    "manual_review": "manual_review",
                },
            )
        else:
            graph.add_edge(START, "planner")
            graph.add_edge("planner", "executor")
        graph.add_edge("executor", "fact_adapter")
        graph.add_edge("fact_adapter", "sufficiency_gate")
        graph.add_conditional_edges(
            "sufficiency_gate",
            self._route_after_sufficiency,
            {
                "executor": (
                    "strategy_router"
                    if include_knowledge_investigator
                    else "executor"
                ),
                "replanner": "replanner",
                "hypothesis_adjudicator": "hypothesis_adjudicator",
                "decision": "decision",
            },
        )
        graph.add_edge("hypothesis_adjudicator", "sufficiency_gate")
        graph.add_conditional_edges(
            "replanner",
            self._route_after_replanner,
            {"executor": "executor", "decision": "decision"},
        )
        graph.add_edge("decision", "deterministic_validator")
        graph.add_conditional_edges(
            "deterministic_validator",
            self._route_after_deterministic_validation_v4,
            {
                "replanner": "replanner",
                "manual_review": "manual_review",
                "recovery_planner": "recovery_planner",
            },
        )
        graph.add_edge("manual_review", "policy_gate")
        graph.add_edge("recovery_planner", "validator_router")
        graph.add_conditional_edges(
            "validator_router",
            self._route_after_validator_router_v4,
            {"llm_validator": "llm_validator", "policy_gate": "policy_gate"},
        )
        graph.add_edge("llm_validator", "policy_gate")
        graph.add_edge("policy_gate", "report")
        graph.add_edge("report", END)
        return graph.compile(checkpointer=checkpointer)

    async def _run_knowledge_retrieval(
        self,
        state: AiopsDiagnosticState,
        *,
        actor: str,
    ) -> tuple[JsonDict, list[dict[str, object]]]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        query = str(state["query"])
        events = [
            _task_status_event(
                task_id,
                "running",
                f"{actor}: retrieving SOP evidence.",
                15,
            )
        ]
        retrieval_audit_id = _stable_public_id(
            "tool", task_id, "knowledge_retrieval", query
        )
        events.append(
            _tool_event(
                retrieval_audit_id,
                "knowledge_retrieval",
                "started",
                {"query": query},
            )
        )
        await self._create_audit(
            owner_user_id=owner_user_id,
            task_id=task_id,
            audit_id=retrieval_audit_id,
            tool_name="knowledge_retrieval",
            arguments={"query": query},
        )

        async def retrieve_operation() -> JsonDict:
            try:
                result = await self._retrieval_tool.run(
                    KnowledgeRetrievalToolInput(query=query, top_k=3),
                    owner_user_id=owner_user_id,
                    accessible_knowledge_base_ids=cast(
                        Sequence[str], state["accessible_knowledge_base_ids"]
                    ),
                )
            except KnowledgeRetrievalError as exc:
                return {
                    "retrievalAvailable": False,
                    "retrievalError": exc.message,
                    "sopHits": [],
                    "citations": [],
                    "noSopMatched": True,
                }
            sop_hits = [_sop_hit_payload(hit) for hit in result.results]
            return {
                "retrievalAvailable": True,
                "retrievalError": None,
                "sopHits": sop_hits,
                "citations": [
                    _citation_payload(citation) for citation in result.citations
                ],
                "noSopMatched": not sop_hits,
            }

        graph_version = str(state.get("graph_version") or AIOPS_LEGACY_GRAPH_VERSION)
        if (
            graph_version == AIOPS_GRAPH_VERSION
            and self._repositories.aiops_runtime is not None
        ):
            execution_repository = self._repositories.aiops_runtime.execution_repository(
                owner_user_id=owner_user_id,
                task_id=task_id,
                graph_version=graph_version,
            )
            coordinated = await ExecutionCoordinator(
                execution_repository,
                worker_id=f"diagnostic-service-{id(self)}",
            ).run_once(
                ExecutionIdentity(
                    task_id=task_id,
                    graph_version=graph_version,
                    node_name="knowledge_investigator",
                    logical_iteration=0,
                    input_payload={
                        "query": query,
                        "accessibleKnowledgeBaseIds": sorted(
                            cast(
                                Sequence[str],
                                state["accessible_knowledge_base_ids"],
                            )
                        ),
                    },
                ),
                retrieve_operation,
            )
            context = dict(coordinated.output)
        else:
            context = await retrieve_operation()

        citations = _json_list(context.get("citations"))
        if context.get("retrievalAvailable") is True:
            retrieval_payload: JsonDict = {
                "query": query,
                "results": _json_list(context.get("sopHits")),
                "citations": citations,
            }
            events.append(
                _tool_event(
                    retrieval_audit_id,
                    "knowledge_retrieval",
                    "completed",
                    retrieval_payload,
                )
            )
            await self._finalize_audit(
                owner_user_id=owner_user_id,
                audit_id=retrieval_audit_id,
                status="completed",
                result_summary=_bounded_json(retrieval_payload),
            )
            events.extend(_reference_event_from_payload(item) for item in citations)
        else:
            safe_error = str(
                context.get("retrievalError")
                or "Knowledge retrieval was unavailable."
            )
            context["retrievalError"] = safe_error
            events.extend(
                [
                    _tool_event(
                        retrieval_audit_id,
                        "knowledge_retrieval",
                        "failed",
                        {"error": safe_error},
                    ),
                    _error_event("SYSTEM_UNAVAILABLE"),
                ]
            )
            await self._finalize_audit(
                owner_user_id=owner_user_id,
                audit_id=retrieval_audit_id,
                status="failed",
                error_message=safe_error,
            )
        if context.get("noSopMatched") is True:
            events.append(
                _task_status_event(
                    task_id,
                    "running",
                    f"{actor}: no SOP matched; using a generic evidence-gathering plan.",
                    25,
                )
            )
        return context, events

    async def _knowledge_investigator(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        context, events = await self._run_knowledge_retrieval(
            state,
            actor="Knowledge Investigator",
        )
        step_payload: JsonDict = {
            "workflowVersion": str(
                state.get("workflow_version") or "evidence-driven-v4"
            ),
            "graphVersion": str(state.get("graph_version") or AIOPS_GRAPH_VERSION),
            "retrievalAvailable": context.get("retrievalAvailable") is True,
            "retrievalError": context.get("retrievalError"),
            "sopHits": _json_list(context.get("sopHits")),
            "noSopMatched": context.get("noSopMatched") is True,
        }
        step = await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="knowledge_investigator",
            status="completed",
            payload=step_payload,
        )
        persisted_evidence_ids: list[str] = []
        for citation_payload in _json_list(context.get("citations")):
            citation_id = str(citation_payload.get("id") or "")
            if not citation_id:
                continue
            evidence_record = await self._repositories.diagnostics.create_evidence(
                owner_user_id=owner_user_id,
                evidence_id=_stable_public_id(
                    "evidence", task_id, "knowledge_reference", citation_id
                ),
                task_id=task_id,
                step_id=step.id,
                kind="knowledge_reference",
                source=str(citation_payload.get("source") or "knowledge_retrieval"),
                summary=str(citation_payload.get("title") or "Knowledge reference"),
                payload=citation_payload,
            )
            persisted_evidence_ids.append(evidence_record.id)
        retrieval_error = context.get("retrievalError")
        if context.get("retrievalAvailable") is not True and isinstance(
            retrieval_error, str
        ):
            evidence_record = await self._repositories.diagnostics.create_evidence(
                owner_user_id=owner_user_id,
                evidence_id=_stable_public_id(
                    "evidence", task_id, "knowledge_retrieval_error"
                ),
                task_id=task_id,
                step_id=step.id,
                kind="knowledge_reference",
                source="knowledge_retrieval",
                summary=retrieval_error,
                payload={"error": retrieval_error},
            )
            persisted_evidence_ids.append(evidence_record.id)
        await self._save_checkpoint(state, "knowledge_investigator", step_payload)
        public_context: JsonDict = {
            "retrievalAvailable": context.get("retrievalAvailable") is True,
            "retrievalError": context.get("retrievalError"),
            "sopHits": _json_list(context.get("sopHits")),
            "noSopMatched": context.get("noSopMatched") is True,
        }
        return {
            "knowledge_context": public_context,
            "knowledge_evidence_ids": persisted_evidence_ids,
            "knowledge_completed": True,
            "evidence_ids": persisted_evidence_ids,
            "events": events,
        }

    async def _planner(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        query = str(state["query"])
        knowledge_completed = state.get("knowledge_completed") is True
        if knowledge_completed:
            knowledge_context = _json_dict(state.get("knowledge_context"))
            events = [
                _task_status_event(
                    task_id,
                    "running",
                    "Planner: creating a plan from collected knowledge.",
                    25,
                )
            ]
        else:
            knowledge_context, events = await self._run_knowledge_retrieval(
                state,
                actor="Planner",
            )
        sop_hits = _json_list(knowledge_context.get("sopHits"))
        no_sop_matched = knowledge_context.get("noSopMatched") is True
        retrieval_available = knowledge_context.get("retrievalAvailable") is True
        retrieval_error_value = knowledge_context.get("retrievalError")
        retrieval_error = (
            retrieval_error_value if isinstance(retrieval_error_value, str) else None
        )
        citation_payloads = _json_list(knowledge_context.get("citations"))

        try:
            mcp_client = await self._mcp_client_for(owner_user_id)
            discovered_tools = constrain_tool_definitions(
                await mcp_client.discover_tools(),
                self._tool_argument_contracts,
            )
        except McpClientError:
            discovered_tools = []
            events.append(
                _task_status_event(
                    task_id,
                    "running",
                    "Planner: MCP discovery was unavailable; execution will report an "
                    "explicit failure.",
                    30,
                )
            )

        model_runtime = (
            self._model_runtime(state)
            if state.get("workflow_version") == "evidence-driven-v4"
            else None
        )
        diagnostic_tools = [
            item for item in discovered_tools if item.name not in self._tool_policies
        ]
        known_hypotheses = [
            str(item.get("id"))
            for item in cast(list[JsonDict], state.get("public_hypotheses") or [])
            if item.get("id")
        ]

        async def create_plan_operation() -> JsonDict:
            created_plan, created_origin = await self._create_plan(
                query=query,
                alert=_json_dict(state.get("alert")),
                sop_hits=sop_hits,
                no_sop_matched=no_sop_matched,
                tool_definitions=diagnostic_tools,
                known_hypotheses=known_hypotheses,
                model_runtime=model_runtime,
            )
            return {
                "plan": created_plan,
                "planOrigin": created_origin,
                "modelCallCount": model_runtime.budget.used if model_runtime else 0,
                "modelCallAudits": model_runtime.audits if model_runtime else [],
            }

        graph_version = str(state.get("graph_version") or AIOPS_LEGACY_GRAPH_VERSION)
        if model_runtime is not None and self._repositories.aiops_runtime is not None:
            execution_repository = self._repositories.aiops_runtime.execution_repository(
                owner_user_id=owner_user_id,
                task_id=task_id,
                graph_version=graph_version,
            )
            coordinated_plan = await ExecutionCoordinator(
                execution_repository,
                worker_id=f"diagnostic-service-{id(self)}",
            ).run_once(
                ExecutionIdentity(
                    task_id=task_id,
                    graph_version=graph_version,
                    node_name="planner",
                    logical_iteration=0,
                    input_payload={
                        "query": query,
                        "alert": _json_dict(state.get("alert")),
                        "sopHits": sop_hits,
                        "toolContracts": _tool_contracts_payload(diagnostic_tools),
                        "knownHypotheses": known_hypotheses,
                    },
                ),
                create_plan_operation,
            )
            plan = _json_list(coordinated_plan.output.get("plan"))
            plan_origin = str(coordinated_plan.output.get("planOrigin") or "generic")
            persisted_count = coordinated_plan.output.get("modelCallCount")
            if isinstance(persisted_count, int) and not isinstance(persisted_count, bool):
                model_runtime.budget.used = persisted_count
            model_runtime.audits = _json_list(
                coordinated_plan.output.get("modelCallAudits")
            )
        else:
            created = await create_plan_operation()
            plan = _json_list(created.get("plan"))
            plan_origin = str(created.get("planOrigin") or "generic")
        investigator_capabilities = build_investigator_capabilities(
            discovered_tools=discovered_tools,
            trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
            tool_policies=self._tool_policies,
            retrieval_available=retrieval_available,
            cls_available=any(
                item.name in {"SearchLog", "SearchLogs"}
                for item in discovered_tools
            ),
        )
        plan = normalize_plan_source_domains(plan, investigator_capabilities)
        events.append(
            _task_status_event(
                task_id,
                "running",
                f"Planner: created a {plan_origin} plan with {len(plan)} step(s).",
                35,
            )
        )
        planner_payload: JsonDict = {
            "workflowVersion": str(
                state.get("workflow_version") or "evidence-driven-v3"
            ),
            "graphVersion": graph_version,
            "noSopMatched": no_sop_matched,
            "sopHits": sop_hits,
            "plan": plan,
            "planOrigin": plan_origin,
            "retrievalError": retrieval_error,
            **_plan_causal_coverage_payload(plan),
        }
        if model_runtime is not None:
            planner_payload["modelCallCount"] = model_runtime.budget.used
            planner_payload["modelCallAudits"] = model_runtime.audits
        planner_step = await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="planner",
            status="completed",
            payload=planner_payload,
        )
        persisted_evidence_ids: list[str] = []
        if not knowledge_completed and retrieval_available:
            for citation_payload in citation_payloads:
                citation_id = str(citation_payload.get("id") or "")
                if not citation_id:
                    continue
                evidence_record = await self._repositories.diagnostics.create_evidence(
                    owner_user_id=owner_user_id,
                    evidence_id=_stable_public_id(
                        "evidence", task_id, "knowledge_reference", citation_id
                    ),
                    task_id=task_id,
                    step_id=planner_step.id,
                    kind="knowledge_reference",
                    source=str(citation_payload["source"]),
                    summary=str(citation_payload["title"]),
                    payload=citation_payload,
                )
                persisted_evidence_ids.append(evidence_record.id)
        elif not knowledge_completed and retrieval_error is not None:
            evidence_record = await self._repositories.diagnostics.create_evidence(
                owner_user_id=owner_user_id,
                evidence_id=_stable_public_id(
                    "evidence", task_id, "knowledge_retrieval_error"
                ),
                task_id=task_id,
                step_id=planner_step.id,
                kind="knowledge_reference",
                source="knowledge_retrieval",
                summary=retrieval_error,
                payload={"error": retrieval_error},
            )
            persisted_evidence_ids.append(evidence_record.id)
        await self._save_checkpoint(
            state,
            "planner",
            planner_payload,
        )
        update: dict[str, object] = {
            "sop_hits": sop_hits,
            "no_sop_matched": no_sop_matched,
            "plan": plan,
            "plan_origin": plan_origin,
            "tool_definitions": tuple(discovered_tools),
            "hypothesis_states": cast(
                list[JsonDict], state.get("hypothesis_states") or []
            ),
            "evidence_ids": persisted_evidence_ids,
            "events": events,
        }
        if model_runtime is not None:
            update["model_call_count"] = model_runtime.budget.used
            update["model_call_audits"] = model_runtime.audits
        return update

    async def _strategy_router(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        plan = cast(list[JsonDict], state.get("plan") or [])
        plan_index = int(state.get("plan_index") or 0)
        routing_plan = plan[plan_index:]
        discovered_tools = tuple(state.get("tool_definitions") or ())
        capabilities = build_investigator_capabilities(
            discovered_tools=discovered_tools,
            trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
            tool_policies=self._tool_policies,
            retrieval_available=state.get("knowledge_completed") is True,
            cls_available=any(
                item.name in {"SearchLog", "SearchLogs"}
                for item in discovered_tools
            ),
        )
        required_domains = frozenset(
            cast(Any, str(step.get("sourceDomain")))
            for step in routing_plan
            if step.get("sourceDomain") in {"knowledge", "runtime", "log", "change"}
        )
        causal_roles = {
            str(step.get("causalIntent"))
            for step in routing_plan
            if step.get("causalIntent") in {"trigger", "mechanism", "impact"}
        }
        evidence_ids = tuple(sorted(set(state.get("evidence_ids") or [])))
        evidence_snapshot_hash = hashlib.sha256(
            "\x1f".join(evidence_ids).encode("utf-8")
        ).hexdigest()
        assessments = cast(
            list[JsonDict], state.get("hypothesis_assessments") or []
        )
        remaining_time_ms = 480_000
        hard_deadline = state.get("hard_deadline_at")
        if hard_deadline:
            remaining_time_ms = max(
                0,
                int(
                    (
                        datetime.fromisoformat(str(hard_deadline)) - _now()
                    ).total_seconds()
                    * 1_000
                ),
            )
        soft_deadline = state.get("soft_deadline_at")
        if soft_deadline:
            remaining_time_ms = min(
                remaining_time_ms,
                max(
                    0,
                    int(
                        (
                            datetime.fromisoformat(str(soft_deadline)) - _now()
                        ).total_seconds()
                        * 1_000
                    ),
                ),
            )
        routing_input = InvestigationRoutingInput(
            required_domains=required_domains,
            unresolved_hypothesis_count=sum(
                1
                for item in assessments
                if item.get("disposition") in {None, "unresolved"}
            ),
            causal_component_count=max(
                1,
                len(
                    {
                        str(step.get("targetComponent"))
                        for step in routing_plan
                        if step.get("targetComponent")
                    }
                ),
            ),
            missing_causal_roles=frozenset(
                {"trigger", "mechanism", "impact"} - causal_roles
            ),
            high_quality_conflict=any(
                item.get("hasHighQualityConflict") is True for item in assessments
            ),
            severity=str(_json_dict(state.get("alert")).get("severity") or "warning"),
            trusted_pattern_matched=state.get("root_cause_decision") is not None,
            decision_ready=(
                _json_dict(state.get("evidence_sufficiency")).get("status")
                == "sufficient"
            ),
            valid_tool_calls_without_gain=min(plan_index, 2),
            knowledge_hit=bool(state.get("sop_hits")),
            remaining_time_ms=remaining_time_ms,
            remaining_model_calls=max(0, 8 - int(state.get("model_call_count") or 0)),
            completed_dispatch_keys=frozenset(),
            evidence_snapshot_hash=evidence_snapshot_hash,
            wave=int(state.get("investigation_wave") or 0),
        )
        mode = cast(StrategyMode, state.get("investigation_strategy_mode") or "auto")
        effective_mode: StrategyMode = (
            "single"
            if mode == "auto"
            and plan_index
            < self._investigation_router_policy.single_agent_max_initial_steps
            else mode
        )
        route = route_investigation(
            routing_input,
            capabilities=capabilities,
            policy=self._investigation_router_policy,
            mode=effective_mode,
        )
        dispatches = build_investigation_dispatches(
            task_id=str(state["task_id"]),
            owner_user_id=str(state["owner_user_id"]),
            plan=routing_plan,
            capabilities=capabilities,
            selected_investigators=route.selected_investigators,
            policy_version=route.policy_version,
            evidence_snapshot_hash=evidence_snapshot_hash,
            existing_evidence_ids=evidence_ids,
            deadline_ms=self._investigation_router_policy.investigator_deadline_ms,
            model_call_budget=0,
            missing_causal_roles=tuple(routing_input.missing_causal_roles),
        )
        route_payload: JsonDict = {
            "strategy": route.strategy,
            "score": route.score,
            "escalationWatch": route.escalation_watch,
            "selectedInvestigators": list(route.selected_investigators),
            "rejectedInvestigators": dict(route.rejected_investigators),
            "reasonCodes": list(route.reason_codes),
            "policyVersion": route.policy_version,
            "mode": mode,
            "wave": routing_input.wave,
        }
        dispatch_payloads = [_investigation_dispatch_payload(item) for item in dispatches]
        step_payload: JsonDict = {
            "workflowVersion": str(state.get("workflow_version") or "evidence-driven-v4"),
            "graphVersion": str(state.get("graph_version") or AIOPS_GRAPH_VERSION),
            "route": route_payload,
            "dispatches": dispatch_payloads,
        }
        await self._create_step(
            owner_user_id=str(state["owner_user_id"]),
            task_id=str(state["task_id"]),
            phase="strategy_router",
            status="completed",
            payload=step_payload,
        )
        await self._save_checkpoint(state, "strategy_router", step_payload)
        return {
            "investigation_route": route_payload,
            "investigation_dispatches": dispatch_payloads,
            "investigation_wave": (
                routing_input.wave + 1
                if route.strategy == "multi_agent"
                else routing_input.wave
            ),
            "events": [
                _task_status_event(
                    str(state["task_id"]),
                    "running",
                    f"Strategy Router selected {route.strategy}.",
                    38,
                )
            ],
        }

    def _route_after_strategy(
        self, state: AiopsDiagnosticState
    ) -> str | list[Send]:
        strategy = _json_dict(state.get("investigation_route")).get("strategy")
        if strategy == "deterministic_fast_path":
            return "sufficiency_gate"
        if strategy != "multi_agent":
            return "executor"
        dispatches = sorted(
            _json_list(state.get("investigation_dispatches")),
            key=lambda item: (
                {"runtime": 0, "log": 1}.get(str(item.get("investigatorType")), 9),
                str(item.get("dispatchId") or ""),
            ),
        )
        sends: list[Send] = []
        for dispatch in dispatches:
            branch_state = dict(state)
            branch_state["investigation_dispatch"] = dispatch
            branch_state["investigation_packets"] = []
            sends.append(Send("investigator_dispatch", branch_state))
        return sends or "executor"

    async def _investigator_dispatch(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        dispatch = _json_dict(state.get("investigation_dispatch"))
        steps = _json_list(dispatch.get("steps"))
        claims: list[EvidenceClaim] = []
        tool_call_ids: list[str] = []
        branch_events: list[dict[str, object]] = []
        failed = False
        for step in steps:
            local_state = cast(
                AiopsDiagnosticState,
                {
                    **state,
                    "plan": [step],
                    "plan_index": 0,
                    "executor_attempt_count": 0,
                    "executed_step_fingerprints": [],
                },
            )
            update = await self._executor(local_state)
            branch_events.extend(
                cast(list[dict[str, object]], update.get("events") or [])
            )
            evidence_id = str(update.get("current_evidence_id") or "")
            tool_name = str(step.get("tool") or "unknown")
            fingerprint = _step_fingerprint(step)
            if update.get("execution_failed") is True or not evidence_id:
                failed = True
                continue
            tool_call_ids.append(
                _stable_public_id(
                    "tool",
                    str(state["task_id"]),
                    str(step.get("id") or "step_1"),
                    tool_name,
                    fingerprint,
                )
            )
            causal_value = step.get("causalIntent")
            claims.append(
                EvidenceClaim(
                    claim_id=f"{tool_name}.observation",
                    value=cast(Any, update.get("current_tool_output")),
                    quality="direct",
                    causal_role=(
                        str(causal_value)
                        if causal_value in {"trigger", "mechanism", "impact"}
                        else None
                    ),
                    supports=(),
                    refutes=(),
                    evidence_ids=(evidence_id,),
                    target_component=str(
                        step.get("targetComponent") or tool_name
                    ),
                    observed_at=_now(),
                    time_scope="incident_window",
                )
            )
        packet = EvidencePacket(
            task_id=str(state["task_id"]),
            owner_user_id=str(state["owner_user_id"]),
            dispatch_id=str(dispatch.get("dispatchId") or "dispatch_invalid"),
            investigator_type=cast(Any, dispatch.get("investigatorType")),
            status=("inconclusive" if claims and failed else "completed" if claims else "failed"),
            claims=tuple(claims),
            limitations=(("investigator_execution_failed",) if failed else ()),
            tool_call_ids=tuple(tool_call_ids),
            model_calls_used=0,
        )
        return {
            "investigation_packets": [_evidence_packet_payload(packet)],
            "events": branch_events,
        }

    async def _evidence_aggregator(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        if state.get("root_cause_decision") is not None or (
            _json_dict(state.get("evidence_sufficiency")).get("status")
            == "sufficient"
        ):
            return {
                "aggregated_facts": [],
                "investigation_aggregation": {
                    "completedPacketCount": 0,
                    "failedPacketCount": 0,
                    "fallbackPermitted": False,
                    "lateResultIgnored": True,
                    "fallbackReason": "late_result_ignored",
                },
                "events": [
                    _task_status_event(
                        str(state["task_id"]),
                        "running",
                        "Late Investigator result ignored after decision readiness.",
                        70,
                    )
                ],
            }
        packets = tuple(
            _evidence_packet_from_payload(item)
            for item in _json_list(state.get("investigation_packets"))
        )
        evidence_records = await self._repositories.diagnostics.list_evidence(
            owner_user_id=str(state["owner_user_id"]),
            task_id=str(state["task_id"]),
        )
        audit_repository = self._repositories.tool_call_audits
        audits = (
            await audit_repository.list_for_diagnostic_task(
                owner_user_id=str(state["owner_user_id"]),
                diagnostic_task_id=str(state["task_id"]),
            )
            if audit_repository is not None
            else []
        )
        dispatches = _json_list(state.get("investigation_dispatches"))
        allowed_by_type: dict[Any, frozenset[str]] = {
            "runtime": frozenset(),
            "log": frozenset(),
            "knowledge": frozenset({"knowledge_retrieval"}),
            "change": frozenset(),
        }
        investigator_by_dispatch: dict[str, Any] = {}
        for dispatch in dispatches:
            investigator = str(dispatch.get("investigatorType") or "")
            investigator_by_dispatch[str(dispatch.get("dispatchId") or "")] = investigator
            allowed_by_type[investigator] = frozenset(
                str(item) for item in cast(list[object], dispatch.get("allowedTools") or [])
            )
        result = aggregate_evidence_packets(
            packets,
            context=AggregationContext(
                owner_user_id=str(state["owner_user_id"]),
                task_id=str(state["task_id"]),
                investigator_by_dispatch=cast(Any, investigator_by_dispatch),
                evidence_ids=frozenset(item.id for item in evidence_records),
                completed_tool_call_ids=frozenset(
                    item.id for item in audits if item.status == "completed"
                ),
                tool_name_by_call_id={item.id: item.tool_name for item in audits},
                tool_call_id_by_evidence_id={
                    item.id: str(item.tool_call_id or "") for item in evidence_records
                },
                allowed_tools_by_investigator=cast(Any, allowed_by_type),
                maximum_quality_by_evidence_id={
                    item.id: (
                        "reference" if item.kind == "knowledge_reference" else "direct"
                    )
                    for item in evidence_records
                },
            ),
        )
        facts: list[JsonDict] = []
        for claim in result.claims:
            for evidence_id in claim.evidence_ids:
                facts.append(
                    {
                        "key": claim.claim_id,
                        "value": _safe_value(claim.value),
                        "evidenceId": evidence_id,
                        "sourceTool": claim.claim_id.split(".", 1)[0],
                        "quality": claim.quality,
                        "public": True,
                        "causalRole": claim.causal_role,
                        "targetComponent": claim.target_component,
                        "timeScope": claim.time_scope,
                    }
                )
        completed_packet_count = sum(
            1
            for packet in result.accepted_packets
            if packet.status in {"completed", "inconclusive"} and packet.claims
        )
        failed_packet_count = len(packets) - completed_packet_count
        remaining_plan = int(state.get("plan_index") or 0) < len(
            cast(list[JsonDict], state.get("plan") or [])
        )
        fallback_permitted = (
            completed_packet_count == 0
            and remaining_plan
            and int(state.get("model_call_count") or 0) <= 6
            and not _execution_deadlines_from_state(state).hard_expired()
        )
        aggregation_payload: JsonDict = {
            "completedPacketCount": completed_packet_count,
            "failedPacketCount": failed_packet_count,
            "rejectedDispatches": dict(result.rejected_dispatches),
            "fallbackPermitted": fallback_permitted,
            "fallbackReason": (
                "fallback_to_single_agent"
                if fallback_permitted
                else "manual_review_required"
                if completed_packet_count == 0
                else None
            ),
        }
        audit_payload: JsonDict = {
            "workflowVersion": str(
                state.get("workflow_version") or "evidence-driven-v4"
            ),
            "graphVersion": str(state.get("graph_version") or AIOPS_GRAPH_VERSION),
            "packetStatuses": [
                packet.status
                for packet in sorted(
                    packets,
                    key=lambda item: (item.investigator_type, item.dispatch_id),
                )
            ],
            **aggregation_payload,
        }
        await self._create_step(
            owner_user_id=str(state["owner_user_id"]),
            task_id=str(state["task_id"]),
            phase="evidence_aggregator",
            status="completed",
            payload=audit_payload,
        )
        await self._save_checkpoint(state, "evidence_aggregator", audit_payload)
        return {
            "aggregated_facts": facts,
            "investigation_aggregation": aggregation_payload,
            "evidence_ids": sorted(
                {
                    evidence_id
                    for claim in result.claims
                    for evidence_id in claim.evidence_ids
                }
            ),
            "events": [
                _task_status_event(
                    str(state["task_id"]),
                    "running",
                    "Evidence Aggregator validated Investigator packets.",
                    55,
                )
            ],
        }

    def _route_after_aggregation(self, state: AiopsDiagnosticState) -> str:
        aggregation = _json_dict(state.get("investigation_aggregation"))
        if aggregation.get("lateResultIgnored") is True:
            return "fact_adapter"
        if int(cast(Any, aggregation.get("completedPacketCount") or 0)) > 0:
            return "fact_adapter"
        if aggregation.get("fallbackPermitted") is True:
            return "executor"
        return "manual_review"

    async def _executor(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        plan = cast(list[JsonDict], state.get("plan") or [])
        plan_index = int(state.get("plan_index") or 0)
        attempt_count = int(state.get("executor_attempt_count") or 0)
        max_total_steps = int(state.get("max_total_steps") or 6)
        if (
            state.get("workflow_version") == "evidence-driven-v4"
            and _execution_deadlines_from_state(state).hard_expired()
        ):
            return {
                "plan_index": len(plan),
                "current_evidence_id": "",
                "termination_reason": "hard_deadline_exceeded",
                "events": [],
            }
        if attempt_count >= max_total_steps:
            return {
                "current_evidence_id": "",
                "termination_reason": "step_budget_exhausted",
                "events": [],
            }
        if plan_index >= len(plan):
            return {"current_evidence_id": "", "events": []}

        source_step = plan[plan_index]
        normalized_steps, contract_errors = normalize_tool_plan_steps(
            [source_step],
            trusted_tool_arguments=self._trusted_tool_arguments,
            tool_argument_contracts=self._tool_argument_contracts,
            tool_definitions=tuple(state.get("tool_definitions") or ()),
        )
        if contract_errors or not normalized_steps:
            error = (
                contract_errors[0]
                if contract_errors
                else ToolArgumentContractError(
                    code="schema_mismatch",
                    tool_name=str(source_step.get("tool") or "unknown"),
                )
            )
            tool_name = str(source_step.get("tool") or "unknown")
            fingerprint = _step_fingerprint(source_step)
            payload: JsonDict = {
                "planStepId": str(source_step.get("id") or ""),
                "tool": tool_name,
                "errorCategory": "invalid_arguments",
                "contractCode": error.code,
            }
            await self._create_step(
                owner_user_id=owner_user_id,
                task_id=task_id,
                phase="executor",
                status="failed",
                payload=payload,
            )
            await self._save_checkpoint(state, "executor", payload)
            return {
                "plan_index": plan_index + 1,
                "executor_attempt_count": attempt_count + 1,
                "executed_step_fingerprints": [fingerprint],
                "current_evidence_id": "",
                "current_plan_step": {
                    "id": str(source_step.get("id") or ""),
                    "tool": tool_name,
                },
                "events": [
                    _task_status_event(
                        task_id,
                        "running",
                        "Executor: rejected invalid diagnostic tool arguments.",
                        55,
                    )
                ],
            }

        step = normalized_steps[0]
        tool_name = str(step.get("tool") or "")
        arguments = _json_dict(step.get("arguments"))
        fingerprint = _step_fingerprint(step)
        if fingerprint in set(state.get("executed_step_fingerprints") or []):
            payload: JsonDict = {
                "planStepId": str(step.get("id") or ""),
                "tool": tool_name,
                "errorCategory": "duplicate_step",
            }
            await self._create_step(
                owner_user_id=owner_user_id,
                task_id=task_id,
                phase="executor",
                status="failed",
                payload=payload,
            )
            await self._save_checkpoint(state, "executor", payload)
            return {
                "plan_index": plan_index + 1,
                "executor_attempt_count": attempt_count,
                "executed_step_fingerprints": [fingerprint],
                "current_evidence_id": "",
                "current_plan_step": step,
                "events": [
                    _task_status_event(
                        task_id,
                        "running",
                        "Executor: rejected a duplicate diagnostic step.",
                        55,
                    )
                ],
            }
        plan_step_id = str(step.get("id") or f"step_{plan_index + 1}")
        audit_id = _stable_public_id(
            "tool", task_id, plan_step_id, tool_name, fingerprint
        )
        events = [
            _task_status_event(
                task_id,
                "running",
                f"Executor: running step {plan_index + 1} with {tool_name}.",
                45 + min(plan_index * 15, 30),
            ),
            _tool_event(audit_id, tool_name, "started", arguments),
        ]
        await self._create_audit(
            owner_user_id=owner_user_id,
            task_id=task_id,
            audit_id=audit_id,
            tool_name=tool_name,
            arguments=arguments,
        )

        tool_cache_hit = False

        async def invoke_tool() -> JsonDict:
            if tool_name == "knowledge_retrieval":
                result = await self._retrieval_tool.run(
                    KnowledgeRetrievalToolInput(
                        query=str(arguments.get("query") or state["query"]),
                        top_k=_optional_int(arguments.get("topK")),
                    ),
                    owner_user_id=owner_user_id,
                    accessible_knowledge_base_ids=cast(
                        Sequence[str], state["accessible_knowledge_base_ids"]
                    ),
                )
                raw_output: object = {
                    "results": [_sop_hit_payload(hit) for hit in result.results],
                    "citations": [
                        _citation_payload(citation) for citation in result.citations
                    ],
                }
            elif tool_name:
                mcp_client = await self._mcp_client_for(owner_user_id)
                raw_output = await mcp_client.call_tool(tool_name, arguments)
            else:
                raise ValueError("Diagnostic plan did not specify a tool.")
            return {"output": _safe_value(raw_output)}

        service = self
        cache_state = {"hit": False}

        class _SingleAgentToolRuntime:
            async def execute_prepared(
                self,
                request: DiagnosticToolExecutionRequest,
                prepared: PreparedDiagnosticToolExecution,
            ) -> DiagnosticToolExecutionResult:
                del self, prepared
                if (
                    state.get("workflow_version") == "evidence-driven-v4"
                    and service._repositories.aiops_runtime is not None
                ):
                    execution_repository = (
                        service._repositories.aiops_runtime.execution_repository(
                            owner_user_id=request.owner_user_id,
                            task_id=request.task_id,
                            graph_version=request.graph_version,
                        )
                    )
                    coordinated = await ExecutionCoordinator(
                        execution_repository,
                        worker_id=f"diagnostic-service-{id(service)}",
                    ).run_once(
                        ExecutionIdentity(
                            task_id=request.task_id,
                            graph_version=request.graph_version,
                            node_name=f"tool:{tool_name}",
                            logical_iteration=request.logical_iteration,
                            input_payload={
                                "tool": tool_name,
                                "arguments": arguments,
                            },
                            execution_kind="tool",
                        ),
                        invoke_tool,
                    )
                    runtime_output = coordinated.output.get("output")
                    cache_state["hit"] = coordinated.cache_hit
                else:
                    runtime_output = (await invoke_tool())["output"]
                return DiagnosticToolExecutionResult(
                    status="completed",
                    evidence_id=evidence_id,
                    tool_call_id=audit_id,
                    safe_output=runtime_output,
                    safe_summary=_tool_result_summary(tool_name, runtime_output),
                    events=(),
                )

        evidence_id = _stable_public_id(
            "evidence", task_id, plan_step_id, tool_name, fingerprint
        )
        allowed_tools = frozenset(
            {
                definition.name
                for definition in tuple(state.get("tool_definitions") or ())
                if definition.name not in self._tool_policies
            }
            | {"knowledge_retrieval"}
        )
        try:
            shared_result = await execute_diagnostic_tool(
                DiagnosticToolExecutionRequest(
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    graph_version=str(
                        state.get("graph_version") or AIOPS_LEGACY_GRAPH_VERSION
                    ),
                    plan_step=step,
                    logical_iteration=plan_index,
                    allowed_tools=allowed_tools,
                ),
                runtime=_SingleAgentToolRuntime(),
            )
            output = shared_result.safe_output
            tool_cache_hit = cache_state["hit"]
        except Exception as exc:
            safe_error = _safe_error(exc)
            evidence: JsonDict = {
                "stepId": str(step.get("id") or f"step_{plan_index + 1}"),
                "tool": tool_name or "unknown",
                "status": "failed",
                "summary": safe_error,
            }
            events.extend(
                [
                    _tool_event(audit_id, tool_name or "unknown", "failed", {"error": safe_error}),
                    _error_event("SYSTEM_UNAVAILABLE"),
                ]
            )
            await self._finalize_audit(
                owner_user_id=owner_user_id,
                audit_id=audit_id,
                status="failed",
                error_message=safe_error,
            )
            executor_step = await self._create_step(
                owner_user_id=owner_user_id,
                task_id=task_id,
                phase="executor",
                status="failed",
                payload={"planStep": step, "tool": tool_name, "error": safe_error},
            )
            evidence_record = await self._repositories.diagnostics.create_evidence(
                owner_user_id=owner_user_id,
                evidence_id=_stable_public_id(
                    "evidence", task_id, plan_step_id, tool_name, fingerprint
                ),
                task_id=task_id,
                step_id=executor_step.id,
                tool_call_id=audit_id,
                kind=_evidence_kind_for_tool(tool_name),
                source=tool_name or "unknown",
                summary=safe_error,
                payload={"error": safe_error, "arguments": arguments},
            )
            evidence["evidenceId"] = evidence_record.id
            await self._save_checkpoint(state, "executor", {"evidence": evidence})
            return {
                "plan_index": plan_index + 1,
                "executor_attempt_count": attempt_count + 1,
                "executed_step_fingerprints": [fingerprint],
                "execution_failed": True,
                "evidence": [evidence],
                "evidence_ids": [evidence_record.id],
                "current_evidence_id": evidence_record.id,
                "current_evidence_summary": safe_error,
                "current_tool_output": {"error": safe_error},
                "current_plan_step": step,
                "events": events,
            }

        summary = _tool_result_summary(tool_name, output)
        evidence: JsonDict = {
            "stepId": str(step.get("id") or f"step_{plan_index + 1}"),
            "tool": tool_name,
            "status": "completed",
            "summary": summary,
            "cacheHit": tool_cache_hit,
        }
        events.append(_tool_event(audit_id, tool_name, "completed", _safe_value(output)))
        if tool_name == "knowledge_retrieval" and isinstance(output, Mapping):
            citations = output.get("citations")
            if isinstance(citations, list):
                events.extend(_reference_event_from_payload(citation) for citation in citations)
        else:
            events.append(
                _sse_event(
                    "reference.source",
                    {
                        "reference": {
                            "id": audit_id,
                            "title": f"{tool_name} result",
                            "sourceType": "log",
                            "metadata": {"stepId": evidence["stepId"], "tool": tool_name},
                        }
                    },
                )
            )
        await self._finalize_audit(
            owner_user_id=owner_user_id,
            audit_id=audit_id,
            status="completed",
            result_summary=summary,
        )
        executor_step = await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="executor",
            status="completed",
            payload={"planStep": step, "tool": tool_name, "output": _safe_value(output)},
        )
        evidence_record = await self._repositories.diagnostics.create_evidence(
            owner_user_id=owner_user_id,
            evidence_id=_stable_public_id(
                "evidence", task_id, plan_step_id, tool_name, fingerprint
            ),
            task_id=task_id,
            step_id=executor_step.id,
            tool_call_id=audit_id,
            kind=_evidence_kind_for_tool(tool_name),
            source=tool_name,
            summary=summary,
            payload={"arguments": arguments, "output": _safe_value(output)},
        )
        evidence["evidenceId"] = evidence_record.id
        await self._save_checkpoint(state, "executor", {"evidence": evidence})
        update: dict[str, object] = {
            "plan_index": plan_index + 1,
            "executor_attempt_count": attempt_count + 1,
            "executed_step_fingerprints": [fingerprint],
            "evidence": [evidence],
            "evidence_ids": [evidence_record.id],
            "current_evidence_id": evidence_record.id,
            "current_evidence_summary": summary,
            "current_tool_output": _safe_value(output),
            "current_plan_step": step,
            "events": events,
        }
        return update

    async def _fact_adapter(self, state: AiopsDiagnosticState) -> dict[str, object]:
        """Convert one bounded public tool result into facts and four-state assessments."""
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        evidence_id = str(state.get("current_evidence_id") or "")
        current_step = _json_dict(state.get("current_plan_step"))
        output = state.get("current_tool_output")
        trusted_evidence_ids = {
            value
            for value in cast(list[object], state.get("evidence_ids") or [])
            if isinstance(value, str) and value
        }
        if evidence_id:
            trusted_evidence_ids.add(evidence_id)
        prior_facts = tuple(
            fact
            for fact in _diagnostic_facts_from_payload(
                cast(list[JsonDict], state.get("diagnostic_facts") or [])
            )
            if fact.evidence_id in trusted_evidence_ids
        )
        new_facts = tuple(
            fact
            for fact in _diagnostic_facts_from_payload(
                cast(list[JsonDict], state.get("aggregated_facts") or [])
            )
            if fact.evidence_id in trusted_evidence_ids
        )
        if evidence_id and isinstance(output, Mapping):
            new_facts = (
                *new_facts,
                *extract_public_facts(
                    (
                        PublicToolObservation(
                            tool_name=str(current_step.get("tool") or "unknown"),
                            evidence_id=evidence_id,
                            output=cast(Mapping[str, object], output),
                        ),
                    )
                ),
            )
        all_facts = _deduplicate_diagnostic_facts((*prior_facts, *new_facts))
        assessments = _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        if not assessments:
            assessments = tuple(
                _new_hypothesis_assessment(str(item["id"]))
                for item in cast(list[JsonDict], state.get("public_hypotheses") or [])
                if item.get("id")
            )
        rules = _trusted_rules_from_plan(cast(list[JsonDict], state.get("plan") or []))
        reduced = reduce_hypotheses(
            assessments=assessments,
            facts=all_facts,
            rules=rules,
        )
        trusted_resolution = resolve_trusted_patterns(
            assessments=reduced,
            facts=all_facts,
            trusted_evidence_ids=frozenset(trusted_evidence_ids),
        )
        reduced = trusted_resolution.assessments
        assessment_payloads = [_hypothesis_assessment_payload(item) for item in reduced]
        projected_states = [
            _hypothesis_state_payload(project_hypothesis_assessment(item))
            for item in reduced
        ]
        observation_payloads: list[JsonDict] = [
            dict(item) for item in trusted_resolution.observations
        ]
        if evidence_id:
            supports = [
                item.hypothesis_id
                for item in reduced
                if item.disposition == "supported" and evidence_id in item.evidence_ids
            ]
            refutes = [
                item.hypothesis_id
                for item in reduced
                if item.disposition in {"refuted", "causally_inactive"}
                and evidence_id in item.evidence_ids
            ]
            supported_assessments = [
                item for item in reduced if item.disposition == "supported"
            ]
            unresolved_assessments = [
                item for item in reduced if item.disposition == "unresolved"
            ]
            converged_causal_link = False
            if (
                not supports
                and refutes
                and len(supported_assessments) == 1
                and not unresolved_assessments
            ):
                supports.append(supported_assessments[0].hypothesis_id)
            if (
                not supports
                and not refutes
                and len(supported_assessments) == 1
                and not unresolved_assessments
                and current_step.get("causalIntent") in {"mechanism", "impact"}
                and supported_assessments[0].hypothesis_id
                in {
                    value
                    for value in cast(
                        list[object], current_step.get("testsHypotheses") or []
                    )
                    if isinstance(value, str)
                }
            ):
                supports.append(supported_assessments[0].hypothesis_id)
                converged_causal_link = True
            linked_ids = _unique_strings(
                [
                    linked_id
                    for item in reduced
                    if evidence_id in item.evidence_ids
                    for linked_id in item.evidence_ids
                ]
            ) or [evidence_id]
            if converged_causal_link:
                linked_ids = _unique_strings(
                    [
                        *linked_ids,
                        *supported_assessments[0].evidence_ids,
                    ]
                )
            rule_causal_roles = {
                role
                for item in reduced
                if evidence_id in item.evidence_ids
                and (role := trusted_reason_causal_role(item.reason_code)) is not None
            }
            if len(rule_causal_roles) == 1:
                causal_role = next(iter(rule_causal_roles))
                causal_role_origin = "trusted_evidence_rule"
            else:
                causal_role = _safe_causal_role(current_step.get("causalIntent"))
                causal_role_origin = "plan_contract"
            observation_payloads.append(
                {
                    "purpose": str(current_step.get("purpose") or "Inspect public evidence."),
                    "supports": supports,
                    "refutes": refutes,
                    "summary": str(
                        state.get("current_evidence_summary")
                        or "A bounded public tool observation was collected."
                    ),
                    "evidenceIds": linked_ids,
                    "causalRole": causal_role,
                    "causalRoleOrigin": causal_role_origin,
                    "assessmentSource": "deterministic",
                }
            )
            if len(supported_assessments) == 1 and (
                supported_assessments[0].hypothesis_id in supports
            ):
                observation_payloads.extend(
                    _derive_upstream_deadline_observations(
                        hypothesis_id=supported_assessments[0].hypothesis_id,
                        facts=new_facts,
                        evidence_id=evidence_id,
                    )
                )
        payload: JsonDict = {
            "workflowVersion": "evidence-driven-v4",
            "factCount": len(all_facts),
            "newFactCount": len(new_facts),
            "hypothesisAssessments": assessment_payloads,
            "observationDecisions": observation_payloads,
            "matchedTrustedPatternIds": list(
                trusted_resolution.matched_pattern_ids
            ),
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="fact_adapter",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "fact_adapter", payload)
        return {
            "diagnostic_facts": [_diagnostic_fact_payload(item) for item in all_facts],
            "hypothesis_assessments": assessment_payloads,
            "hypothesis_states": projected_states,
            "observation_decisions": observation_payloads,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Fact Adapter: reduced bounded public observations into hypothesis states.",
                    65,
                )
            ],
        }

    async def _sufficiency_gate_v4(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        assessments = _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        decision = assess_sufficiency(assessments)
        projected_states = [
            _hypothesis_state_payload(project_hypothesis_assessment(item))
            for item in assessments
        ]
        observation_decisions = cast(
            list[JsonDict], state.get("observation_decisions") or []
        )
        causal_coverage = supported_causal_coverage(
            hypothesis_states=projected_states,
            observation_decisions=observation_decisions,
        )
        supported_assessments = [
            item for item in assessments if item.disposition == "supported"
        ]
        independent_positive_evidence_count = (
            len(
                _supporting_observation_evidence_ids(
                    observation_decisions,
                    hypothesis_id=supported_assessments[0].hypothesis_id,
                )
            )
            if len(supported_assessments) == 1
            else 0
        )
        buildable_decision = build_grounded_fallback_decision(
            public_hypotheses=cast(
                list[JsonDict], state.get("public_hypotheses") or []
            ),
            hypothesis_states=projected_states,
            observation_decisions=observation_decisions,
            decision_vocabulary=_json_dict(state.get("decision_vocabulary")),
        )
        decision_ready = (
            decision.status == "sufficient"
            and buildable_decision is not None
        )
        if decision.status == "sufficient" and not decision_ready:
            readiness_gaps = [
                f"causal_role:{role}" for role in causal_coverage.missing_roles
            ]
            if independent_positive_evidence_count < 2:
                readiness_gaps.insert(0, "independent_positive_evidence")
            decision = replace(
                decision,
                status="insufficient",
                missing_evidence=tuple(
                    dict.fromkeys((*decision.missing_evidence, *readiness_gaps))
                ),
                summary=(
                    "Hypotheses converged, but the public evidence chain cannot yet "
                    "produce a grounded causal decision."
                ),
            )
        payload = _evidence_sufficiency_payload(decision)
        payload.update(
            {
                "decisionReady": decision_ready,
                "independentPositiveEvidenceCount": independent_positive_evidence_count,
                "causalTriggerCount": causal_coverage.trigger_count,
                "causalMechanismCount": causal_coverage.mechanism_count,
                "causalImpactCount": causal_coverage.impact_count,
                "missingCausalRoles": list(causal_coverage.missing_roles),
                "ambiguousTrigger": causal_coverage.ambiguous_trigger,
            }
        )
        plan = cast(list[JsonDict], state.get("plan") or [])
        plan_index = int(state.get("plan_index") or 0)
        attempts = int(state.get("executor_attempt_count") or 0)
        maximum = int(state.get("max_total_steps") or 6)
        soft_expired = _execution_deadlines_from_state(state).soft_expired()
        if plan_index < len(plan) and attempts < maximum:
            next_route = "executor"
        elif decision_ready:
            next_route = "decision"
        elif soft_expired:
            next_route = "decision"
        elif decision.unresolved_hypotheses and _can_adjudicate_new_evidence(
            state,
            fact_count=len(
                _diagnostic_facts_from_payload(
                    cast(list[JsonDict], state.get("diagnostic_facts") or [])
                )
            ),
        ):
            next_route = "hypothesis_adjudicator"
        elif int(state.get("replan_count") or 0) < int(state.get("max_replans") or 1):
            next_route = "replanner"
        else:
            next_route = "decision"
        gate_payload: JsonDict = {**payload, "nextRoute": next_route}
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="sufficiency_gate",
            status="completed",
            payload=gate_payload,
        )
        await self._save_checkpoint(state, "sufficiency_gate", gate_payload)
        return {
            "evidence_sufficiency": payload,
            "next_route": next_route,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    f"Sufficiency Gate: deterministically routed to {next_route}.",
                    70,
                )
            ],
        }

    async def _hypothesis_adjudicator(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        """Use at most one batch model call for rule-unresolved public hypotheses."""
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        assessments = _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        facts = _diagnostic_facts_from_payload(
            cast(list[JsonDict], state.get("diagnostic_facts") or [])
        )
        unresolved = {
            item.hypothesis_id
            for item in assessments
            if item.disposition == "unresolved" and not item.has_high_quality_conflict
        }
        public_evidence_ids = {item.evidence_id for item in facts if item.public}
        prompt = (
            "Return JSON only with an `assessments` array. Adjudicate all unresolved public "
            "hypotheses in one batch. Each item has hypothesisId, disposition, evidenceIds, "
            "and reasonCode. disposition is supported, refuted, causally_inactive, or "
            "unresolved. Closed dispositions require cited public evidence IDs. Do not add "
            "private reasoning or hidden answers. For a supported hypothesis, cite every "
            "relevant public evidence ID needed to establish its trigger, mechanism, and "
            "impact; do not cite unrelated evidence. Unresolved IDs: "
            f"{json.dumps(sorted(unresolved))}. Public hypotheses: "
            f"{json.dumps(state.get('public_hypotheses') or [], ensure_ascii=False)}. "
            "Public facts: "
            f"{json.dumps([_diagnostic_fact_payload(item) for item in facts], ensure_ascii=False)}."
        )
        model_runtime = self._model_runtime(state)
        initial_model_count = model_runtime.budget.used
        accepted = list(assessments)
        accepted_count = 0
        source_observations = cast(
            list[JsonDict], state.get("observation_decisions") or []
        )
        decision_vocabulary = _json_dict(state.get("decision_vocabulary"))
        coverage_required = bool(source_observations) and bool(
            _json_dict(decision_vocabulary.get("labelsByHypothesis"))
        )
        accepted_observations = list(source_observations)
        accepted_quality = (False, 0, 0, 0)
        adjudication_error_category: str | None = None
        first_failure_category = "invalid_batch"
        prompts = (
            prompt,
            (
                f"{prompt} Correct the response format. The top-level object must contain "
                "exactly one `assessments` array. Every array item must contain exactly "
                "hypothesisId, disposition, evidenceIds, and reasonCode; no additional fields "
                "are allowed. Return one item for every unresolved hypothesis. Allowed "
                f"hypothesis IDs: {json.dumps(sorted(unresolved))}. Allowed public evidence "
                f"IDs: {json.dumps(sorted(public_evidence_ids))}. For a supported hypothesis, "
                "include all and only relevant public evidence IDs needed for a grounded "
                "trigger, mechanism, and impact chain."
            ),
        )
        for attempt, adjudication_prompt in enumerate(prompts, start=1):
            candidate = list(assessments)
            candidate_count = 0
            try:
                response = await self._invoke_v4_model(
                    model_runtime,
                    role="adjudicator",
                    prompt=adjudication_prompt,
                )
                if response is None:
                    raise RuntimeError("Adjudicator model call was unavailable.")
                candidate, candidate_count = _apply_llm_adjudication_payload(
                    assessments=assessments,
                    text=_model_text(response),
                    unresolved_hypothesis_ids=unresolved,
                    public_evidence_ids=public_evidence_ids,
                )
            except Exception:
                candidate_count = 0
            candidate_observations = _project_adjudicated_observations(
                observations=source_observations,
                assessments=candidate,
                facts=facts,
            )
            candidate_states = [
                _hypothesis_state_payload(project_hypothesis_assessment(item))
                for item in candidate
            ]
            coverage = supported_causal_coverage(
                hypothesis_states=candidate_states,
                observation_decisions=candidate_observations,
            )
            supported_candidates = [
                item for item in candidate if item.disposition == "supported"
            ]
            positive_evidence_count = (
                len(
                    _supporting_observation_evidence_ids(
                        candidate_observations,
                        hypothesis_id=supported_candidates[0].hypothesis_id,
                    )
                )
                if len(supported_candidates) == 1
                else 0
            )
            candidate_ready = candidate_count == len(unresolved) and (
                not coverage_required
                or (
                    assess_sufficiency(candidate).status == "sufficient"
                    and build_grounded_fallback_decision(
                        public_hypotheses=cast(
                            list[JsonDict], state.get("public_hypotheses") or []
                        ),
                        hypothesis_states=candidate_states,
                        observation_decisions=candidate_observations,
                        decision_vocabulary=decision_vocabulary,
                    )
                    is not None
                )
            )
            candidate_quality = (
                candidate_ready,
                candidate_count,
                3 - len(coverage.missing_roles),
                positive_evidence_count,
            )
            if candidate_quality > accepted_quality:
                accepted = candidate
                accepted_count = candidate_count
                accepted_observations = candidate_observations
                accepted_quality = candidate_quality
            if candidate_ready:
                if attempt == 2:
                    adjudication_error_category = (
                        "corrected_insufficient_coverage"
                        if first_failure_category == "insufficient_coverage"
                        else "corrected_invalid_batch"
                    )
                break
            if attempt == 1 and candidate_count == len(unresolved):
                first_failure_category = "insufficient_coverage"
        else:
            adjudication_error_category = "retry_exhausted"
        adjudication_attempts = model_runtime.budget.used - initial_model_count
        assessment_payloads = [_hypothesis_assessment_payload(item) for item in accepted]
        observation_payloads = accepted_observations
        payload: JsonDict = {
            "workflowVersion": "evidence-driven-v4",
            "adjudicationRound": int(state.get("adjudication_count") or 0) + 1,
            "adjudicationAttempt": adjudication_attempts,
            "adjudicationAttempts": adjudication_attempts,
            "adjudicatedFactCount": len(facts),
            "adjudicationErrorCategory": adjudication_error_category,
            "acceptedAssessmentCount": accepted_count,
            "hypothesisAssessments": assessment_payloads,
            "observationDecisions": observation_payloads,
            "modelCallCount": model_runtime.budget.used,
            "modelCallAudits": model_runtime.audits,
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="hypothesis_adjudicator",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "hypothesis_adjudicator", payload)
        return {
            "hypothesis_assessments": assessment_payloads,
            "hypothesis_states": [
                _hypothesis_state_payload(project_hypothesis_assessment(item))
                for item in accepted
            ],
            "observation_decisions": observation_payloads,
            "adjudication_count": int(state.get("adjudication_count") or 0) + 1,
            "adjudicated_fact_count": len(facts),
            "used_llm_adjudication": accepted_count > 0,
            "model_call_count": model_runtime.budget.used,
            "model_call_audits": model_runtime.audits,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Hypothesis Adjudicator: completed one bounded batch review.",
                    75,
                )
            ],
        }


    async def _evidence_evaluator(
        self,
        state: AiopsDiagnosticState,
    ) -> dict[str, object]:
        evidence_id = str(state.get("current_evidence_id") or "")
        if not evidence_id:
            return {"events": []}
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        plan_step = _json_dict(state.get("current_plan_step"))
        public_hypotheses = cast(
            list[JsonDict], state.get("public_hypotheses") or []
        )
        known_hypotheses = {
            str(item.get("id")) for item in public_hypotheses if item.get("id")
        }
        summary = str(state.get("current_evidence_summary") or "")
        raw_plan_intent = plan_step.get("causalIntent")
        plan_intent = cast(
            CausalRole,
            raw_plan_intent
            if raw_plan_intent in {"trigger", "mechanism", "impact", "context"}
            else "context",
        )
        prompt = (
            "Return one JSON observation decision with purpose, supports, refutes, summary, "
            "and causalRole. causalRole must be one of trigger, mechanism, impact, or context "
            "and describes the public causal function of this observation, not private "
            "reasoning. Evidence that is merely compatible with a hypothesis is not sufficient "
            "to support it; supports requires discriminating positive evidence. Add a hypothesis "
            "to refutes when the observation decisively contradicts a condition that hypothesis "
            "requires. Evaluate every hypothesis named by testsHypotheses, while leaving any "
            "genuinely unresolved hypothesis out of both lists. Use only known hypothesis IDs. "
            "Do not include hidden reasoning. "
            f"Known hypotheses: {json.dumps(public_hypotheses, ensure_ascii=False)}. "
            f"Plan step: {json.dumps(plan_step, ensure_ascii=False)}. "
            f"Persisted evidence ID: {evidence_id}. Observation: {summary}."
        )
        try:
            response = await self._llm_provider.create_chat_model().ainvoke(prompt)
            decision = parse_observation_decision(
                _model_text(response),
                known_hypotheses=known_hypotheses,
            )
            reported_role = decision.causal_role
            tool_name = str(plan_step.get("tool") or "")
            accepted_role = (
                reported_role
                if reported_role in allowed_causal_intents(tool_name)
                else plan_intent
            )
            decision = replace(
                decision,
                causal_role=accepted_role,
                causal_role_origin=(
                    "model" if accepted_role == reported_role else "plan_contract"
                ),
                reported_causal_role=reported_role,
                causal_role_corrected=accepted_role != reported_role,
            )
        except Exception:
            decision = ObservationDecision(
                purpose=str(plan_step.get("purpose") or "Evaluate persisted observation."),
                supports=(),
                refutes=(),
                summary="The observation could not be mapped to a validated hypothesis update.",
                causal_role=plan_intent,
                causal_role_origin="plan_contract",
            )
        decision_payload = _observation_decision_payload(decision, evidence_id=evidence_id)
        hypothesis_states = _update_hypothesis_states(
            cast(list[JsonDict], state.get("hypothesis_states") or []),
            decision=decision,
            evidence_id=evidence_id,
        )
        payload: JsonDict = {
            "evidenceIds": [evidence_id],
            "observationDecision": decision_payload,
            "hypothesisStates": hypothesis_states,
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="evidence_evaluation",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "evidence_evaluation", payload)
        return {
            "hypothesis_states": hypothesis_states,
            "observation_decisions": [decision_payload],
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Evidence Evaluator: updated public hypotheses from persisted evidence.",
                    60,
                )
            ],
        }

    async def _sufficiency_gate(
        self,
        state: AiopsDiagnosticState,
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        plan = cast(list[JsonDict], state.get("plan") or [])
        plan_index = int(state.get("plan_index") or 0)
        evidence_ids = _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        public_hypotheses = cast(list[JsonDict], state.get("public_hypotheses") or [])
        known_hypotheses = {
            str(item.get("id")) for item in public_hypotheses if item.get("id")
        }
        tool_definitions = tuple(
            definition
            for definition in (state.get("tool_definitions") or ())
            if definition.name not in self._tool_policies
        )
        available_tools = {definition.name for definition in tool_definitions}
        prompt = (
            "Return one JSON evidence sufficiency decision with status, evidenceIds, "
            "supportedHypotheses, refutedHypotheses, unresolvedHypotheses, missingEvidence, "
            "recommendedTools, and summary. Use only public IDs and discovered read tools. "
            "Do not include private chain-of-thought. "
            f"Public hypotheses: {json.dumps(public_hypotheses, ensure_ascii=False)}. "
            "Hypothesis states: "
            f"{json.dumps(state.get('hypothesis_states') or [], ensure_ascii=False)}. "
            "Structured observations: "
            f"{json.dumps(state.get('observation_decisions') or [], ensure_ascii=False)}. "
            f"Persisted evidence IDs: {json.dumps(evidence_ids)}. "
            "Bounded evidence summaries: "
            f"{json.dumps(state.get('evidence') or [], ensure_ascii=False)}. "
            f"Discovered tools: {json.dumps(sorted(available_tools))}."
        )
        try:
            response = await self._llm_provider.create_chat_model().ainvoke(prompt)
            decision = parse_evidence_sufficiency(
                _model_text(response),
                available_evidence_ids=set(evidence_ids),
                known_hypotheses=known_hypotheses,
                available_tools=available_tools,
            )
        except Exception:
            decision = _fallback_evidence_sufficiency(
                public_hypotheses=public_hypotheses,
                hypothesis_states=cast(
                    list[JsonDict], state.get("hypothesis_states") or []
                ),
                evidence_ids=evidence_ids,
            )
        decision = _project_evidence_sufficiency(
            model_decision=decision,
            public_hypotheses=public_hypotheses,
            hypothesis_states=cast(
                list[JsonDict], state.get("hypothesis_states") or []
            ),
            evidence_ids=evidence_ids,
        )
        payload = _evidence_sufficiency_payload(decision)
        causal_coverage = supported_causal_coverage(
            hypothesis_states=cast(
                list[JsonDict], state.get("hypothesis_states") or []
            ),
            observation_decisions=cast(
                list[JsonDict], state.get("observation_decisions") or []
            ),
        )
        payload.update(
            {
                "causalTriggerCount": causal_coverage.trigger_count,
                "causalMechanismCount": causal_coverage.mechanism_count,
                "causalImpactCount": causal_coverage.impact_count,
                "missingCausalRoles": list(causal_coverage.missing_roles),
                "ambiguousTrigger": causal_coverage.ambiguous_trigger,
            }
        )
        attempt_count = int(state.get("executor_attempt_count") or 0)
        max_total_steps = int(state.get("max_total_steps") or 6)
        refinement_index: int | None = None
        refinement_reason = ""
        if decision.unresolved_hypotheses:
            if attempt_count < max_total_steps:
                refinement_index = _next_open_hypothesis_step_index(
                    plan=plan,
                    plan_index=plan_index,
                    open_hypothesis_ids=decision.unresolved_hypotheses,
                    executed_fingerprints=cast(
                        list[str], state.get("executed_step_fingerprints") or []
                    ),
                )
            if refinement_index is not None:
                next_route = "executor"
                termination_reason = ""
                refinement_reason = "open_hypothesis_plan_step_remaining"
            elif _can_replan(state):
                next_route = "replanner"
                termination_reason = ""
                refinement_reason = "open_hypothesis_requires_replan"
            else:
                next_route = "decision"
                termination_reason = _budget_termination_reason(state)
        elif decision.status == "sufficient":
            supported_hypothesis_id = (
                decision.supported_hypotheses[0]
                if len(decision.supported_hypotheses) == 1
                else None
            )
            if causal_coverage.complete:
                next_route = "decision"
                termination_reason = "evidence_sufficient"
            elif attempt_count < max_total_steps:
                refinement_index = next_causal_refinement_index(
                    plan=plan,
                    plan_index=plan_index,
                    missing_roles=causal_coverage.missing_roles,
                    supported_hypothesis_id=supported_hypothesis_id,
                    executed_fingerprints=cast(
                        list[str], state.get("executed_step_fingerprints") or []
                    ),
                    fingerprint=_step_fingerprint,
                )
                if refinement_index is not None:
                    next_route = "executor"
                    termination_reason = ""
                    refinement_reason = "missing_causal_role_plan_step_remaining"
                elif _can_replan(state):
                    next_route = "replanner"
                    termination_reason = ""
                    refinement_reason = "missing_causal_role_requires_replan"
                else:
                    next_route = "decision"
                    termination_reason = _budget_termination_reason(state)
            elif _can_replan(state):
                next_route = "replanner"
                termination_reason = ""
                refinement_reason = "missing_causal_role_requires_replan"
            else:
                next_route = "decision"
                termination_reason = _budget_termination_reason(state)
        elif plan_index < len(plan) and attempt_count < max_total_steps:
            next_route = "executor"
            termination_reason = ""
        elif _can_replan(state):
            next_route = "replanner"
            termination_reason = ""
        else:
            next_route = "decision"
            termination_reason = _budget_termination_reason(state)
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="sufficiency_gate",
            status="completed",
            payload={
                **payload,
                "nextRoute": next_route,
                "refinementReason": refinement_reason,
            },
        )
        await self._save_checkpoint(
            state,
            "sufficiency_gate",
            {
                **payload,
                "nextRoute": next_route,
                "refinementReason": refinement_reason,
            },
        )
        update: dict[str, object] = {
            "evidence_sufficiency": payload,
            "next_route": next_route,
            "termination_reason": termination_reason,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    f"Sufficiency Gate: routed to {next_route} from persisted evidence.",
                    70,
                )
            ],
        }
        if refinement_index is not None:
            update["plan_index"] = refinement_index
        return update

    async def _replanner(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        current_plan = cast(list[JsonDict], state.get("plan") or [])
        tool_definitions = tuple(
            definition
            for definition in (state.get("tool_definitions") or ())
            if definition.name not in self._tool_policies
        )
        available_tools = {definition.name for definition in tool_definitions}
        known_hypotheses = {
            str(item.get("id"))
            for item in cast(list[JsonDict], state.get("public_hypotheses") or [])
            if item.get("id")
        }
        attempt_count = int(state.get("executor_attempt_count") or 0)
        remaining_budget = max(0, int(state.get("max_total_steps") or 6) - attempt_count)
        validation_gap = _json_dict(state.get("decision_validation"))
        replan_reason = (
            "decision_validation_gap"
            if validation_gap.get("status") == "invalid"
            else "evidence_gap"
        )
        prompt = (
            "Return JSON only for a gap-targeted diagnostic replan with a `steps` array. "
            "Each step has id, tool, arguments, purpose, testsHypotheses, and causalIntent. "
            "causalIntent must be allowed by the selected tool contract. Use only the "
            "discovered contracts and public hypothesis IDs. Do not repeat an executed tool and "
            "arguments pair. "
            "Evidence gap: "
            f"{json.dumps(state.get('evidence_sufficiency') or {}, ensure_ascii=False)}. "
            f"Decision validation gap: {json.dumps(validation_gap, ensure_ascii=False)}. "
            "Discovered contracts: "
            f"{json.dumps(_tool_contracts_payload(tool_definitions), ensure_ascii=False)}. "
            f"Public hypotheses: {json.dumps(sorted(known_hypotheses))}. "
            f"Executed fingerprints: {json.dumps(state.get('executed_step_fingerprints') or [])}. "
            f"Remaining executor attempt budget: {remaining_budget}."
        )
        parsed_steps: list[JsonDict] = []
        model_runtime = (
            self._model_runtime(state)
            if state.get("workflow_version") == "evidence-driven-v4"
            else None
        )
        try:
            response = (
                await self._invoke_v4_model(
                    model_runtime,
                    role="replanner",
                    prompt=prompt,
                )
                if model_runtime is not None
                else await self._llm_provider.create_chat_model().ainvoke(prompt)
            )
            if response is None:
                raise RuntimeError("Replanner model call was unavailable.")
            parsed = parse_plan(
                _model_text(response),
                available_tools=available_tools,
                known_hypotheses=known_hypotheses,
                causal_capabilities={
                    definition.name: allowed_causal_intents(definition.name)
                    for definition in tool_definitions
                },
            )
            parsed_steps = [_diagnostic_plan_step_payload(step) for step in parsed]
        except Exception:
            parsed_steps = []
        parsed_steps, _contract_errors = normalize_tool_plan_steps(
            parsed_steps,
            trusted_tool_arguments=self._trusted_tool_arguments,
            tool_argument_contracts=self._tool_argument_contracts,
            tool_definitions=tool_definitions,
        )
        parsed_coverage = repair_plan_causal_coverage(parsed_steps)
        parsed_steps = list(parsed_coverage.steps)
        executed = set(state.get("executed_step_fingerprints") or [])
        missing_roles = {
            item
            for item in cast(
                list[object],
                _json_dict(state.get("evidence_sufficiency")).get(
                    "missingCausalRoles"
                )
                or [],
            )
            if isinstance(item, str)
        }
        accepted: list[JsonDict] = []
        causal_intent_rejected_count = 0
        for step in parsed_steps:
            if not _step_targets_replan_gap(
                step,
                replan_reason=replan_reason,
                missing_roles=missing_roles,
            ):
                causal_intent_rejected_count += 1
                continue
            fingerprint = _step_fingerprint(step)
            if fingerprint in executed or any(
                _step_fingerprint(existing) == fingerprint for existing in accepted
            ):
                continue
            accepted.append(step)
            if len(accepted) >= remaining_budget:
                break
        if (
            not accepted
            and remaining_budget
            and state.get("workflow_version") == "evidence-driven-v4"
        ):
            fallback_steps = _deterministic_gap_replan_steps(
                state,
                available_tools=available_tools,
            )
            normalized_fallback, _fallback_contract_errors = normalize_tool_plan_steps(
                fallback_steps,
                trusted_tool_arguments=self._trusted_tool_arguments,
                tool_argument_contracts=self._tool_argument_contracts,
                tool_definitions=tool_definitions,
            )
            for step in normalized_fallback:
                fingerprint = _step_fingerprint(step)
                if fingerprint in executed or any(
                    _step_fingerprint(existing) == fingerprint for existing in accepted
                ):
                    continue
                accepted.append(step)
                if len(accepted) >= remaining_budget:
                    break
        replan_count = int(state.get("replan_count") or 0) + 1
        termination_reason = "" if accepted else "no_useful_step"
        payload: JsonDict = {
            "reason": replan_reason,
            "addedStepCount": len(accepted),
            "replanCount": replan_count,
            "remainingAttemptBudget": remaining_budget,
            "plan": accepted,
            "terminationReason": termination_reason,
            "causalIntentRejectedStepCount": causal_intent_rejected_count,
            **_plan_causal_coverage_payload(accepted),
        }
        if model_runtime is not None:
            payload["modelCallCount"] = model_runtime.budget.used
            payload["modelCallAudits"] = model_runtime.audits
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="replanner",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "replanner", payload)
        update: dict[str, object] = {
            "plan": [*current_plan, *accepted],
            "replan_count": replan_count,
            "termination_reason": termination_reason,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    f"Replanner: added {len(accepted)} gap-targeted step(s).",
                    75,
                )
            ],
        }
        if model_runtime is not None:
            update["model_call_count"] = model_runtime.budget.used
            update["model_call_audits"] = model_runtime.audits
        return update

    async def _decision_v4(self, state: AiopsDiagnosticState) -> dict[str, object]:
        """Assemble the v4 decision without another model call."""
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        assessments = _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        projected_states = [
            _hypothesis_state_payload(project_hypothesis_assessment(item))
            for item in assessments
        ]
        decision = build_grounded_fallback_decision(
            public_hypotheses=cast(
                list[JsonDict], state.get("public_hypotheses") or []
            ),
            hypothesis_states=projected_states,
            observation_decisions=cast(
                list[JsonDict], state.get("observation_decisions") or []
            ),
            decision_vocabulary=_json_dict(state.get("decision_vocabulary")),
        )
        decision_payload = (
            _root_cause_decision_payload(decision) if decision is not None else None
        )
        payload: JsonDict = {
            "workflowVersion": "evidence-driven-v4",
            "rootCauseDecision": decision_payload,
            "evidenceIds": list(decision.evidence_ids) if decision is not None else [],
            "status": "grounded" if decision is not None else "insufficient_evidence",
            "decisionOrigin": "deterministic_grounded" if decision is not None else "none",
            "decisionAttempts": 0,
            "decisionErrorCategory": None,
            "decisionErrorCodes": [],
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="decision",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "decision", payload)
        update: dict[str, object] = {
            "root_cause_decision": decision_payload,
            "hypothesis_states": projected_states,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Decision: assembled a deterministic evidence-grounded conclusion."
                    if decision is not None
                    else "Decision: evidence was insufficient for a unique conclusion.",
                    85,
                )
            ],
        }
        return update

    async def _deterministic_validator_v4(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        """Validate v4 from four-state assessments without semantic model review."""
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        assessments = _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        candidate = _root_cause_decision_from_payload(state.get("root_cause_decision"))
        evidence_ids = set(
            _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        )
        deterministic_result = (
            validate_grounded_assessments(
                candidate=candidate,
                available_evidence_ids=evidence_ids,
                hypothesis_assessments=assessments,
                observation_decisions=cast(
                    list[JsonDict], state.get("observation_decisions") or []
                ),
                decision_vocabulary=_json_dict(state.get("decision_vocabulary")),
            )
            if candidate is not None
            else None
        )
        valid = deterministic_result is not None and deterministic_result.passed
        deadline_soft_expired = _execution_deadlines_from_state(state).soft_expired()
        replan_eligible = (
            not valid
            and int(state.get("replan_count") or 0)
            < int(state.get("max_replans") or 1)
            and not deadline_soft_expired
        )
        next_route = (
            "recovery_planner"
            if valid
            else "replanner"
            if replan_eligible
            else "manual_review"
        )
        payload: JsonDict = {
            "workflowVersion": "evidence-driven-v4",
            "status": "valid" if valid else "invalid",
            "validationOrigin": "deterministic",
            "validationAttempts": 0,
            "validationErrorCategory": None if valid else "deterministic_gap",
            "evidenceIds": list(candidate.evidence_ids) if candidate is not None else [],
            "unsupportedFields": list(
                deterministic_result.unsupported_fields
                if deterministic_result is not None
                else ()
            ),
            "missingEvidence": list(
                deterministic_result.missing_evidence
                if deterministic_result is not None
                else ("No grounded root-cause decision was available.",)
            ),
            "deterministicChecks": (
                deterministic_checks_payload(deterministic_result)
                if deterministic_result is not None
                else []
            ),
            "summary": (
                "Deterministic public-evidence validation passed."
                if valid
                else "Deterministic public-evidence validation failed closed."
            ),
            "targetedReplanEligible": replan_eligible,
            "nextRoute": next_route,
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="decision_validator",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "deterministic_validator", payload)
        return {
            "decision_validation": payload,
            "next_route": next_route,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Decision Validator: completed deterministic public-evidence checks.",
                    88,
                )
            ],
        }

    async def _manual_review_v4(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        candidate = _root_cause_decision_from_payload(state.get("root_cause_decision"))
        proposal_tools = {
            definition.name
            for definition in (state.get("tool_definitions") or ())
            if self._tool_policies.get(definition.name) == "proposal_only"
        }
        plan = _fallback_recovery_plan(
            candidate,
            proposal_tools=proposal_tools,
            force_manual_review=True,
        )
        payload = _recovery_plan_payload(plan)
        payload["origin"] = "deterministic_fail_closed"
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="recovery_planning",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "manual_review", payload)
        return {
            "recovery_plan": payload,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Recovery Planner: deterministic validation failed closed to manual review.",
                    89,
                )
            ],
        }

    async def _validator_router_v4(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        validation = _json_dict(state.get("decision_validation"))
        recovery = _json_dict(state.get("recovery_plan"))
        vocabulary = _json_dict(state.get("decision_vocabulary"))
        components = tuple(
            item
            for item in cast(list[object], vocabulary.get("causalComponents") or [])
            if isinstance(item, str)
        )
        if not components:
            candidate = _root_cause_decision_from_payload(state.get("root_cause_decision"))
            components = (candidate.component,) if candidate is not None else ()
        assessments = _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        routing = requires_llm_validation(
            ValidatorRiskContext(
                deterministic_valid=validation.get("status") == "valid",
                used_llm_adjudication=bool(state.get("used_llm_adjudication")),
                execution_requested=recovery.get("mode")
                == "external_policy_required",
                max_risk_tier=_risk_tier(recovery.get("risk")),
                compound_root_cause=vocabulary.get("compoundRootCause") is True,
                causal_components=components,
                has_high_quality_conflict=any(
                    item.has_high_quality_conflict for item in assessments
                ),
            )
        )
        payload: JsonDict = {
            "validationRequired": routing.required,
            "validationSkipped": not routing.required,
            "validationReasonCodes": list(routing.reason_codes),
            "validationSkipReason": routing.skip_reason,
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="validator_router",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "validator_router", payload)
        return {
            "validator_routing": payload,
            "next_route": "llm_validator" if routing.required else "policy_gate",
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Validator Router: semantic validation required."
                    if routing.required
                    else "Validator Router: deterministic validation was sufficient.",
                    89,
                )
            ],
        }

    async def _llm_validator_v4(
        self, state: AiopsDiagnosticState
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        candidate = _root_cause_decision_from_payload(state.get("root_cause_decision"))
        evidence_ids = set(
            _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        )
        model_runtime = self._model_runtime(state)
        initial_model_count = model_runtime.budget.used
        validation: RootCauseValidationDecision | None = None
        if candidate is not None:
            prompt = (
                "Return JSON only with status, evidenceIds, unsupportedFields, "
                "missingEvidence, and summary. Semantically validate only the public candidate "
                "against public structured observations and cited Evidence IDs. Do not include "
                "private reasoning. Candidate: "
                f"{json.dumps(state.get('root_cause_decision'), ensure_ascii=False)}. "
                "Observations: "
                f"{json.dumps(state.get('observation_decisions') or [], ensure_ascii=False)}."
            )
            try:
                response = await self._invoke_v4_model(
                    model_runtime,
                    role="validator",
                    prompt=prompt,
                )
                if response is None:
                    raise RuntimeError("Validator model call was unavailable.")
                validation = parse_root_cause_validation(
                    _model_text(response),
                    available_evidence_ids=evidence_ids,
                )
            except Exception:
                validation = None
        semantic_valid = validation is not None and validation.status == "valid"
        payload: JsonDict = {
            **_json_dict(state.get("decision_validation")),
            "validationOrigin": "llm_semantic" if validation is not None else "llm_failed",
            "semanticValidationStatus": (
                validation.status if validation is not None else "failed"
            ),
            "semanticValidationAttempts": model_runtime.budget.used
            - initial_model_count,
            "validationRequired": True,
            "validationSkipped": False,
            "validationReasonCodes": list(
                cast(
                    list[object],
                    _json_dict(state.get("validator_routing")).get(
                        "validationReasonCodes"
                    )
                    or [],
                )
            ),
            "modelCallCount": model_runtime.budget.used,
            "modelCallAudits": model_runtime.audits,
        }
        update: dict[str, object] = {
            "decision_validation": payload,
            "model_call_count": model_runtime.budget.used,
            "model_call_audits": model_runtime.audits,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "LLM Validator: semantic validation passed."
                    if semantic_valid
                    else "LLM Validator: failed closed to manual review.",
                    89,
                )
            ],
        }
        if not semantic_valid:
            proposal_tools = {
                definition.name
                for definition in (state.get("tool_definitions") or ())
                if self._tool_policies.get(definition.name) == "proposal_only"
            }
            fallback = _fallback_recovery_plan(
                candidate,
                proposal_tools=proposal_tools,
                force_manual_review=True,
            )
            recovery_payload = _recovery_plan_payload(fallback)
            recovery_payload["origin"] = "semantic_validator_fail_closed"
            update["recovery_plan"] = recovery_payload
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="llm_validator",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "llm_validator", payload)
        return update

    async def _decision(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        evidence_ids = _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        decision_evidence_ids = _supporting_decision_evidence_ids(
            hypothesis_states=cast(
                list[JsonDict], state.get("hypothesis_states") or []
            ),
            observation_decisions=cast(
                list[JsonDict], state.get("observation_decisions") or []
            ),
            persisted_evidence_ids=evidence_ids,
        )
        decision_vocabulary = _json_dict(state.get("decision_vocabulary"))
        prompt = (
            "Return JSON only for one root-cause decision with component, mechanism, trigger, "
            "causalChain, evidenceIds, and confidence. Trigger must state the direct "
            "triggering condition, not repeat the alert symptom or the broad hypothesis "
            "description. causalChain must contain 2 to 6 ordered atomic causal facts, and "
            "every fact must map to a supporting structured observation. This is a "
            "structured public decision, not private chain-of-thought. Use only persisted "
            "evidence IDs. "
            f"Alert: {json.dumps(_json_dict(state.get('alert')), ensure_ascii=False)}. "
            "Public hypotheses: "
            f"{json.dumps(state.get('public_hypotheses') or [], ensure_ascii=False)}. "
            "Hypothesis states: "
            f"{json.dumps(state.get('hypothesis_states') or [], ensure_ascii=False)}. "
            "Structured observations: "
            f"{json.dumps(state.get('observation_decisions') or [], ensure_ascii=False)}. "
            "Use the canonical component and mechanism labels declared by this public "
            "decision vocabulary when it contains a matching alias: "
            f"{json.dumps(decision_vocabulary, ensure_ascii=False)}. "
            "Supporting observation evidence IDs: "
            f"{json.dumps(decision_evidence_ids)}."
        )
        decision: RootCauseDecision | None = None
        decision_origin = "none"
        decision_outcome = await invoke_structured_root_cause_decision(
            model=self._llm_provider.create_chat_model(),
            prompt=prompt,
            available_evidence_ids=set(decision_evidence_ids),
            structured_output_method=_provider_structured_output_method(
                self._llm_provider
            ),
        )
        decision = decision_outcome.decision
        decision_error_category = decision_outcome.error_category
        if decision is not None:
            decision = normalize_root_cause_decision(
                decision,
                component_aliases=_string_mapping(
                    decision_vocabulary.get("componentAliases")
                ),
                mechanism_aliases=_string_mapping(
                    decision_vocabulary.get("mechanismAliases")
                ),
            )
            decision_origin = "llm"
            normalized = _normalize_grounded_decision(
                decision,
                available_evidence_ids=set(evidence_ids),
                public_hypotheses=cast(
                    list[JsonDict], state.get("public_hypotheses") or []
                ),
                hypothesis_states=cast(
                    list[JsonDict], state.get("hypothesis_states") or []
                ),
                observation_decisions=cast(
                    list[JsonDict], state.get("observation_decisions") or []
                ),
                decision_vocabulary=decision_vocabulary,
            )
            if normalized is not None:
                decision = normalized
                decision_origin = "llm_grounded_normalization"
        if decision is None:
            decision = build_grounded_fallback_decision(
                public_hypotheses=cast(
                    list[JsonDict], state.get("public_hypotheses") or []
                ),
                hypothesis_states=cast(
                    list[JsonDict], state.get("hypothesis_states") or []
                ),
                observation_decisions=cast(
                    list[JsonDict], state.get("observation_decisions") or []
                ),
                decision_vocabulary=decision_vocabulary,
            )
            if decision is not None:
                decision_origin = "grounded_fallback"
        decision_payload = (
            _root_cause_decision_payload(decision) if decision is not None else None
        )
        payload: JsonDict = {
            "rootCauseDecision": decision_payload,
            "evidenceIds": list(decision.evidence_ids) if decision is not None else [],
            "status": "grounded" if decision is not None else "insufficient_evidence",
            "decisionOrigin": decision_origin,
            "decisionErrorCategory": decision_error_category,
            "decisionAttempts": decision_outcome.attempts,
            "decisionErrorCodes": list(decision_outcome.error_codes),
            "decisionErrorCode": decision_outcome.error_code,
            "decisionErrorPhase": decision_outcome.error_phase,
            "decisionRetryable": decision_outcome.retryable,
            "decisionHttpStatusClass": decision_outcome.http_status_class,
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="decision",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "decision", payload)
        return {
            "root_cause_decision": decision_payload,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    "Decision: persisted an evidence-grounded conclusion."
                    if decision is not None
                    else "Decision: evidence was insufficient for a grounded conclusion.",
                    85,
                )
            ],
        }

    async def _decision_validator(
        self,
        state: AiopsDiagnosticState,
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        evidence_ids = _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        candidate = _root_cause_decision_from_payload(state.get("root_cause_decision"))
        validation_origin = "none"
        validation_error_category: str | None = None
        validation_error_code: str | None = None
        validation_error_codes: tuple[str, ...] = ()
        validation_error_phase: str | None = None
        validation_retryable: bool | None = None
        validation_http_status_class: str | None = None
        validation_attempts = 0
        validation_warning: str | None = None
        validation_model = _validator_model_name(self._llm_provider)
        deterministic_result = None
        deterministic_replan_allowed = False
        validation = RootCauseValidationDecision(
            status="invalid",
            evidence_ids=(),
            unsupported_fields=(),
            missing_evidence=("No grounded root-cause decision was available.",),
            summary="Decision validation failed closed because no candidate was available.",
        )
        if candidate is None:
            validation_error_category = "candidate_missing"
        else:
            deterministic_result = validate_grounded_candidate(
                candidate=candidate,
                available_evidence_ids=set(evidence_ids),
                hypothesis_states=cast(
                    list[JsonDict], state.get("hypothesis_states") or []
                ),
                observation_decisions=cast(
                    list[JsonDict], state.get("observation_decisions") or []
                ),
                decision_vocabulary=_json_dict(state.get("decision_vocabulary")),
            )
        structural_gaps = (
            _deterministic_decision_gaps(
                candidate,
                decision_vocabulary=_json_dict(state.get("decision_vocabulary")),
            )
            if candidate is not None
            else ()
        )
        if deterministic_result is not None:
            sufficiency = _json_dict(state.get("evidence_sufficiency"))
            recommended_tools = _unique_strings(
                [
                    item
                    for item in cast(
                        list[object], sufficiency.get("recommendedTools") or []
                    )
                    if isinstance(item, str)
                ]
            )
            available_tools = {
                definition.name for definition in state.get("tool_definitions") or ()
            }
            executed_tools = {
                tool
                for item in cast(list[JsonDict], state.get("evidence") or [])
                if isinstance((tool := item.get("tool")), str)
            }
            deterministic_replan_allowed = can_replan_deterministic_gap(
                deterministic_result,
                recommended_tools=recommended_tools,
                available_tools=available_tools,
                executed_tools=executed_tools,
            )
        deterministic_failed_codes = (
            {
                check.code
                for check in deterministic_result.checks
                if not check.passed
            }
            if deterministic_result is not None
            else set()
        )
        semantic_expression_codes = {"grounded_causal_chain", "trigger_present"}
        semantic_review_allowed = deterministic_failed_codes.issubset(
            semantic_expression_codes
        )
        if candidate is not None and deterministic_result is not None and structural_gaps:
            validation = RootCauseValidationDecision(
                status="invalid",
                evidence_ids=candidate.evidence_ids,
                unsupported_fields=cast(
                    tuple[
                        Literal["component", "mechanism", "trigger", "causalChain"], ...
                    ],
                    structural_gaps,
                ),
                missing_evidence=(),
                summary="Deterministic root-cause validation rejected the candidate.",
            )
            validation_error_category = "deterministic_gap"
        elif (
            candidate is not None
            and deterministic_result is not None
            and not semantic_review_allowed
            and deterministic_replan_allowed
        ):
            validation = RootCauseValidationDecision(
                status="invalid",
                evidence_ids=candidate.evidence_ids,
                unsupported_fields=(),
                missing_evidence=deterministic_result.missing_evidence,
                summary="The deterministic evidence contract identified a targeted gap.",
            )
            validation_error_category = "deterministic_gap"
        elif (
            candidate is not None
            and deterministic_result is not None
            and not semantic_review_allowed
            and not deterministic_result.passed
        ):
            validation = RootCauseValidationDecision(
                status="invalid",
                evidence_ids=candidate.evidence_ids,
                unsupported_fields=deterministic_result.unsupported_fields,
                missing_evidence=deterministic_result.missing_evidence,
                summary="The candidate failed the deterministic evidence contract.",
            )
            validation_error_category = "deterministic_gap"
        elif candidate is not None and deterministic_result is not None:
            response_example = json.dumps(
                {
                    "status": "valid",
                    "evidenceIds": list(candidate.evidence_ids),
                    "unsupportedFields": [],
                    "missingEvidence": [],
                    "summary": "Public evidence supports every candidate field.",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            prompt = (
                "Return JSON only for one root-cause validation decision with status, evidenceIds, "
                "unsupportedFields, missingEvidence, and summary. Judge only whether the "
                "candidate component, mechanism, trigger, and causalChain are supported by the "
                "public structured observations. Verify every candidate field and every "
                "causalChain fact against the candidate-cited observation evidence IDs; semantic "
                "paraphrases are acceptable, but an uncited or unsupported fact is invalid. Do "
                "not compare against hidden answers and do "
                "not include private chain-of-thought. Follow this JSON shape example: "
                f"{response_example}. "
                f"Candidate: {json.dumps(state.get('root_cause_decision'), ensure_ascii=False)}. "
                "Public hypotheses: "
                f"{json.dumps(state.get('public_hypotheses') or [], ensure_ascii=False)}. "
                "Hypothesis states: "
                f"{json.dumps(state.get('hypothesis_states') or [], ensure_ascii=False)}. "
                "Structured observations: "
                f"{json.dumps(state.get('observation_decisions') or [], ensure_ascii=False)}. "
                f"Persisted evidence IDs: {json.dumps(evidence_ids)}."
            )
            outcome = await invoke_structured_root_cause_validation(
                model=_validator_chat_model(self._llm_provider),
                prompt=prompt,
                available_evidence_ids=set(evidence_ids),
                structured_output_method=_validator_structured_output_method(
                    self._llm_provider
                ),
            )
            validation_attempts = outcome.attempts
            validation_error_category = outcome.error_category
            validation_error_code = outcome.error_code
            validation_error_codes = outcome.error_codes
            validation_error_phase = outcome.error_phase
            validation_retryable = outcome.retryable
            validation_http_status_class = outcome.http_status_class
            if outcome.decision is not None:
                validation = outcome.decision
                if validation.status == "valid":
                    validation_origin = "llm_confirmed"
            elif deterministic_result.passed:
                validation = RootCauseValidationDecision(
                    status="valid",
                    evidence_ids=candidate.evidence_ids,
                    unsupported_fields=(),
                    missing_evidence=(),
                    summary=(
                        "The candidate passed every public deterministic evidence check; "
                        "the LLM Validator was unavailable."
                    ),
                )
                validation_origin = "deterministic_grounded_fallback"
                validation_warning = "llm_validator_unavailable"
            else:
                validation = RootCauseValidationDecision(
                    status="invalid",
                    evidence_ids=candidate.evidence_ids,
                    unsupported_fields=deterministic_result.unsupported_fields,
                    missing_evidence=deterministic_result.missing_evidence,
                    summary=(
                        "The LLM Validator was unavailable and the candidate did not pass "
                        "the deterministic fallback contract."
                    ),
                )
                validation_warning = "llm_validator_unavailable"
        payload = _root_cause_validation_payload(validation)
        can_replan = (
            candidate is not None
            and validation.status == "invalid"
            and (
                (
                    validation_error_category == "model_rejected"
                    and bool(validation.missing_evidence or validation.unsupported_fields)
                )
                or (
                    validation_error_category == "deterministic_gap"
                    and deterministic_replan_allowed
                )
            )
            and _can_replan(state)
        )
        route = "replanner" if can_replan else "recovery_planner"
        termination_reason = str(state.get("termination_reason") or "")
        root_cause_payload = state.get("root_cause_decision")
        if validation.status == "invalid" and not can_replan:
            root_cause_payload = None
            termination_reason = "unsupported_decision"
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="decision_validation",
            status="completed",
            payload={
                **payload,
                "validationOrigin": validation_origin,
                "validationErrorCategory": validation_error_category,
                "validationErrorCode": validation_error_code,
                "validationErrorCodes": list(validation_error_codes),
                "validationErrorPhase": validation_error_phase,
                "validationRetryable": validation_retryable,
                "validationHttpStatusClass": validation_http_status_class,
                "validationAttempts": validation_attempts,
                "validationModel": validation_model,
                "validationWarning": validation_warning,
                "deterministicChecks": (
                    deterministic_checks_payload(deterministic_result)
                    if deterministic_result is not None
                    else []
                ),
                "nextRoute": route,
            },
        )
        await self._save_checkpoint(
            state,
            "decision_validation",
            {
                **payload,
                "validationOrigin": validation_origin,
                "validationErrorCategory": validation_error_category,
                "validationErrorCode": validation_error_code,
                "validationErrorCodes": list(validation_error_codes),
                "validationErrorPhase": validation_error_phase,
                "validationRetryable": validation_retryable,
                "validationHttpStatusClass": validation_http_status_class,
                "validationAttempts": validation_attempts,
                "validationModel": validation_model,
                "validationWarning": validation_warning,
                "deterministicChecks": (
                    deterministic_checks_payload(deterministic_result)
                    if deterministic_result is not None
                    else []
                ),
                "nextRoute": route,
            },
        )
        validation_payload: JsonDict = {
            **payload,
            "validationOrigin": validation_origin,
            "validationErrorCategory": validation_error_category,
            "validationErrorCode": validation_error_code,
            "validationErrorCodes": list(validation_error_codes),
            "validationErrorPhase": validation_error_phase,
            "validationRetryable": validation_retryable,
            "validationHttpStatusClass": validation_http_status_class,
            "validationAttempts": validation_attempts,
            "validationModel": validation_model,
            "validationWarning": validation_warning,
            "deterministicChecks": (
                deterministic_checks_payload(deterministic_result)
                if deterministic_result is not None
                else []
            ),
        }
        return {
            "decision_validation": validation_payload,
            "root_cause_decision": root_cause_payload,
            "next_route": cast(Literal["replanner", "recovery_planner"], route),
            "termination_reason": termination_reason,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    f"Decision Validator: {validation.status}.",
                    87,
                )
            ],
        }

    async def _recovery_planner(
        self,
        state: AiopsDiagnosticState,
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        evidence_ids = _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        candidate = _root_cause_decision_from_payload(state.get("root_cause_decision"))
        proposal_definitions = tuple(
            definition
            for definition in (state.get("tool_definitions") or ())
            if self._tool_policies.get(definition.name) == "proposal_only"
        )
        proposal_tools = {definition.name for definition in proposal_definitions}
        model_runtime = (
            self._model_runtime(state)
            if state.get("workflow_version") == "evidence-driven-v4"
            else None
        )
        if candidate is None:
            plan = _fallback_recovery_plan(None, proposal_tools=proposal_tools)
        elif _json_dict(state.get("decision_validation")).get(
            "validationOrigin"
        ) == "deterministic_grounded_fallback":
            plan = _fallback_recovery_plan(
                candidate,
                proposal_tools=proposal_tools,
                force_manual_review=True,
            )
        else:
            prompt = (
                "Return one JSON structured recovery plan with mode, action, target, rationale, "
                "tool, arguments, risk, rollback, verificationSteps, evidenceIds, "
                "decisionConfidence, and humanApprovalRequired. A plan is a recommendation, not "
                "authorization. Use only the validated decision, persisted evidence IDs, and "
                "discovered proposal-only contracts. Do not request infrastructure execution. "
                "Validated decision: "
                f"{json.dumps(state.get('root_cause_decision'), ensure_ascii=False)}. "
                f"Persisted evidence IDs: {json.dumps(evidence_ids)}. "
                "Proposal-only contracts: "
                f"{json.dumps(_tool_contracts_payload(proposal_definitions), ensure_ascii=False)}."
            )
            try:
                response = (
                    await self._invoke_v4_model(
                        model_runtime,
                        role="recovery_planner",
                        prompt=prompt,
                    )
                    if model_runtime is not None
                    else await self._llm_provider.create_chat_model().ainvoke(prompt)
                )
                if response is None:
                    raise RuntimeError("Recovery Planner model call was unavailable.")
                plan = parse_recovery_plan(
                    _model_text(response),
                    available_evidence_ids=set(evidence_ids),
                    proposal_tools=proposal_tools,
                )
            except Exception:
                plan = _deterministic_proposal_fallback(
                    candidate,
                    proposal_definitions=proposal_definitions,
                ) or _fallback_recovery_plan(
                    candidate,
                    proposal_tools=proposal_tools,
                )
        payload = _recovery_plan_payload(plan)
        if model_runtime is not None:
            payload["modelCallCount"] = model_runtime.budget.used
            payload["modelCallAudits"] = model_runtime.audits
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="recovery_planning",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "recovery_planning", payload)
        update: dict[str, object] = {
            "recovery_plan": payload,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    f"Recovery Planner: classified plan as {plan.mode}.",
                    89,
                )
            ],
        }
        if model_runtime is not None:
            update["model_call_count"] = model_runtime.budget.used
            update["model_call_audits"] = model_runtime.audits
        return update

    async def _policy_gate(
        self,
        state: AiopsDiagnosticState,
    ) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        plan = _recovery_plan_from_payload(state.get("recovery_plan"))
        if plan is None or plan.mode == "no_action":
            decision = RecoveryPolicyDecision(
                status="deferred",
                authorization_code="no_grounded_action",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=False,
                summary="No grounded recovery action was authorized.",
            )
        elif plan.mode == "external_policy_required":
            decision = RecoveryPolicyDecision(
                status="deferred",
                authorization_code="external_policy_required",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=plan.human_approval_required,
                summary="The existing external recovery policy must revalidate this action.",
            )
        elif plan.mode == "manual_review":
            decision = RecoveryPolicyDecision(
                status="deferred",
                authorization_code="manual_review_required",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=True,
                summary="A human must review the recovery recommendation.",
            )
        else:
            decision = await self._record_proposal(
                state=state,
                plan=plan,
            )
        payload = _recovery_policy_payload(decision)
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="policy_gate",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "policy_gate", payload)
        return {
            "recovery_policy": payload,
            "events": [
                _task_status_event(
                    task_id,
                    "running",
                    f"Policy Gate: {decision.authorization_code}.",
                    90,
                )
            ],
        }

    async def _record_proposal(
        self,
        *,
        state: AiopsDiagnosticState,
        plan: RecoveryPlan,
    ) -> RecoveryPolicyDecision:
        tool_name = plan.tool or ""
        if self._tool_policies.get(tool_name) != "proposal_only":
            return RecoveryPolicyDecision(
                status="denied",
                authorization_code="proposal_tool_not_allowed",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=plan.human_approval_required,
                summary="The proposal tool is not allowed by this request's policy.",
            )
        if not plan.human_approval_required:
            return RecoveryPolicyDecision(
                status="denied",
                authorization_code="human_approval_required",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=False,
                summary="A proposal must require human approval before any later action.",
            )

        definitions = tuple(state.get("tool_definitions") or ())
        proposal_step: JsonDict = {
            "tool": tool_name,
            "arguments": dict(plan.arguments),
        }
        if not plan_matches_tool_contracts([proposal_step], definitions):
            return RecoveryPolicyDecision(
                status="denied",
                authorization_code="proposal_schema_invalid",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=plan.human_approval_required,
                summary="The proposal arguments do not match the discovered tool contract.",
            )

        owner_user_id = str(state["owner_user_id"])
        task_id = str(state["task_id"])
        recovery_intent_id = _stable_public_id(
            "recovery_intent",
            task_id,
            plan.action,
            plan.target,
            json.dumps(plan.arguments, sort_keys=True, separators=(",", ":")),
        )
        audit_id = _stable_public_id("tool", recovery_intent_id, tool_name)
        try:
            await self._create_audit(
                owner_user_id=owner_user_id,
                task_id=task_id,
                audit_id=audit_id,
                tool_name=tool_name,
                arguments=dict(plan.arguments),
            )
            mcp_client = await self._mcp_client_for(owner_user_id)
            output = await mcp_client.call_tool(tool_name, dict(plan.arguments))
            await self._finalize_audit(
                owner_user_id=owner_user_id,
                audit_id=audit_id,
                status="completed",
                result_summary=_tool_result_summary(tool_name, output),
            )
        except Exception as exc:
            safe_error = _safe_error(exc)
            try:
                await self._finalize_audit(
                    owner_user_id=owner_user_id,
                    audit_id=audit_id,
                    status="failed",
                    error_message=safe_error,
                )
            except Exception:
                pass
            return RecoveryPolicyDecision(
                status="denied",
                authorization_code="proposal_record_failed",
                execution_permitted=False,
                proposal_recorded=False,
                human_approval_required=plan.human_approval_required,
                summary="The proposal could not be recorded safely.",
            )

        return RecoveryPolicyDecision(
            status="allowed",
            authorization_code="proposal_recorded",
            execution_permitted=False,
            proposal_recorded=True,
            human_approval_required=True,
            summary="The side-effect-free proposal was recorded for human review.",
        )

    async def _report(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        evidence = cast(list[JsonDict], state.get("evidence") or [])
        evidence_ids = _unique_strings(cast(list[str], state.get("evidence_ids") or []))
        no_sop_matched = bool(state.get("no_sop_matched"))
        status: Literal["succeeded", "failed"] = (
            "failed" if bool(state.get("execution_failed")) else "succeeded"
        )
        events = [
            _task_status_event(task_id, "running", "Report: compiling evidence-backed report.", 90)
        ]
        model_runtime = (
            self._model_runtime(state)
            if state.get("workflow_version") == "evidence-driven-v4"
            else None
        )
        report_content, report_generation = await self._generate_report_content(
            state,
            model_runtime=model_runtime,
        )
        report_payload: JsonDict = {
            "workflowVersion": str(
                state.get("workflow_version") or "evidence-driven-v3"
            ),
            "noSopMatched": no_sop_matched,
            "plan": _json_list(state.get("plan")),
            "planOrigin": str(state.get("plan_origin") or "generic"),
            "evidence": evidence,
            "evidenceIds": evidence_ids,
            "reportGeneration": report_generation,
            "status": status,
            "rootCauseDecision": state.get("root_cause_decision"),
            "decisionValidation": state.get("decision_validation"),
            "recoveryPlan": state.get("recovery_plan"),
            "recoveryPolicy": state.get("recovery_policy"),
            "evidenceSufficiency": state.get("evidence_sufficiency"),
            "terminationReason": str(state.get("termination_reason") or ""),
            "modelCallCount": (
                model_runtime.budget.used
                if model_runtime is not None
                else int(state.get("model_call_count") or 0)
            ),
            "validatorRouting": state.get("validator_routing"),
        }
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="report",
            status="completed",
            payload={"status": status, "evidenceIds": evidence_ids},
        )
        report = await self._repositories.diagnostics.add_report(
            owner_user_id=owner_user_id,
            report_id=_stable_public_id("report", task_id, "diagnostic"),
            task_id=task_id,
            title=AIOPS_REPORT_TITLE,
            content=report_content,
            payload=report_payload,
        )
        for evidence_id in evidence_ids:
            await self._repositories.diagnostics.link_report_evidence(
                owner_user_id=owner_user_id,
                link_id=_stable_public_id(
                    "report_evidence", task_id, report.id, evidence_id
                ),
                task_id=task_id,
                report_id=report.id,
                evidence_id=evidence_id,
            )
        result_payload: JsonDict = {**report_payload, "reportId": report.id}
        updated_task = await self._repositories.diagnostics.update_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status=status,
            result_payload=result_payload,
            completed_at=_now(),
        )
        if updated_task is None:
            raise RuntimeError("Diagnostic task disappeared during report persistence.")
        if status == "succeeded" and self._case_persistor is not None:
            case = await self._case_persistor.persist(task=updated_task, report=report)
            refreshed_task = await self._repositories.diagnostics.update_task(
                owner_user_id=owner_user_id,
                task_id=task_id,
                status=status,
                result_payload={**result_payload, "diagnosticCaseId": case.id},
                completed_at=_now(),
            )
            if refreshed_task is not None:
                updated_task = refreshed_task
        await self._save_checkpoint(
            state,
            "report",
            {"reportId": report.id, "status": status, "resultPayload": result_payload},
        )
        events.extend(
            [
                _sse_event(
                    "report",
                    {
                        "report": {
                            "id": report.id,
                            "title": report.title,
                            "content": report.content,
                            "format": "markdown",
                        }
                    },
                ),
                _task_status_event(task_id, status, "Report: diagnostic finished.", 100),
                _sse_event(
                    "complete",
                    {
                        "result": {
                            "task": _task_payload(updated_task),
                            "report": _report_payload(report),
                        }
                    },
                ),
            ]
        )
        update: dict[str, object] = {"report_id": report.id, "events": events}
        if model_runtime is not None:
            update["model_call_count"] = model_runtime.budget.used
            update["model_call_audits"] = model_runtime.audits
        return update

    async def _generate_report_content(
        self,
        state: AiopsDiagnosticState,
        *,
        model_runtime: _ModelRuntime | None = None,
    ) -> tuple[str, str]:
        fallback = _fallback_report_content(
            alert=_json_dict(state.get("alert")),
            no_sop_matched=bool(state.get("no_sop_matched")),
            sop_hits=cast(list[JsonDict], state.get("sop_hits") or []),
            evidence=cast(list[JsonDict], state.get("evidence") or []),
            execution_failed=bool(state.get("execution_failed")),
        )
        prompt = _report_prompt(state)
        try:
            response = (
                await self._invoke_v4_model(
                    model_runtime,
                    role="report",
                    prompt=prompt,
                )
                if model_runtime is not None
                else await self._llm_provider.create_chat_model().ainvoke(prompt)
            )
            if response is None:
                return fallback, "fallback"
        except Exception:
            return fallback, "fallback"
        report = _clean_markdown_report(_model_text(response))
        return (report, "llm") if report is not None else (fallback, "fallback")

    def _route_after_sufficiency(self, state: AiopsDiagnosticState) -> str:
        return str(state.get("next_route") or "decision")

    def _route_after_replanner(self, state: AiopsDiagnosticState) -> str:
        plan = cast(list[JsonDict], state.get("plan") or [])
        plan_index = int(state.get("plan_index") or 0)
        attempts = int(state.get("executor_attempt_count") or 0)
        maximum = int(state.get("max_total_steps") or 6)
        return "executor" if plan_index < len(plan) and attempts < maximum else "decision"

    def _route_after_decision_validation(self, state: AiopsDiagnosticState) -> str:
        return str(state.get("next_route") or "recovery_planner")

    def _route_after_deterministic_validation_v4(
        self, state: AiopsDiagnosticState
    ) -> str:
        return str(state.get("next_route") or "manual_review")

    def _route_after_validator_router_v4(self, state: AiopsDiagnosticState) -> str:
        return str(state.get("next_route") or "policy_gate")

    async def _create_plan(
        self,
        *,
        query: str,
        alert: JsonDict,
        sop_hits: Sequence[JsonDict],
        no_sop_matched: bool,
        tool_definitions: Sequence[McpToolDefinition],
        known_hypotheses: Sequence[str],
        model_runtime: _ModelRuntime | None = None,
    ) -> tuple[list[JsonDict], str]:
        available_tools = [definition.name for definition in tool_definitions]
        generic_plan = build_generic_live_plan(
            available_tools=available_tools,
            known_hypotheses=known_hypotheses,
        )
        if not generic_plan and "SearchLog" in available_tools:
            generic_plan = [self._generic_search_log_step(query)]
        generic_plan, _generic_contract_errors = normalize_tool_plan_steps(
            generic_plan,
            trusted_tool_arguments=self._trusted_tool_arguments,
            tool_argument_contracts=self._tool_argument_contracts,
            tool_definitions=tool_definitions,
        )
        generic_plan = list(repair_plan_causal_coverage(generic_plan).steps)
        prompt = (
            "Return JSON only for a bounded diagnostic plan with a `steps` array. Each step "
            "has `id`, `tool`, `arguments`, `purpose`, `testsHypotheses`, and "
            "`causalIntent`, plus optional `evidenceRules`. causalIntent must be allowed by "
            "the selected tool contract. evidenceRules may only contain templateId, "
            "hypothesisId, and the exact bounded parameters from this trusted catalog: "
            f"{json.dumps(trusted_evidence_rule_catalog(), ensure_ascii=False)}. "
            "Do not define a predicate, disposition, reason code, scenario ID, Oracle, or "
            "unavailable tool in evidenceRules. "
            "Use at most six "
            "steps and only the tools and argument schemas in these discovered contracts. "
            "The initial plan must contain at most four steps: "
            f"{json.dumps(_tool_contracts_payload(tool_definitions), ensure_ascii=False)}. "
            f"User query: {query}. Alert: "
            f"{json.dumps(alert)}. SOP evidence: {json.dumps(list(sop_hits))}. "
            f"Known hypotheses: {json.dumps(list(known_hypotheses))}. "
            f"No SOP matched: {str(no_sop_matched).lower()}."
        )
        try:
            response = (
                await self._invoke_v4_model(
                    model_runtime,
                    role="planner",
                    prompt=prompt,
                )
                if model_runtime is not None
                else await self._llm_provider.create_chat_model().ainvoke(prompt)
            )
            if response is None:
                raise RuntimeError("Planner model call was unavailable.")
            parsed_plan = parse_plan(
                _model_text(response),
                available_tools=set(available_tools),
                known_hypotheses=set(known_hypotheses),
                causal_capabilities={
                    definition.name: allowed_causal_intents(definition.name)
                    for definition in tool_definitions
                },
            )
            if len(parsed_plan) > 4:
                raise ValueError("Initial diagnostic plan cannot contain more than four steps.")
            plan = [_diagnostic_plan_step_payload(step) for step in parsed_plan]
        except Exception:
            plan = []
        plan, _contract_errors = normalize_tool_plan_steps(
            plan,
            trusted_tool_arguments=self._trusted_tool_arguments,
            tool_argument_contracts=self._tool_argument_contracts,
            tool_definitions=tool_definitions,
        )
        plan = list(repair_plan_causal_coverage(plan).steps)
        if not plan:
            return generic_plan, "generic"
        return plan, "SOP-backed" if sop_hits else "model"

    def _generic_search_log_step(self, query: str) -> JsonDict:
        now_ms = int(_now().timestamp() * 1000)
        return {
            "id": "search-cls-logs",
            "tool": "SearchLog",
            "arguments": {
                "Region": self._cls_region,
                "TopicId": self._cls_topic_id,
                "From": now_ms - 86_400_000,
                "To": now_ms,
                "Query": "*",
                "Limit": 20,
            },
            "purpose": f"Gather real CLS evidence relevant to: {query}",
            "testsHypotheses": [],
            "causalIntent": "context",
            "causalIntentOrigin": "generic",
        }

    async def _create_step(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        phase: str,
        status: str,
        payload: JsonDict,
    ) -> DiagnosticStepRecord:
        async with self._step_sequence_lock:
            existing_steps = await self._repositories.diagnostics.list_steps(
                owner_user_id=owner_user_id,
                task_id=task_id,
            )
            return await self._repositories.diagnostics.create_step(
                owner_user_id=owner_user_id,
                step_id=_stable_public_id(
                    "diagnostic_step",
                    task_id,
                    phase,
                    json.dumps(
                        _safe_value(payload),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                ),
                task_id=task_id,
                sequence=len(existing_steps) + 1,
                phase=phase,
                status=status,
                payload=payload,
            )

    async def _create_audit(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        audit_id: str,
        tool_name: str,
        arguments: JsonDict,
    ) -> None:
        repository = self._repositories.tool_call_audits
        if repository is not None:
            await repository.create_for_diagnostic_task(
                owner_user_id=owner_user_id,
                audit_id=audit_id,
                diagnostic_task_id=task_id,
                tool_name=tool_name,
                arguments=arguments,
            )

    async def _finalize_audit(
        self,
        *,
        owner_user_id: str,
        audit_id: str,
        status: str,
        result_summary: str | None = None,
        error_message: str | None = None,
    ) -> None:
        repository = self._repositories.tool_call_audits
        if repository is not None:
            await repository.finalize(
                owner_user_id=owner_user_id,
                audit_id=audit_id,
                status=status,
                result_summary=result_summary,
                error_message=error_message,
            )

    async def _save_checkpoint(
        self,
        state: AiopsDiagnosticState,
        node: str,
        payload: JsonDict,
    ) -> None:
        task_id = str(state["task_id"])
        payload_model_count = payload.get("modelCallCount")
        model_call_count = (
            payload_model_count
            if isinstance(payload_model_count, int)
            and not isinstance(payload_model_count, bool)
            else int(state.get("model_call_count") or 0)
        )
        runtime_payload: JsonDict = {
            "modelCallCount": model_call_count,
            "modelCallAudits": _json_list(payload.get("modelCallAudits")),
            "startedAt": str(state.get("started_at") or ""),
            "softDeadlineAt": str(state.get("soft_deadline_at") or ""),
            "hardDeadlineAt": str(state.get("hard_deadline_at") or ""),
            "replanCount": int(state.get("replan_count") or 0),
            "maxReplans": int(state.get("max_replans") or 0),
        }
        logical_iteration = int(state.get("plan_index") or 0) + int(
            state.get("replan_count") or 0
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                _safe_value({**payload, "executionRuntime": runtime_payload}),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        stable_id = f"{node}_{logical_iteration}_{fingerprint[:32]}"
        graph_version = str(
            state.get("graph_version") or AIOPS_LEGACY_GRAPH_VERSION
        )
        await self._repositories.diagnostics.save_checkpoint(
            owner_user_id=str(state["owner_user_id"]),
            checkpoint_record_id=f"checkpoint_{stable_id}",
            task_id=task_id,
            thread_id=f"aiops:{task_id}:{graph_version}",
            checkpoint_ns=node,
            checkpoint_id=stable_id,
            checkpoint_payload={**payload, "executionRuntime": runtime_payload},
            metadata={"node": node, "graphVersion": graph_version},
        )


def _investigation_dispatch_payload(dispatch: InvestigationDispatch) -> JsonDict:
    return {
        "taskId": dispatch.task_id,
        "ownerUserId": dispatch.owner_user_id,
        "dispatchId": dispatch.dispatch_id,
        "dispatchKey": dispatch.dispatch_key,
        "investigatorType": dispatch.investigator_type,
        "objective": dispatch.objective,
        "testsHypotheses": list(dispatch.tests_hypotheses),
        "missingCausalRoles": list(dispatch.missing_causal_roles),
        "steps": [dict(step) for step in dispatch.steps],
        "allowedTools": sorted(dispatch.allowed_tools),
        "existingEvidenceIds": list(dispatch.existing_evidence_ids),
        "deadlineMs": dispatch.deadline_ms,
        "modelCallBudget": dispatch.model_call_budget,
    }


def _evidence_packet_payload(packet: EvidencePacket) -> JsonDict:
    return {
        "taskId": packet.task_id,
        "ownerUserId": packet.owner_user_id,
        "dispatchId": packet.dispatch_id,
        "investigatorType": packet.investigator_type,
        "status": packet.status,
        "claims": [
            {
                "claimId": claim.claim_id,
                "value": _safe_value(claim.value),
                "quality": claim.quality,
                "causalRole": claim.causal_role,
                "supports": list(claim.supports),
                "refutes": list(claim.refutes),
                "evidenceIds": list(claim.evidence_ids),
                "targetComponent": claim.target_component,
                "observedAt": (
                    claim.observed_at.isoformat()
                    if claim.observed_at is not None
                    else None
                ),
                "timeScope": claim.time_scope,
            }
            for claim in packet.claims
        ],
        "limitations": list(packet.limitations),
        "toolCallIds": list(packet.tool_call_ids),
        "modelCallsUsed": packet.model_calls_used,
    }


def _evidence_packet_from_payload(payload: Mapping[str, object]) -> EvidencePacket:
    _reject_unknown_packet_fields(
        payload,
        frozenset(
            {
                "taskId",
                "ownerUserId",
                "dispatchId",
                "investigatorType",
                "status",
                "claims",
                "limitations",
                "toolCallIds",
                "modelCallsUsed",
            }
        ),
    )
    claims: list[EvidenceClaim] = []
    for raw_claim in _json_list(payload.get("claims")):
        _reject_unknown_packet_fields(
            raw_claim,
            frozenset(
                {
                    "claimId",
                    "value",
                    "quality",
                    "causalRole",
                    "supports",
                    "refutes",
                    "evidenceIds",
                    "targetComponent",
                    "observedAt",
                    "timeScope",
                }
            ),
        )
        observed_value = raw_claim.get("observedAt")
        claims.append(
            EvidenceClaim(
                claim_id=str(raw_claim.get("claimId") or ""),
                value=cast(Any, raw_claim.get("value")),
                quality=cast(Any, raw_claim.get("quality")),
                causal_role=cast(str | None, raw_claim.get("causalRole")),
                supports=tuple(
                    str(item)
                    for item in cast(list[object], raw_claim.get("supports") or [])
                ),
                refutes=tuple(
                    str(item)
                    for item in cast(list[object], raw_claim.get("refutes") or [])
                ),
                evidence_ids=tuple(
                    str(item)
                    for item in cast(list[object], raw_claim.get("evidenceIds") or [])
                ),
                target_component=str(raw_claim.get("targetComponent") or ""),
                observed_at=(
                    datetime.fromisoformat(observed_value)
                    if isinstance(observed_value, str)
                    else None
                ),
                time_scope=cast(Any, raw_claim.get("timeScope")),
            )
        )
    model_calls_value = payload.get("modelCallsUsed")
    model_calls_used = (
        model_calls_value
        if isinstance(model_calls_value, int) and not isinstance(model_calls_value, bool)
        else 0
    )
    return EvidencePacket(
        task_id=str(payload.get("taskId") or ""),
        owner_user_id=str(payload.get("ownerUserId") or ""),
        dispatch_id=str(payload.get("dispatchId") or ""),
        investigator_type=cast(Any, payload.get("investigatorType")),
        status=cast(Any, payload.get("status")),
        claims=tuple(claims),
        limitations=tuple(
            str(item)
            for item in cast(list[object], payload.get("limitations") or [])
        ),
        tool_call_ids=tuple(
            str(item)
            for item in cast(list[object], payload.get("toolCallIds") or [])
        ),
        model_calls_used=model_calls_used,
    )


def _reject_unknown_packet_fields(
    payload: Mapping[str, object], allowed: frozenset[str]
) -> None:
    if set(payload) - allowed:
        raise ValueError("Evidence packet contains unknown fields.")


def _diagnostic_plan_step_payload(step: DiagnosticPlanStep) -> JsonDict:
    trusted_catalog = {
        (str(item["templateId"]), str(item["hypothesisId"])): item
        for item in trusted_evidence_rule_catalog()
    }
    return {
        "id": step.id,
        "tool": step.tool,
        "arguments": step.arguments,
        "purpose": step.purpose,
        "testsHypotheses": list(step.tests_hypotheses),
        "causalIntent": step.causal_intent,
        "causalIntentOrigin": step.causal_intent_origin,
        "evidenceRules": [
            {
                "templateId": rule.template_id,
                "hypothesisId": rule.hypothesis_id,
                "parameters": dict(
                    cast(
                        Mapping[str, object],
                        trusted_catalog[(rule.template_id, rule.hypothesis_id)][
                            "parameters"
                        ],
                    )
                ),
            }
            for rule in step.evidence_rules
            if (rule.template_id, rule.hypothesis_id) in trusted_catalog
        ],
    }


def _step_fingerprint(step: Mapping[str, object]) -> str:
    return tool_step_fingerprint(
        str(step.get("tool") or ""),
        _json_dict(step.get("arguments")),
    )


def _next_open_hypothesis_step_index(
    *,
    plan: Sequence[JsonDict],
    plan_index: int,
    open_hypothesis_ids: Sequence[str],
    executed_fingerprints: Sequence[str],
) -> int | None:
    open_ids = set(open_hypothesis_ids)
    if not open_ids:
        return None
    executed = set(executed_fingerprints)
    bounded_index = min(max(plan_index, 0), len(plan))
    candidate_indices = [
        *range(bounded_index, len(plan)),
        *range(0, bounded_index),
    ]
    for index in candidate_indices:
        step = plan[index]
        if _step_fingerprint(step) in executed:
            continue
        tested = {
            item
            for item in cast(list[object], step.get("testsHypotheses") or [])
            if isinstance(item, str)
        }
        if tested & open_ids:
            return index
    return None


def _can_replan(state: AiopsDiagnosticState) -> bool:
    return (
        int(state.get("replan_count") or 0) < int(state.get("max_replans") or 2)
        and int(state.get("executor_attempt_count") or 0)
        < int(state.get("max_total_steps") or 6)
    )


def _budget_termination_reason(state: AiopsDiagnosticState) -> str:
    if int(state.get("executor_attempt_count") or 0) >= int(
        state.get("max_total_steps") or 6
    ):
        return "step_budget_exhausted"
    if int(state.get("replan_count") or 0) >= int(state.get("max_replans") or 2):
        return "replan_limit_reached"
    return "no_useful_step"


def _fallback_evidence_sufficiency(
    *,
    public_hypotheses: Sequence[JsonDict],
    hypothesis_states: Sequence[JsonDict],
    evidence_ids: Sequence[str],
) -> EvidenceSufficiencyDecision:
    fallback = EvidenceSufficiencyDecision(
        status="insufficient",
        evidence_ids=tuple(_unique_strings(list(evidence_ids))[:6]),
        supported_hypotheses=(),
        refuted_hypotheses=(),
        unresolved_hypotheses=(),
        missing_evidence=("Structured sufficiency assessment was unavailable.",),
        recommended_tools=(),
        summary="Evidence sufficiency could not be confirmed.",
    )
    return _project_evidence_sufficiency(
        model_decision=fallback,
        public_hypotheses=public_hypotheses,
        hypothesis_states=hypothesis_states,
        evidence_ids=evidence_ids,
    )


def _project_evidence_sufficiency(
    *,
    model_decision: EvidenceSufficiencyDecision,
    public_hypotheses: Sequence[JsonDict],
    hypothesis_states: Sequence[JsonDict],
    evidence_ids: Sequence[str],
) -> EvidenceSufficiencyDecision:
    public_ids = _unique_strings(
        [str(item.get("id") or "") for item in public_hypotheses]
    )
    public_set = set(public_ids)
    state_by_id: dict[str, JsonDict] = {}
    state_integrity_valid = True
    for item in hypothesis_states:
        hypothesis_id = str(item.get("id") or "")
        if not hypothesis_id or hypothesis_id not in public_set:
            state_integrity_valid = False
            continue
        if hypothesis_id in state_by_id:
            state_integrity_valid = False
            continue
        state_by_id[hypothesis_id] = item

    supported: list[str] = []
    refuted: list[str] = []
    unresolved: list[str] = []
    for hypothesis_id in public_ids:
        status = str(state_by_id.get(hypothesis_id, {}).get("status") or "open")
        if status == "supported":
            supported.append(hypothesis_id)
        elif status == "refuted":
            refuted.append(hypothesis_id)
        else:
            unresolved.append(hypothesis_id)

    sufficient = (
        state_integrity_valid and len(supported) == 1 and not unresolved
    )
    return EvidenceSufficiencyDecision(
        status="sufficient" if sufficient else "insufficient",
        evidence_ids=tuple(_unique_strings(list(evidence_ids))[:6]),
        supported_hypotheses=tuple(supported),
        refuted_hypotheses=tuple(refuted),
        unresolved_hypotheses=tuple(unresolved),
        missing_evidence=() if sufficient else model_decision.missing_evidence,
        recommended_tools=() if sufficient else model_decision.recommended_tools,
        summary=model_decision.summary,
    )


def _evidence_sufficiency_payload(
    decision: EvidenceSufficiencyDecision,
) -> JsonDict:
    return {
        "status": decision.status,
        "evidenceIds": list(decision.evidence_ids),
        "supportedHypotheses": list(decision.supported_hypotheses[:6]),
        "refutedHypotheses": list(decision.refuted_hypotheses[:6]),
        "unresolvedHypotheses": list(decision.unresolved_hypotheses[:6]),
        "missingEvidence": list(decision.missing_evidence),
        "recommendedTools": list(decision.recommended_tools),
        "summary": decision.summary,
    }


def _new_hypothesis_assessment(hypothesis_id: str) -> HypothesisAssessment:
    return HypothesisAssessment(
        hypothesis_id=hypothesis_id,
        disposition="unresolved",
        evidence_ids=(),
        reason_code="awaiting_public_evidence",
        assessment_source="deterministic",
    )


def _can_adjudicate_new_evidence(
    state: Mapping[str, object],
    *,
    fact_count: int,
) -> bool:
    raw_adjudication_count = state.get("adjudication_count")
    adjudication_count = (
        raw_adjudication_count
        if isinstance(raw_adjudication_count, int)
        and not isinstance(raw_adjudication_count, bool)
        else 0
    )
    if adjudication_count == 0:
        return True
    if adjudication_count >= 2:
        return False
    prior_fact_count = state.get("adjudicated_fact_count")
    return isinstance(prior_fact_count, int) and fact_count > prior_fact_count


def _step_targets_replan_gap(
    step: Mapping[str, object],
    *,
    replan_reason: str,
    missing_roles: set[str],
) -> bool:
    if replan_reason == "decision_validation_gap":
        return True
    return not missing_roles or step.get("causalIntent") in missing_roles


def _deterministic_gap_replan_steps(
    state: Mapping[str, object],
    *,
    available_tools: set[str],
) -> list[JsonDict]:
    """Derive a bounded refinement step from public, persisted diagnostic facts."""
    if "ProbeUpstreamHealth" not in available_tools:
        return []
    missing_roles = {
        item
        for item in cast(
            list[object],
            _json_dict(state.get("evidence_sufficiency")).get("missingCausalRoles")
            or [],
        )
        if isinstance(item, str)
    }
    if "trigger" not in missing_roles:
        return []
    supported = [
        assessment.hypothesis_id
        for assessment in _hypothesis_assessments_from_payload(
            cast(list[JsonDict], state.get("hypothesis_assessments") or [])
        )
        if assessment.disposition == "supported"
    ]
    if supported != ["nginx_upstream_response_timeout"]:
        return []
    upstream_services = {
        fact.value.strip()
        for fact in _diagnostic_facts_from_payload(
            cast(list[JsonDict], state.get("diagnostic_facts") or [])
        )
        if fact.public
        and fact.key == "InspectGatewayRequestTimeline.upstreamService"
        and isinstance(fact.value, str)
        and fact.value.strip()
    }
    if len(upstream_services) != 1:
        return []
    return [
        {
            "id": "refine_upstream_deadline",
            "tool": "ProbeUpstreamHealth",
            "arguments": {"service": next(iter(upstream_services))},
            "purpose": "Probe the public upstream endpoint against its gateway deadline.",
            "testsHypotheses": ["nginx_upstream_response_timeout"],
            "causalIntent": "mechanism",
            "causalIntentOrigin": "coverage_repair",
            "evidenceRules": [],
        }
    ]


def _model_call_audit_payload(
    *,
    role: ModelRole,
    attempt: int,
    duration_ms: int,
    safe_error_code: str | None,
) -> JsonDict:
    return {
        "role": role,
        "attempt": attempt,
        "durationMs": duration_ms,
        "cacheHit": False,
        "safeErrorCode": safe_error_code,
    }


def _stable_public_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode()).hexdigest()
    return f"{prefix}_{digest[:48]}"


def _safe_model_call_error_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection"
    return "model_call_failed"


def _is_transient_infrastructure_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = exc.__class__.__name__.casefold()
    return any(
        token in name
        for token in (
            "connection",
            "timeout",
            "cannotconnect",
            "operationalerror",
            "interfaceerror",
        )
    )


def _execution_deadlines_from_state(state: AiopsDiagnosticState) -> ExecutionDeadlines:
    started_at = state.get("started_at")
    soft_deadline_at = state.get("soft_deadline_at")
    hard_deadline_at = state.get("hard_deadline_at")
    if all(
        isinstance(value, str)
        for value in (started_at, soft_deadline_at, hard_deadline_at)
    ):
        try:
            return ExecutionDeadlines.from_iso(
                started_at=cast(str, started_at),
                soft_deadline_at=cast(str, soft_deadline_at),
                hard_deadline_at=cast(str, hard_deadline_at),
            )
        except ValueError:
            pass
    return ExecutionDeadlines.start(_now())


def _risk_tier(value: object) -> RiskTier:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"l3", "critical", "very high"}:
            return "L3"
        if normalized in {"l2", "high"}:
            return "L2"
        if normalized in {"l1", "medium", "moderate"}:
            return "L1"
    return "L0"


def _initial_hypothesis_assessments(
    public_hypotheses: Sequence[JsonDict],
) -> list[JsonDict]:
    return [
        _hypothesis_assessment_payload(_new_hypothesis_assessment(str(item["id"])))
        for item in public_hypotheses
        if item.get("id")
    ]


def _hypothesis_assessment_payload(assessment: HypothesisAssessment) -> JsonDict:
    return {
        "hypothesisId": assessment.hypothesis_id,
        "disposition": assessment.disposition,
        "evidenceIds": list(assessment.evidence_ids),
        "reasonCode": assessment.reason_code,
        "assessmentSource": assessment.assessment_source,
        "hasHighQualityConflict": assessment.has_high_quality_conflict,
        "transitions": [
            {
                "previousDisposition": transition.previous_disposition,
                "nextDisposition": transition.next_disposition,
                "evidenceIds": list(transition.evidence_ids),
                "reasonCode": transition.reason_code,
                "assessmentSource": transition.assessment_source,
            }
            for transition in assessment.transitions
        ],
    }


def _hypothesis_assessments_from_payload(
    payloads: Sequence[JsonDict],
) -> tuple[HypothesisAssessment, ...]:
    assessments: list[HypothesisAssessment] = []
    for payload in payloads:
        hypothesis_id = payload.get("hypothesisId")
        disposition = payload.get("disposition")
        reason_code = payload.get("reasonCode")
        source = payload.get("assessmentSource")
        if (
            not isinstance(hypothesis_id, str)
            or disposition not in {"supported", "refuted", "causally_inactive", "unresolved"}
            or not isinstance(reason_code, str)
            or source not in {"deterministic", "llm_adjudicated"}
        ):
            continue
        transitions: list[HypothesisTransition] = []
        for raw_transition in cast(list[object], payload.get("transitions") or []):
            if not isinstance(raw_transition, Mapping):
                continue
            transition = cast(Mapping[str, object], raw_transition)
            previous = transition.get("previousDisposition")
            next_disposition = transition.get("nextDisposition")
            transition_reason = transition.get("reasonCode")
            transition_source = transition.get("assessmentSource")
            if (
                previous not in {"supported", "refuted", "causally_inactive", "unresolved"}
                or next_disposition
                not in {"supported", "refuted", "causally_inactive", "unresolved"}
                or not isinstance(transition_reason, str)
                or transition_source not in {"deterministic", "llm_adjudicated"}
            ):
                continue
            transitions.append(
                HypothesisTransition(
                    previous_disposition=cast(Any, previous),
                    next_disposition=cast(Any, next_disposition),
                    evidence_ids=tuple(
                        item
                        for item in cast(list[object], transition.get("evidenceIds") or [])
                        if isinstance(item, str)
                    ),
                    reason_code=transition_reason,
                    assessment_source=cast(Any, transition_source),
                )
            )
        try:
            assessments.append(
                HypothesisAssessment(
                    hypothesis_id=hypothesis_id,
                    disposition=cast(Any, disposition),
                    evidence_ids=tuple(
                        item
                        for item in cast(list[object], payload.get("evidenceIds") or [])
                        if isinstance(item, str)
                    ),
                    reason_code=reason_code,
                    assessment_source=cast(Any, source),
                    has_high_quality_conflict=bool(
                        payload.get("hasHighQualityConflict")
                    ),
                    transitions=tuple(transitions),
                )
            )
        except ValueError:
            continue
    return tuple(sorted(assessments, key=lambda item: item.hypothesis_id))


def _hypothesis_state_payload(state: HypothesisState) -> JsonDict:
    return {
        "id": state.id,
        "status": state.status,
        "confidence": state.confidence,
        "evidenceIds": list(state.evidence_ids),
    }


def _diagnostic_fact_payload(fact: DiagnosticFact) -> JsonDict:
    return {
        "key": fact.key,
        "value": _safe_value(fact.value),
        "evidenceId": fact.evidence_id,
        "sourceTool": fact.source_tool,
        "quality": fact.quality,
        "public": fact.public,
    }


def _diagnostic_facts_from_payload(
    payloads: Sequence[JsonDict],
) -> tuple[DiagnosticFact, ...]:
    facts: list[DiagnosticFact] = []
    for payload in payloads:
        key = payload.get("key")
        evidence_id = payload.get("evidenceId")
        source_tool = payload.get("sourceTool")
        quality = payload.get("quality")
        if (
            not isinstance(key, str)
            or not isinstance(evidence_id, str)
            or not isinstance(source_tool, str)
            or quality not in {"direct", "context"}
        ):
            continue
        value = payload.get("value")
        if isinstance(value, list):
            value = tuple(value)
        try:
            facts.append(
                DiagnosticFact(
                    key=key,
                    value=value,
                    evidence_id=evidence_id,
                    source_tool=source_tool,
                    quality=cast(Any, quality),
                    public=payload.get("public") is not False,
                )
            )
        except ValueError:
            continue
    return _deduplicate_diagnostic_facts(facts)


def _deduplicate_diagnostic_facts(
    facts: Sequence[DiagnosticFact],
) -> tuple[DiagnosticFact, ...]:
    indexed = {
        (fact.key, fact.evidence_id, repr(fact.value), fact.quality): fact
        for fact in facts
        if fact.public
    }
    return tuple(indexed[key] for key in sorted(indexed))


def _trusted_rules_from_plan(
    plan: Sequence[JsonDict],
) -> tuple[HypothesisEvidenceRule, ...]:
    rules: dict[tuple[str, str], HypothesisEvidenceRule] = {}
    catalog = trusted_evidence_rule_catalog()
    for step in plan:
        tool = step.get("tool")
        if not isinstance(tool, str):
            continue
        for raw_rule in cast(list[object], step.get("evidenceRules") or []):
            if not isinstance(raw_rule, Mapping):
                continue
            rule_payload = cast(Mapping[str, object], raw_rule)
            template_id = rule_payload.get("templateId")
            hypothesis_id = rule_payload.get("hypothesisId")
            parameters = rule_payload.get("parameters")
            if (
                not isinstance(template_id, str)
                or not isinstance(hypothesis_id, str)
                or not isinstance(parameters, Mapping)
                or not all(isinstance(key, str) for key in parameters)
            ):
                continue
            rule = instantiate_trusted_evidence_rule(
                template_id=template_id,
                hypothesis_id=hypothesis_id,
                parameters=cast(Mapping[str, object], parameters),
                step_tool=tool,
            )
            if rule is not None:
                rules[(rule.template_id, rule.hypothesis_id)] = rule
        tested_hypotheses = {
            value
            for value in cast(list[object], step.get("testsHypotheses") or [])
            if isinstance(value, str)
        }
        for catalog_item in catalog:
            template_id = catalog_item.get("templateId")
            hypothesis_id = catalog_item.get("hypothesisId")
            parameters = catalog_item.get("parameters")
            if (
                catalog_item.get("tool") != tool
                or not isinstance(template_id, str)
                or not isinstance(hypothesis_id, str)
                or hypothesis_id not in tested_hypotheses
                or not isinstance(parameters, Mapping)
            ):
                continue
            rule = instantiate_trusted_evidence_rule(
                template_id=template_id,
                hypothesis_id=hypothesis_id,
                parameters=cast(Mapping[str, object], parameters),
                step_tool=tool,
            )
            if rule is not None:
                rules[(rule.template_id, rule.hypothesis_id)] = rule
    return tuple(rules[key] for key in sorted(rules))


def _safe_causal_role(value: object) -> CausalRole:
    if value in {"trigger", "mechanism", "impact", "context"}:
        return cast(CausalRole, value)
    return "context"


def _apply_llm_adjudication_payload(
    *,
    assessments: Sequence[HypothesisAssessment],
    text: str,
    unresolved_hypothesis_ids: set[str],
    public_evidence_ids: set[str],
) -> tuple[list[HypothesisAssessment], int]:
    parsed = json.loads(text)
    if not isinstance(parsed, Mapping):
        raise ValueError("Adjudicator response must be an object.")
    raw_items = parsed.get("assessments")
    if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, Sequence):
        raise ValueError("Adjudicator assessments must be an array.")
    by_id = {item.hypothesis_id: item for item in assessments}
    accepted_count = 0
    seen: set[str] = set()
    for raw_item in cast(Sequence[object], raw_items):
        if not isinstance(raw_item, Mapping):
            continue
        item = cast(Mapping[str, object], raw_item)
        if set(item) != {"hypothesisId", "disposition", "evidenceIds", "reasonCode"}:
            continue
        hypothesis_id = item.get("hypothesisId")
        disposition = item.get("disposition")
        reason_code = item.get("reasonCode")
        raw_evidence = item.get("evidenceIds")
        if (
            not isinstance(hypothesis_id, str)
            or hypothesis_id not in unresolved_hypothesis_ids
            or hypothesis_id in seen
            or disposition
            not in {"supported", "refuted", "causally_inactive", "unresolved"}
            or not isinstance(reason_code, str)
            or isinstance(raw_evidence, (str, bytes))
            or not isinstance(raw_evidence, Sequence)
            or not all(isinstance(value, str) for value in raw_evidence)
        ):
            continue
        evidence_ids = tuple(sorted(set(cast(Sequence[str], raw_evidence))))
        if not set(evidence_ids).issubset(public_evidence_ids):
            continue
        if disposition != "unresolved" and not evidence_ids:
            continue
        previous = by_id[hypothesis_id]
        try:
            transition = HypothesisTransition(
                previous_disposition=previous.disposition,
                next_disposition=cast(Any, disposition),
                evidence_ids=evidence_ids,
                reason_code=reason_code,
                assessment_source="llm_adjudicated",
            )
            by_id[hypothesis_id] = HypothesisAssessment(
                hypothesis_id=hypothesis_id,
                disposition=cast(Any, disposition),
                evidence_ids=evidence_ids,
                reason_code=reason_code,
                assessment_source="llm_adjudicated",
                transitions=previous.transitions + (transition,),
            )
        except ValueError:
            continue
        accepted_count += 1
        seen.add(hypothesis_id)
    return [by_id[key] for key in sorted(by_id)], accepted_count


def _initial_hypothesis_states(public_hypotheses: Sequence[JsonDict]) -> list[JsonDict]:
    return [
        {
            "id": str(item["id"]),
            "status": "open",
            "confidence": 0.5,
            "evidenceIds": [],
        }
        for item in public_hypotheses
        if item.get("id")
    ]


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in cast(Mapping[object, object], value).items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _tool_contracts_payload(
    tool_definitions: Sequence[McpToolDefinition],
) -> list[JsonDict]:
    return [
        {
            "name": item.name,
            "description": item.description,
            "inputSchema": item.input_schema,
            "allowedCausalIntents": sorted(allowed_causal_intents(item.name)),
        }
        for item in tool_definitions
    ]


def _plan_causal_coverage_payload(plan: Sequence[JsonDict]) -> JsonDict:
    coverage = repair_plan_causal_coverage(plan)
    return {
        "planCausalCoverageComplete": coverage.complete,
        "missingCausalRoles": list(coverage.missing_roles),
        "ambiguousTrigger": coverage.ambiguous_trigger,
    }


def _supporting_decision_evidence_ids(
    *,
    hypothesis_states: Sequence[JsonDict],
    observation_decisions: Sequence[JsonDict],
    persisted_evidence_ids: Sequence[str],
) -> list[str]:
    supported = [
        str(item["id"])
        for item in hypothesis_states
        if item.get("status") == "supported" and item.get("id")
    ]
    if len(supported) != 1:
        return []
    supported_id = supported[0]
    persisted = set(persisted_evidence_ids)
    return _unique_strings(
        [
            evidence_id
            for observation in observation_decisions
            if supported_id
            in {
                item
                for item in cast(list[object], observation.get("supports") or [])
                if isinstance(item, str)
            }
            for evidence_id in cast(
                list[object], observation.get("evidenceIds") or []
            )
            if isinstance(evidence_id, str) and evidence_id in persisted
        ]
    )


def _observation_decision_payload(
    decision: ObservationDecision,
    *,
    evidence_id: str,
) -> JsonDict:
    return {
        "purpose": decision.purpose,
        "supports": list(decision.supports),
        "refutes": list(decision.refutes),
        "summary": decision.summary,
        "evidenceIds": [evidence_id],
        "causalRole": decision.causal_role,
        "causalRoleOrigin": decision.causal_role_origin,
        "reportedCausalRole": decision.reported_causal_role,
        "causalRoleCorrected": decision.causal_role_corrected,
    }


def _update_hypothesis_states(
    current: Sequence[JsonDict],
    *,
    decision: ObservationDecision,
    evidence_id: str,
) -> list[JsonDict]:
    updated: list[JsonDict] = []
    for item in current:
        hypothesis_id = str(item.get("id") or "")
        raw_evidence_ids = item.get("evidenceIds")
        existing_evidence_ids = _unique_strings(
            [str(value) for value in raw_evidence_ids]
            if isinstance(raw_evidence_ids, list)
            else []
        )
        status = str(item.get("status") or "open")
        confidence_value = item.get("confidence")
        confidence = (
            float(confidence_value)
            if isinstance(confidence_value, (int, float))
            and not isinstance(confidence_value, bool)
            else 0.5
        )
        if hypothesis_id in decision.supports:
            support_delta = 0.1 if decision.causal_role == "context" else 0.25
            confidence = min(1.0, confidence + support_delta)
            status = "supported" if confidence >= 0.65 else "open"
            existing_evidence_ids = _unique_strings(existing_evidence_ids + [evidence_id])
        elif hypothesis_id in decision.refutes:
            confidence = max(0.0, confidence - 0.4)
            if confidence >= 0.6:
                status = "supported"
            elif confidence <= 0.25:
                status = "refuted"
            else:
                status = "open"
            existing_evidence_ids = _unique_strings(existing_evidence_ids + [evidence_id])
        hypothesis = HypothesisState(
            id=hypothesis_id,
            status=cast(Literal["open", "supported", "refuted"], status),
            confidence=confidence,
            evidence_ids=tuple(existing_evidence_ids),
        )
        updated.append(
            {
                "id": hypothesis.id,
                "status": hypothesis.status,
                "confidence": hypothesis.confidence,
                "evidenceIds": list(hypothesis.evidence_ids),
            }
        )
    return updated


def _root_cause_decision_payload(decision: RootCauseDecision) -> JsonDict:
    return {
        "component": decision.component,
        "mechanism": decision.mechanism,
        "trigger": decision.trigger,
        "causalChain": list(decision.causal_chain),
        "evidenceIds": list(decision.evidence_ids),
        "confidence": decision.confidence,
    }


def _root_cause_decision_from_payload(value: object) -> RootCauseDecision | None:
    payload = _json_dict(value)
    component = payload.get("component")
    mechanism = payload.get("mechanism")
    trigger = payload.get("trigger")
    causal_chain = payload.get("causalChain")
    evidence_ids = payload.get("evidenceIds")
    confidence = payload.get("confidence")
    if (
        not isinstance(component, str)
        or not isinstance(mechanism, str)
        or not isinstance(trigger, str)
        or not isinstance(causal_chain, list)
        or not all(isinstance(item, str) for item in causal_chain)
        or not isinstance(evidence_ids, list)
        or not all(isinstance(item, str) for item in evidence_ids)
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
    ):
        return None
    return RootCauseDecision(
        component=component,
        mechanism=mechanism,
        trigger=trigger,
        causal_chain=tuple(cast(list[str], causal_chain)),
        evidence_ids=tuple(cast(list[str], evidence_ids)),
        confidence=float(confidence),
    )


def _deterministic_decision_gaps(
    decision: RootCauseDecision,
    *,
    decision_vocabulary: Mapping[str, object],
) -> tuple[str, ...]:
    gaps: list[str] = []
    if not decision.trigger.strip():
        gaps.append("trigger")
    if len(decision.causal_chain) < 2 or len(decision.causal_chain) > 6:
        gaps.append("causalChain")
    labels = _json_dict(decision_vocabulary.get("labelsByHypothesis"))
    if labels and not _decision_uses_public_label(decision, labels):
        gaps.extend(["component", "mechanism"])
    return tuple(dict.fromkeys(gaps))


def _decision_uses_public_label(
    decision: RootCauseDecision,
    labels: Mapping[str, object],
) -> bool:
    for value in labels.values():
        candidate = _json_dict(value)
        if (
            candidate.get("component") == decision.component
            and candidate.get("mechanism") == decision.mechanism
        ):
            return True
    return False


def _root_cause_validation_payload(
    decision: RootCauseValidationDecision,
) -> JsonDict:
    return {
        "status": decision.status,
        "evidenceIds": list(decision.evidence_ids),
        "unsupportedFields": list(decision.unsupported_fields),
        "missingEvidence": list(decision.missing_evidence),
        "summary": decision.summary,
    }


def _fallback_recovery_plan(
    decision: RootCauseDecision | None,
    *,
    proposal_tools: set[str],
    force_manual_review: bool = False,
) -> RecoveryPlan:
    if decision is None:
        return RecoveryPlan(
            mode="no_action",
            action="none",
            target="none",
            rationale="No validated root-cause decision is available.",
            tool=None,
            arguments={},
            risk="No action is proposed.",
            rollback="No rollback is required.",
            verification_steps=(),
            evidence_ids=(),
            decision_confidence=0.0,
            human_approval_required=False,
        )
    return RecoveryPlan(
        mode=(
            "manual_review"
            if force_manual_review or proposal_tools
            else "external_policy_required"
        ),
        action="review_validated_diagnosis",
        target=decision.component,
        rationale="Recovery planning model output was unavailable or invalid.",
        tool=None,
        arguments={},
        risk="The appropriate recovery action has not been independently validated.",
        rollback="No action may execute until an external policy approves a rollback plan.",
        verification_steps=(
            "Revalidate the target against current evidence.",
            "Verify service health after an approved external action.",
        ),
        evidence_ids=decision.evidence_ids,
        decision_confidence=decision.confidence,
        human_approval_required=True,
    )


def _deterministic_proposal_fallback(
    decision: RootCauseDecision,
    *,
    proposal_definitions: Sequence[McpToolDefinition],
) -> RecoveryPlan | None:
    """Build a side-effect-free proposal only when one standard contract matches."""
    if len(proposal_definitions) != 1:
        return None
    definition = proposal_definitions[0]
    arguments: JsonDict = {
        "target": decision.component,
        "risk": (
            "The proposed mitigation could shift latency or resource usage and must be "
            "reviewed before any later execution."
        ),
        "rollback": (
            "Discard this proposal if review fails; no infrastructure change has been "
            "applied by this workflow."
        ),
        "verificationSteps": [
            "Re-run the bounded incident probe against the validated target.",
            "Verify target health and the original alert signal after an approved change.",
        ],
        "humanApprovalRequired": True,
    }
    if not plan_matches_tool_contracts(
        [{"tool": definition.name, "arguments": arguments}],
        proposal_definitions,
    ):
        return None
    return RecoveryPlan(
        mode="proposal_only",
        action="record_reviewed_mitigation_proposal",
        target=decision.component,
        rationale=(
            "Record a side-effect-free mitigation proposal for the validated, "
            "evidence-grounded root cause."
        ),
        tool=definition.name,
        arguments=arguments,
        risk=cast(str, arguments["risk"]),
        rollback=cast(str, arguments["rollback"]),
        verification_steps=tuple(
            cast(list[str], arguments["verificationSteps"])
        ),
        evidence_ids=decision.evidence_ids,
        decision_confidence=decision.confidence,
        human_approval_required=True,
    )


def _recovery_plan_payload(plan: RecoveryPlan) -> JsonDict:
    return {
        "mode": plan.mode,
        "action": plan.action,
        "target": plan.target,
        "rationale": plan.rationale,
        "tool": plan.tool,
        "arguments": plan.arguments,
        "risk": plan.risk,
        "rollback": plan.rollback,
        "verificationSteps": list(plan.verification_steps),
        "evidenceIds": list(plan.evidence_ids),
        "decisionConfidence": plan.decision_confidence,
        "humanApprovalRequired": plan.human_approval_required,
    }


def _recovery_plan_from_payload(value: object) -> RecoveryPlan | None:
    payload = _json_dict(value)
    mode = payload.get("mode")
    if mode not in {
        "no_action",
        "proposal_only",
        "external_policy_required",
        "manual_review",
    }:
        return None
    tool = payload.get("tool")
    arguments = payload.get("arguments")
    verification = payload.get("verificationSteps")
    evidence_ids = payload.get("evidenceIds")
    confidence = payload.get("decisionConfidence")
    approval = payload.get("humanApprovalRequired")
    text_fields = [
        payload.get("action"),
        payload.get("target"),
        payload.get("rationale"),
        payload.get("risk"),
        payload.get("rollback"),
    ]
    if (
        not all(isinstance(item, str) and item for item in text_fields)
        or (tool is not None and not isinstance(tool, str))
        or not isinstance(arguments, Mapping)
        or not isinstance(verification, list)
        or not all(isinstance(item, str) for item in verification)
        or not isinstance(evidence_ids, list)
        or not all(isinstance(item, str) for item in evidence_ids)
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not isinstance(approval, bool)
    ):
        return None
    return RecoveryPlan(
        mode=cast(
            Literal[
                "no_action", "proposal_only", "external_policy_required", "manual_review"
            ],
            mode,
        ),
        action=cast(str, text_fields[0]),
        target=cast(str, text_fields[1]),
        rationale=cast(str, text_fields[2]),
        tool=cast(str | None, tool),
        arguments=dict(cast(Mapping[str, object], arguments)),
        risk=cast(str, text_fields[3]),
        rollback=cast(str, text_fields[4]),
        verification_steps=tuple(cast(list[str], verification)),
        evidence_ids=tuple(cast(list[str], evidence_ids)),
        decision_confidence=float(confidence),
        human_approval_required=approval,
    )


def _recovery_policy_payload(decision: RecoveryPolicyDecision) -> JsonDict:
    return {
        "status": decision.status,
        "authorizationCode": decision.authorization_code,
        "executionPermitted": decision.execution_permitted,
        "proposalRecorded": decision.proposal_recorded,
        "humanApprovalRequired": decision.human_approval_required,
        "summary": decision.summary,
    }


def _model_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
            for item in content
        )
    return str(content)


def _sop_hit_payload(hit: KnowledgeRetrievalHit) -> JsonDict:
    return {
        "chunkId": hit.chunk_id,
        "documentId": hit.document_id,
        "knowledgeBaseId": hit.knowledge_base_id,
        "content": hit.content,
        "source": hit.source,
        "metadata": _json_dict(hit.metadata),
        "score": hit.score,
        "vectorRank": hit.vector_rank,
        "bm25Rank": hit.bm25_rank,
        "rerankRank": hit.rerank_rank,
        "vectorScore": hit.vector_score,
        "bm25Score": hit.bm25_score,
        "rrfScore": hit.rrf_score,
        "rerankScore": hit.rerank_score,
    }


def _citation_payload(citation: KnowledgeRetrievalCitationSource) -> JsonDict:
    return {
        "id": citation.id,
        "title": citation.title,
        "sourceType": citation.source_type,
        "chunkId": citation.chunk_id,
        "documentId": citation.document_id,
        "knowledgeBaseId": citation.knowledge_base_id,
        "source": citation.source,
        "metadata": _json_dict(citation.metadata),
        "score": citation.score,
        "vectorRank": citation.vector_rank,
        "bm25Rank": citation.bm25_rank,
        "rerankRank": citation.rerank_rank,
        "vectorScore": citation.vector_score,
        "bm25Score": citation.bm25_score,
        "rrfScore": citation.rrf_score,
        "rerankScore": citation.rerank_score,
    }


def _reference_event_from_payload(citation: object) -> dict[str, object]:
    return _sse_event("reference.source", {"reference": _json_dict(citation)})


def _tool_event(
    audit_id: str,
    name: str,
    status: Literal["started", "completed", "failed"],
    payload: object,
) -> dict[str, object]:
    key = "input" if status == "started" else "output"
    return _sse_event(
        "tool.call",
        {"toolCall": {"id": audit_id, "name": name, "status": status, key: _safe_value(payload)}},
    )


def _task_status_event(
    task_id: str,
    status: Literal["running", "succeeded", "failed"],
    message: str,
    progress: int,
) -> dict[str, object]:
    return _sse_event(
        "task.status",
        {"task": {"id": task_id, "status": status, "message": message, "progress": progress}},
    )


def _error_event(code: str) -> dict[str, object]:
    category, http_status, message = ERROR_DEFINITIONS[code]
    return _sse_event(
        "error",
        {
            "error": {
                "code": code,
                "category": category,
                "httpStatus": http_status,
                "message": message,
            }
        },
    )


def _sse_event(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": f"evt_{uuid4().hex}",
        "type": event_type,
        "channel": "aiops",
        "timestamp": _now().isoformat(),
        **payload,
    }


def _task_payload(record: DiagnosticTaskRecord) -> JsonDict:
    return {
        "id": record.id,
        "ownerUserId": record.owner_user_id,
        "status": record.status,
        "query": record.query,
        "inputPayload": record.input_payload,
        "resultPayload": record.result_payload,
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
        "completedAt": record.completed_at.isoformat() if record.completed_at is not None else None,
    }


def _report_payload(report: DiagnosticReportRecord) -> JsonDict:
    return {
        "id": report.id,
        "title": report.title,
        "content": report.content,
        "payload": _json_dict(report.payload),
        "createdAt": report.created_at.isoformat(),
    }


def _report_prompt(state: AiopsDiagnosticState) -> str:
    report_context = {
        "diagnosticQuery": str(state.get("query") or ""),
        "alert": _json_dict(state.get("alert")),
        "noSopMatched": bool(state.get("no_sop_matched")),
        "sopEvidence": _report_sop_context(state.get("sop_hits")),
        "plan": _json_list(state.get("plan")),
        "executionFailed": bool(state.get("execution_failed")),
        "executionEvidence": _report_evidence_context(state.get("evidence")),
        "hypothesisStates": state.get("hypothesis_states") or [],
        "observationDecisions": state.get("observation_decisions") or [],
        "rootCauseDecision": state.get("root_cause_decision"),
    }
    context_json = json.dumps(
        _safe_value(report_context),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )[:12_000]
    return f"""你是一个严谨的 AIOps 诊断报告生成器。请只根据下方“诊断事实”生成最终报告。

输出要求：
- 最终输出必须是纯 Markdown 文本，不要输出 JSON，不要使用包裹全文的 Markdown 代码围栏。
- 所有告警、时间、服务、症状、日志、根因、排查步骤和建议必须来自诊断事实；严禁编造。
- 缺失字段写“未获取”，证据不能支持根因时写“证据不足，无法确认根因”。
- 工具调用失败必须在“日志证据”“已执行的排查步骤”和“结论”中如实说明，不得跳过。
- 没有匹配 SOP 时，必须在处理建议和结论中说明使用了通用诊断计划。
- 有多个告警时，按相同结构依次生成“告警根因分析2”“处理方案执行2”等章节。
- 必须严格保留下面的一级、二级和三级标题结构。

# 告警分析报告

---

## 📋 活跃告警清单

| 告警名称 | 级别 | 目标服务 | 首次触发时间 | 最新触发时间 | 状态 |
|---------|------|----------|-------------|-------------|------|
| [名称] | [级别] | [服务] | [首次时间] | [最新时间] | [状态] |

---

## 🔍 告警根因分析1 - [告警名称]

### 告警详情
- **告警级别**: [级别]
- **受影响服务**: [服务名]
- **持续时间**: [有证据时填写，否则未获取]

### 症状描述
[仅描述证据中存在的症状]

### 日志证据
[引用真实工具返回的关键日志，失败或无结果时明确说明]

### 根因结论
[基于证据得出；证据不足时明确无法确认]

---

## 🛠️ 处理方案执行1 - [告警名称]

### 已执行的排查步骤
1. [仅列出真实执行过的步骤]

### 处理建议
[基于 SOP 或证据给出；没有 SOP 时标注通用建议]

### 预期效果
[说明建议实施后的预期效果，不得写成已执行结果]

---

## 📊 结论

### 整体评估
[总结已验证事实和无法验证的部分]

### 关键发现
- [证据支持的发现]

### 后续建议
1. [可执行建议]

### 风险评估
[仅根据告警级别、持续时间和影响证据评估；信息不足时明确说明]

诊断事实：
{context_json}
"""


def _clean_markdown_report(content: str) -> str | None:
    report = content.strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", report, flags=re.DOTALL)
    if fenced is not None:
        report = fenced.group(1).strip()
    if report.startswith(("{", "[")):
        return None
    if not all(heading in report for heading in AIOPS_REPORT_REQUIRED_HEADINGS):
        return None
    return report


def _fallback_report_content(
    *,
    alert: JsonDict,
    no_sop_matched: bool,
    sop_hits: Sequence[JsonDict],
    evidence: Sequence[JsonDict],
    execution_failed: bool,
) -> str:
    alert_name = _report_value(alert, "alertName", "name")
    severity = _report_value(alert, "severity", "level")
    service = _report_value(alert, "service", "target")
    starts_at = _report_value(alert, "startsAt", "startTime")
    latest_at = _report_value(alert, "updatedAt", "endsAt", "latestTime")
    status = _report_value(alert, "status")
    evidence_lines = _fallback_evidence_lines(evidence)
    sop_line = (
        "未检索到匹配的 SOP，本报告采用通用诊断计划。"
        if no_sop_matched
        else "已检索到 SOP：" + "、".join(str(hit.get("source", "未获取")) for hit in sop_hits)
    )
    execution_line = (
        "诊断工具执行失败，部分结论无法验证。" if execution_failed else "诊断流程已执行完成。"
    )
    first_suggestion = (
        "1. 检查失败的工具调用并恢复数据源连接。"
        if execution_failed
        else "1. 继续补充相关服务的指标和日志证据。"
    )
    lines = [
        "# 告警分析报告",
        "",
        "---",
        "",
        "## 📋 活跃告警清单",
        "",
        "| 告警名称 | 级别 | 目标服务 | 首次触发时间 | 最新触发时间 | 状态 |",
        "|---------|------|----------|-------------|-------------|------|",
        f"| {alert_name} | {severity} | {service} | {starts_at} | {latest_at} | {status} |",
        "",
        "---",
        "",
        f"## 🔍 告警根因分析1 - {alert_name}",
        "",
        "### 告警详情",
        f"- **告警级别**: {severity}",
        f"- **受影响服务**: {service}",
        "- **持续时间**: 未获取",
        "",
        "### 症状描述",
        "当前仅能确认诊断输入和下列工具执行结果，未获取可独立验证的完整症状指标。",
        "",
        "### 日志证据",
        *evidence_lines,
        "",
        "### 根因结论",
        "证据不足，无法确认根因。",
        "",
        "---",
        "",
        f"## 🛠️ 处理方案执行1 - {alert_name}",
        "",
        "### 已执行的排查步骤",
        *[f"{index}. {line.removeprefix('- ')}" for index, line in enumerate(evidence_lines, 1)],
        "",
        "### 处理建议",
        sop_line,
        "在变更系统状态前，请补充可验证的日志和指标证据，并由值班人员确认处置动作。",
        "",
        "### 预期效果",
        "补充证据后可缩小故障范围，并验证后续处置是否降低告警影响；当前尚未执行处置动作。",
        "",
        "---",
        "",
        "## 📊 结论",
        "",
        "### 整体评估",
        execution_line,
        "当前报告未获得足以确认根因的完整证据，不作未经验证的根因判断。",
        "",
        "### 关键发现",
        f"- {sop_line}",
        f"- {execution_line}",
        "",
        "### 后续建议",
        first_suggestion,
        "2. 将新增证据与告警触发时间对齐后重新执行诊断。",
        "",
        "### 风险评估",
        f"告警级别为 {severity}；由于影响范围和持续时间信息不完整，当前风险等级无法进一步确认。",
    ]
    return "\n".join(lines)


def _report_value(payload: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("|", "\\|")
    return "未获取"


def _fallback_evidence_lines(evidence: Sequence[JsonDict]) -> list[str]:
    if not evidence:
        return ["- 未获取工具证据，无法验证日志症状。"]
    lines: list[str] = []
    for item in evidence:
        tool = str(item.get("tool") or "未知工具")
        status = str(item.get("status") or "unknown")
        details = _evidence_detail_lines(item)
        lines.append(f"- {tool} [{status}]：{details[0].removeprefix('  - ')}")
    return lines


def _evidence_detail_lines(evidence: JsonDict) -> list[str]:
    summary = evidence.get("summary")
    if not isinstance(summary, str):
        return ["  - No serializable evidence summary was returned."]
    try:
        tool_content = json.loads(summary)
        if isinstance(tool_content, Mapping) and isinstance(tool_content.get("records"), list):
            return _search_log_detail_lines(cast(list[object], tool_content["records"]))
        first_item = tool_content[0] if isinstance(tool_content, list) and tool_content else None
        raw_logs = first_item.get("text") if isinstance(first_item, Mapping) else None
        logs = json.loads(raw_logs) if isinstance(raw_logs, str) else None
    except (IndexError, TypeError, json.JSONDecodeError):
        return [f"  - {summary}"]
    if not isinstance(logs, list):
        return [f"  - {summary}"]
    if not logs:
        return ["  - The real tool returned no matching records."]
    lines: list[str] = []
    for log in logs[:10]:
        if not isinstance(log, Mapping):
            continue
        raw_log_json = log.get("LogJson")
        try:
            log_payload = json.loads(raw_log_json) if isinstance(raw_log_json, str) else {}
        except json.JSONDecodeError:
            log_payload = {}
        if not isinstance(log_payload, Mapping):
            continue
        scenario = log_payload.get("scenario", "unknown")
        service = log_payload.get("service", "unknown")
        message = log_payload.get("message", "")
        lines.append(f"  - scenario={scenario}; service={service}; message={message}")
    return lines or [f"  - {summary}"]


def _tool_result_summary(tool_name: str, output: object) -> str:
    if tool_name != "SearchLog":
        return _bounded_json(output)
    records = _search_log_records(output)
    if not records:
        return json.dumps(
            {"recordCount": 0, "records": [], "message": "CLS 未返回可解析日志。"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return json.dumps(
        {"recordCount": len(records), "records": records[:10]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _search_log_records(output: object) -> list[JsonDict]:
    if isinstance(output, Mapping):
        raw_records = output.get("records")
        if not isinstance(raw_records, Sequence) or isinstance(raw_records, str | bytes):
            return []
        return [
            _bounded_search_log_record(record)
            for record in raw_records
            if isinstance(record, Mapping)
        ]
    if not isinstance(output, Sequence) or isinstance(output, str | bytes):
        return []
    records: list[JsonDict] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            logs = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(logs, list):
            continue
        for log in logs:
            if not isinstance(log, Mapping):
                continue
            raw_payload = log.get("LogJson")
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, Mapping):
                continue
            records.append(_bounded_search_log_record(payload))
    return records


def _bounded_search_log_record(payload: Mapping[object, object]) -> JsonDict:
    return {
        key: _safe_value(payload[key])
        for key in (
            "timestamp",
            "level",
            "service",
            "host",
            "event",
            "message",
            "latency_ms",
            "exception",
            "request_id",
            "run_id",
            "scenario_id",
            "incident_id",
            "trace_id",
            "component",
        )
        if key in payload
    }


def _search_log_detail_lines(records: Sequence[object]) -> list[str]:
    lines: list[str] = []
    for record in records[:10]:
        if not isinstance(record, Mapping):
            continue
        fields = [
            f"{key}={record[key]}"
            for key in ("timestamp", "level", "service", "event", "message", "latency_ms")
            if key in record
        ]
        if fields:
            lines.append("  - " + "; ".join(fields))
    return lines or ["  - CLS 未返回可解析日志。"]


def _report_sop_context(value: object) -> list[JsonDict]:
    return [
        {
            "source": str(hit.get("source") or "未获取"),
            "score": hit.get("score"),
            "content": str(hit.get("content") or "")[:1_200],
        }
        for hit in _json_list(value)[:3]
    ]


def _report_evidence_context(value: object) -> list[JsonDict]:
    return [
        {
            "tool": str(item.get("tool") or "未知工具"),
            "status": str(item.get("status") or "unknown"),
            "summary": str(item.get("summary") or "")[:4_000],
        }
        for item in _json_list(value)
    ]


def _safe_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _bounded_json(value: object, limit: int = 4_000) -> str:
    encoded = json.dumps(_safe_value(value), ensure_ascii=True, separators=(",", ":"), default=str)
    return encoded[:limit]


def _safe_error(exc: Exception) -> str:
    message = re.sub(r"(?:sk-[A-Za-z0-9_-]+|AKID[A-Za-z0-9]+)", "[redacted]", str(exc))
    return message[:500] or "Tool invocation failed."


def _evidence_kind_for_tool(tool_name: str) -> str:
    if tool_name == "knowledge_retrieval":
        return "knowledge_reference"
    return "log"


def _unique_strings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _json_dict(value: object) -> JsonDict:
    safe_value = _safe_value(value)
    return cast(JsonDict, safe_value) if isinstance(safe_value, dict) else {}


def _json_list(value: object) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    return [_json_dict(item) for item in value]


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)
