"""Evidence-based LangGraph workflow for AIOps diagnostics."""
# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportTypedDictNotRequiredAccess=false, reportUnnecessaryCast=false

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from operator import add
from time import monotonic
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypedDict, cast
from uuid import uuid4

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for
from langgraph.graph import END, START, StateGraph

from super_ai.aiops.cases import DiagnosisCasePersistor
from super_ai.aiops.causal_intents import (
    allowed_causal_intents,
    next_causal_refinement_index,
    repair_plan_causal_coverage,
    supported_causal_coverage,
)
from super_ai.aiops.decision_validation import (
    can_replan_deterministic_gap,
    deterministic_checks_payload,
    invoke_structured_root_cause_decision,
    invoke_structured_root_cause_validation,
    validate_grounded_candidate,
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
)
from super_ai.error_catalog import ERROR_DEFINITIONS
from super_ai.llm import LlmProvider
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
    KnowledgeRetrievalToolResult,
    KnowledgeRetrievalToolRunner,
)


class AiopsDiagnosticState(TypedDict, total=False):
    owner_user_id: str
    task_id: str
    query: str
    alert: JsonDict
    accessible_knowledge_base_ids: tuple[str, ...]
    sop_hits: list[JsonDict]
    no_sop_matched: bool
    plan: list[JsonDict]
    plan_origin: str
    plan_index: int
    tool_definitions: tuple[McpToolDefinition, ...]
    public_hypotheses: list[JsonDict]
    decision_vocabulary: JsonDict
    hypothesis_states: list[JsonDict]
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
        "executor", "replanner", "decision", "report", "recovery_planner"
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

AIOPS_REPORT_TITLE = "告警分析报告"
AIOPS_REPORT_REQUIRED_HEADINGS = (
    "# 告警分析报告",
    "## 📋 活跃告警清单",
    "## 📊 结论",
)


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
        if isinstance(hypothesis_id, str) and len(evidence_ids) >= 2:
            candidates.append((hypothesis_id, tuple(evidence_ids), float(confidence)))
    if len(candidates) != 1:
        return None

    hypothesis_id, evidence_ids, confidence = candidates[0]
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


def _repair_grounded_causal_chain(
    decision: RootCauseDecision,
    *,
    public_hypotheses: Sequence[JsonDict],
    hypothesis_states: Sequence[JsonDict],
    observation_decisions: Sequence[JsonDict],
    decision_vocabulary: JsonDict,
) -> RootCauseDecision | None:
    gaps = _deterministic_decision_gaps(
        decision,
        decision_vocabulary=decision_vocabulary,
    )
    if set(gaps) != {"causalChain"}:
        return None
    fallback = build_grounded_fallback_decision(
        public_hypotheses=public_hypotheses,
        hypothesis_states=hypothesis_states,
        observation_decisions=observation_decisions,
        decision_vocabulary=decision_vocabulary,
    )
    if (
        fallback is None
        or fallback.component != decision.component
        or fallback.mechanism != decision.mechanism
        or not set(fallback.evidence_ids).issubset(set(decision.evidence_ids))
        or not 2 <= len(fallback.causal_chain) <= 6
    ):
        return None
    repaired = RootCauseDecision(
        component=decision.component,
        mechanism=decision.mechanism,
        trigger=fallback.trigger,
        causal_chain=fallback.causal_chain,
        evidence_ids=decision.evidence_ids,
        confidence=decision.confidence,
    )
    if _deterministic_decision_gaps(
        repaired,
        decision_vocabulary=decision_vocabulary,
    ):
        return None
    return repaired


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
        alert = _json_dict(task.input_payload.get("alert"))
        initial_evidence_ids: list[str] = []
        if alert:
            alert_evidence = await self._repositories.diagnostics.create_evidence(
                owner_user_id=task.owner_user_id,
                evidence_id=f"evidence_{uuid4().hex}",
                task_id=task.id,
                kind="alert",
                source="diagnostic-input",
                summary="Original alert input for the diagnostic.",
                payload=alert,
            )
            initial_evidence_ids.append(alert_evidence.id)
        graph = self._build_graph()
        initial_state: AiopsDiagnosticState = {
            "owner_user_id": task.owner_user_id,
            "task_id": task.id,
            "query": task.query,
            "alert": alert,
            "public_hypotheses": _json_list(task.input_payload.get("hypotheses")),
            "decision_vocabulary": _json_dict(
                task.input_payload.get("decisionVocabulary")
            ),
            "hypothesis_states": _initial_hypothesis_states(
                _json_list(task.input_payload.get("hypotheses"))
            ),
            "observation_decisions": [],
            "accessible_knowledge_base_ids": tuple(accessible_knowledge_base_ids),
            "plan_index": 0,
            "replan_count": 0,
            "max_replans": 2,
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
            async for update in graph.astream(initial_state, stream_mode="updates"):
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

    def _build_graph(self) -> Any:
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

    async def _planner(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        query = str(state["query"])
        events = [_task_status_event(task_id, "running", "Planner: retrieving SOP evidence.", 15)]
        retrieval_audit_id = f"tool_{uuid4().hex}"
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

        retrieval_result: KnowledgeRetrievalToolResult | None = None
        retrieval_error: str | None = None
        try:
            retrieval_result = await self._retrieval_tool.run(
                KnowledgeRetrievalToolInput(query=query, top_k=3),
                owner_user_id=owner_user_id,
                accessible_knowledge_base_ids=cast(
                    Sequence[str], state["accessible_knowledge_base_ids"]
                ),
            )
        except KnowledgeRetrievalError as exc:
            retrieval_error = exc.message

        sop_hits: list[JsonDict] = []
        if retrieval_result is not None:
            sop_hits = [_sop_hit_payload(hit) for hit in retrieval_result.results]
            retrieval_payload = {
                "query": retrieval_result.query,
                "results": sop_hits,
                "citations": [
                    _citation_payload(citation) for citation in retrieval_result.citations
                ],
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
            events.extend(_reference_event(citation) for citation in retrieval_result.citations)
        else:
            safe_error = retrieval_error or "Knowledge retrieval was unavailable."
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

        no_sop_matched = not sop_hits
        if no_sop_matched:
            events.append(
                _task_status_event(
                    task_id,
                    "running",
                    "Planner: no SOP matched; using a generic evidence-gathering plan.",
                    25,
                )
            )

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

        plan, plan_origin = await self._create_plan(
            query=query,
            alert=_json_dict(state.get("alert")),
            sop_hits=sop_hits,
            no_sop_matched=no_sop_matched,
            tool_definitions=[
                item for item in discovered_tools if item.name not in self._tool_policies
            ],
            known_hypotheses=[
                str(item.get("id"))
                for item in cast(list[JsonDict], state.get("public_hypotheses") or [])
                if item.get("id")
            ],
        )
        events.append(
            _task_status_event(
                task_id,
                "running",
                f"Planner: created a {plan_origin} plan with {len(plan)} step(s).",
                35,
            )
        )
        planner_payload: JsonDict = {
            "workflowVersion": "evidence-driven-v3",
            "noSopMatched": no_sop_matched,
            "sopHits": sop_hits,
            "plan": plan,
            "planOrigin": plan_origin,
            "retrievalError": retrieval_error,
            **_plan_causal_coverage_payload(plan),
        }
        planner_step = await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="planner",
            status="completed",
            payload=planner_payload,
        )
        persisted_evidence_ids: list[str] = []
        if retrieval_result is not None:
            for citation in retrieval_result.citations:
                citation_payload = _citation_payload(citation)
                evidence_record = await self._repositories.diagnostics.create_evidence(
                    owner_user_id=owner_user_id,
                    evidence_id=f"evidence_{uuid4().hex}",
                    task_id=task_id,
                    step_id=planner_step.id,
                    kind="knowledge_reference",
                    source=str(citation_payload["source"]),
                    summary=str(citation_payload["title"]),
                    payload=citation_payload,
                )
                persisted_evidence_ids.append(evidence_record.id)
        elif retrieval_error is not None:
            evidence_record = await self._repositories.diagnostics.create_evidence(
                owner_user_id=owner_user_id,
                evidence_id=f"evidence_{uuid4().hex}",
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
        return {
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

    async def _executor(self, state: AiopsDiagnosticState) -> dict[str, object]:
        task_id = str(state["task_id"])
        owner_user_id = str(state["owner_user_id"])
        plan = cast(list[JsonDict], state.get("plan") or [])
        plan_index = int(state.get("plan_index") or 0)
        attempt_count = int(state.get("executor_attempt_count") or 0)
        max_total_steps = int(state.get("max_total_steps") or 6)
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
        audit_id = f"tool_{uuid4().hex}"
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

        try:
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
                output: object = {
                    "results": [_sop_hit_payload(hit) for hit in result.results],
                    "citations": [_citation_payload(citation) for citation in result.citations],
                }
            elif tool_name:
                mcp_client = await self._mcp_client_for(owner_user_id)
                output = await mcp_client.call_tool(tool_name, arguments)
            else:
                raise ValueError("Diagnostic plan did not specify a tool.")
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
                evidence_id=f"evidence_{uuid4().hex}",
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
                "current_plan_step": step,
                "events": events,
            }

        summary = _tool_result_summary(tool_name, output)
        evidence: JsonDict = {
            "stepId": str(step.get("id") or f"step_{plan_index + 1}"),
            "tool": tool_name,
            "status": "completed",
            "summary": summary,
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
            evidence_id=f"evidence_{uuid4().hex}",
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
        return {
            "plan_index": plan_index + 1,
            "executor_attempt_count": attempt_count + 1,
            "executed_step_fingerprints": [fingerprint],
            "evidence": [evidence],
            "evidence_ids": [evidence_record.id],
            "current_evidence_id": evidence_record.id,
            "current_evidence_summary": summary,
            "current_plan_step": step,
            "events": events,
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
            "reasoning. Use only known hypothesis IDs. Do not include hidden reasoning. "
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
            tested_hypotheses = {
                item
                for item in cast(
                    list[object], plan_step.get("testsHypotheses") or []
                )
                if isinstance(item, str)
            }
            reported_role = decision.causal_role
            decision = replace(
                decision,
                supports=tuple(
                    item for item in decision.supports if item in tested_hypotheses
                ),
                refutes=tuple(
                    item for item in decision.refutes if item in tested_hypotheses
                ),
                causal_role=plan_intent,
                causal_role_origin=(
                    "model" if reported_role == plan_intent else "plan_contract"
                ),
                reported_causal_role=reported_role,
                causal_role_corrected=reported_role != plan_intent,
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
        if decision.status == "sufficient":
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
        try:
            response = await self._llm_provider.create_chat_model().ainvoke(prompt)
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
            if missing_roles and step.get("causalIntent") not in missing_roles:
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
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="replanner",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "replanner", payload)
        return {
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
            repaired = _repair_grounded_causal_chain(
                decision,
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
            if repaired is not None:
                decision = repaired
                decision_origin = "llm_grounded_causal_chain_repair"
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
        validation_error_phase: str | None = None
        validation_retryable: bool | None = None
        validation_http_status_class: str | None = None
        validation_attempts = 0
        validation_warning: str | None = None
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
            prompt = (
                "Return one JSON root-cause validation decision with status, evidenceIds, "
                "unsupportedFields, missingEvidence, and summary. Judge only whether the "
                "candidate component, mechanism, trigger, and causalChain are supported by the "
                "public structured observations. Do not compare against hidden answers and do "
                "not include private chain-of-thought. "
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
                model=self._llm_provider.create_chat_model(),
                prompt=prompt,
                available_evidence_ids=set(evidence_ids),
            )
            validation_attempts = outcome.attempts
            validation_error_category = outcome.error_category
            validation_error_code = outcome.error_code
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
                "validationErrorPhase": validation_error_phase,
                "validationRetryable": validation_retryable,
                "validationHttpStatusClass": validation_http_status_class,
                "validationAttempts": validation_attempts,
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
                "validationErrorPhase": validation_error_phase,
                "validationRetryable": validation_retryable,
                "validationHttpStatusClass": validation_http_status_class,
                "validationAttempts": validation_attempts,
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
            "validationErrorPhase": validation_error_phase,
            "validationRetryable": validation_retryable,
            "validationHttpStatusClass": validation_http_status_class,
            "validationAttempts": validation_attempts,
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
                response = await self._llm_provider.create_chat_model().ainvoke(prompt)
                plan = parse_recovery_plan(
                    _model_text(response),
                    available_evidence_ids=set(evidence_ids),
                    proposal_tools=proposal_tools,
                )
            except Exception:
                plan = _fallback_recovery_plan(candidate, proposal_tools=proposal_tools)
        payload = _recovery_plan_payload(plan)
        await self._create_step(
            owner_user_id=owner_user_id,
            task_id=task_id,
            phase="recovery_planning",
            status="completed",
            payload=payload,
        )
        await self._save_checkpoint(state, "recovery_planning", payload)
        return {
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
        audit_id = f"tool_{uuid4().hex}"
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
        report_content, report_generation = await self._generate_report_content(state)
        report_payload: JsonDict = {
            "workflowVersion": "evidence-driven-v3",
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
            report_id=f"report_{uuid4().hex}",
            task_id=task_id,
            title=AIOPS_REPORT_TITLE,
            content=report_content,
            payload=report_payload,
        )
        for evidence_id in evidence_ids:
            await self._repositories.diagnostics.link_report_evidence(
                owner_user_id=owner_user_id,
                link_id=f"report_evidence_{uuid4().hex}",
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
        return {"report_id": report.id, "events": events}

    async def _generate_report_content(self, state: AiopsDiagnosticState) -> tuple[str, str]:
        fallback = _fallback_report_content(
            alert=_json_dict(state.get("alert")),
            no_sop_matched=bool(state.get("no_sop_matched")),
            sop_hits=cast(list[JsonDict], state.get("sop_hits") or []),
            evidence=cast(list[JsonDict], state.get("evidence") or []),
            execution_failed=bool(state.get("execution_failed")),
        )
        prompt = _report_prompt(state)
        try:
            response = await self._llm_provider.create_chat_model().ainvoke(prompt)
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

    async def _create_plan(
        self,
        *,
        query: str,
        alert: JsonDict,
        sop_hits: Sequence[JsonDict],
        no_sop_matched: bool,
        tool_definitions: Sequence[McpToolDefinition],
        known_hypotheses: Sequence[str],
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
            "`causalIntent`. causalIntent must be allowed by the selected tool contract. "
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
            response = await self._llm_provider.create_chat_model().ainvoke(prompt)
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
        existing_steps = await self._repositories.diagnostics.list_steps(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        return await self._repositories.diagnostics.create_step(
            owner_user_id=owner_user_id,
            step_id=f"diagnostic_step_{uuid4().hex}",
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
        await self._repositories.diagnostics.save_checkpoint(
            owner_user_id=str(state["owner_user_id"]),
            checkpoint_record_id=f"checkpoint_{uuid4().hex}",
            task_id=task_id,
            thread_id=f"aiops:{task_id}",
            checkpoint_ns=node,
            checkpoint_id=f"{node}_{uuid4().hex}",
            checkpoint_payload=payload,
            metadata={"node": node},
        )


def _diagnostic_plan_step_payload(step: DiagnosticPlanStep) -> JsonDict:
    return {
        "id": step.id,
        "tool": step.tool,
        "arguments": step.arguments,
        "purpose": step.purpose,
        "testsHypotheses": list(step.tests_hypotheses),
        "causalIntent": step.causal_intent,
        "causalIntentOrigin": step.causal_intent_origin,
    }


def _step_fingerprint(step: Mapping[str, object]) -> str:
    return tool_step_fingerprint(
        str(step.get("tool") or ""),
        _json_dict(step.get("arguments")),
    )


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
    hypothesis_states: Sequence[JsonDict],
    evidence_ids: Sequence[str],
) -> EvidenceSufficiencyDecision:
    supported = tuple(
        str(item.get("id"))
        for item in hypothesis_states
        if item.get("id") and item.get("status") == "supported"
    )
    refuted = tuple(
        str(item.get("id"))
        for item in hypothesis_states
        if item.get("id") and item.get("status") == "refuted"
    )
    unresolved = tuple(
        str(item.get("id"))
        for item in hypothesis_states
        if item.get("id") and item.get("status") not in {"supported", "refuted"}
    )
    return EvidenceSufficiencyDecision(
        status="insufficient",
        evidence_ids=tuple(_unique_strings(list(evidence_ids))[:6]),
        supported_hypotheses=supported[:6],
        refuted_hypotheses=refuted[:6],
        unresolved_hypotheses=unresolved[:6],
        missing_evidence=("Structured sufficiency assessment was unavailable.",),
        recommended_tools=(),
        summary="Evidence sufficiency could not be confirmed.",
    )


def _evidence_sufficiency_payload(
    decision: EvidenceSufficiencyDecision,
) -> JsonDict:
    return {
        "status": decision.status,
        "evidenceIds": list(decision.evidence_ids),
        "supportedHypotheses": list(decision.supported_hypotheses),
        "refutedHypotheses": list(decision.refuted_hypotheses),
        "unresolvedHypotheses": list(decision.unresolved_hypotheses),
        "missingEvidence": list(decision.missing_evidence),
        "recommendedTools": list(decision.recommended_tools),
        "summary": decision.summary,
    }


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
            status = "supported"
            confidence = min(1.0, confidence + 0.25)
            existing_evidence_ids = _unique_strings(existing_evidence_ids + [evidence_id])
        elif hypothesis_id in decision.refutes:
            status = "refuted"
            confidence = max(0.0, confidence - 0.4)
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


def _reference_event(citation: KnowledgeRetrievalCitationSource) -> dict[str, object]:
    return _sse_event("reference.source", {"reference": _citation_payload(citation)})


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
