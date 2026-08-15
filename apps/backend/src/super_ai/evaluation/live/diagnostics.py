"""Production diagnostic adapter for answer-isolated Docker Live evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import uuid4

from langchain_core.tools import StructuredTool

from super_ai.aiops import AiopsDiagnosticService
from super_ai.evaluation.artifacts import (
    LiveEvidenceAudit,
    LiveRecoveryAudit,
    RunArtifact,
    build_run_artifact,
)
from super_ai.evaluation.live.cls_evidence import build_cls_search_arguments
from super_ai.evaluation.live.domain import (
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveScenario,
    LiveVerification,
)
from super_ai.evaluation.live.evidence_client import (
    LiveCompositeEvidenceMcpClient,
    LiveMcpClient,
)
from super_ai.llm import LlmProvider
from super_ai.mcp_client import McpClientError, McpToolDefinition
from super_ai.memory.repositories import JsonDict, MemoryRepositories
from super_ai.retrieval import KnowledgeRetrievalToolRunner


def build_live_diagnostic_input(scenario: LiveScenario) -> JsonDict:
    """Build Agent input solely from the public scenario contract."""
    return {
        "query": scenario.title,
        "alert": dict(scenario.alert),
        "hypotheses": [
            {"id": item.id, "description": item.description} for item in scenario.hypotheses
        ],
        "benchmarkScenarioId": scenario.id,
        "benchmarkMode": "live",
        "decisionVocabulary": _decision_vocabulary(scenario),
    }


def proposal_tool_policies_for_scenario(
    scenario: LiveScenario,
) -> Mapping[str, Literal["proposal_only"]] | None:
    """Expose the Nginx proposal recorder only for its matching Live scenario."""
    if scenario.driver != "nginx_timeout":
        return None
    return {"ProposeNginxTimeoutMitigation": "proposal_only"}


def _decision_vocabulary(scenario: LiveScenario) -> JsonDict:
    labels_by_driver: dict[str, dict[str, tuple[str, str]]] = {
        "postgres_lock_wait": {
            "postgres_lock_blocking": ("postgresql", "row_lock_blocking"),
            "postgres_slow_query_without_lock": (
                "postgresql",
                "slow_query_without_lock",
            ),
            "postgres_connectivity_failure": ("postgresql", "connectivity_failure"),
        },
        "postgres_deadlock": {
            "postgres_deadlock": (
                "postgresql",
                "opposite_order_transaction_deadlock",
            ),
            "postgres_long_lock_wait": ("postgresql", "row_lock_blocking"),
            "postgres_slow_statement": ("postgresql", "slow_query_without_lock"),
        },
        "redis_maxclients": {
            "redis_maxclients": (
                "live-eval-redis",
                "benchmark_clients_exhausted_maxclients",
            ),
            "redis_process_unavailable": (
                "live-eval-redis",
                "connectivity_failure",
            ),
            "host_file_descriptor_exhaustion": (
                "host",
                "file_descriptor_exhaustion",
            ),
            "redis_stale_client_pool": ("application", "stale_client_pool"),
        },
        "nginx_timeout": {
            "nginx_upstream_response_timeout": (
                "live-eval-upstream",
                "upstream_response_exceeded_proxy_read_timeout",
            ),
            "nginx_upstream_unavailable": (
                "live-eval-upstream",
                "upstream_unavailable",
            ),
            "nginx_route_mismatch": ("nginx", "route_mismatch"),
            "nginx_gateway_pressure": ("nginx", "gateway_resource_pressure"),
        },
    }
    labels = labels_by_driver.get(scenario.driver)
    if labels is None or set(labels) != {item.id for item in scenario.hypotheses}:
        raise ValueError("Live scenario decision vocabulary is incomplete.")
    component_aliases = {
        alias: canonical
        for canonical in {component for component, _ in labels.values()}
        for alias in (canonical,)
    }
    if scenario.driver.startswith("postgres"):
        component_aliases["postgres"] = "postgresql"
    if scenario.driver == "redis_maxclients":
        component_aliases["redis"] = "live-eval-redis"
    if scenario.driver == "nginx_timeout":
        component_aliases["upstream"] = "live-eval-upstream"
    mechanism_aliases = {
        alias: mechanism
        for hypothesis, (_, mechanism) in labels.items()
        for alias in (hypothesis, mechanism)
    }
    return {
        "componentAliases": component_aliases,
        "mechanismAliases": mechanism_aliases,
        "labelsByHypothesis": {
            hypothesis: {"component": component, "mechanism": mechanism}
            for hypothesis, (component, mechanism) in labels.items()
        },
    }


class LivePostgresEvidenceMcpClient:
    """Expose one immutable, read-only and ownership-free evidence snapshot."""

    def __init__(self, observation: LiveFaultObservation) -> None:
        self._observation = observation

    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        return (
            McpToolDefinition(
                "InspectPostgresSessions",
                "Inspect safe PostgreSQL session wait-event facts.",
                {
                    "type": "object",
                    "properties": {
                        "state_filter": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["active", "idle in transaction"],
                            },
                        },
                        "include_wait_events": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "docker-live-postgres",
            ),
            McpToolDefinition(
                "InspectPostgresLockGraph",
                "Inspect safe PostgreSQL blocking-graph facts.",
                {
                    "type": "object",
                    "properties": {
                        "detect_deadlocks": {"type": "boolean"},
                        "analyze_blocking_chains": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "docker-live-postgres",
            ),
            McpToolDefinition(
                "VerifyServiceHealth",
                "Inspect database reachability separately from the synthetic business probe.",
                {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "enum": ["postgres_cluster"],
                        },
                        "check_connection_pool": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "docker-live-postgres",
            ),
        )

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        _validate_live_evidence_arguments(name, arguments)
        if name == "InspectPostgresSessions":
            return {
                "waitingSession": "waiter",
                "waitEventType": (
                    "Lock"
                    if self._observation.check_passed("waiter_has_lock_event")
                    else None
                ),
                "benchmarkEvidenceId": "postgres-wait-event-lock",
            }
        if name == "InspectPostgresLockGraph":
            return {
                "edge": "blocker->waiter",
                "blockerEdgeConfirmed": self._observation.check_passed(
                    "blocker_edge_confirmed"
                ),
                "benchmarkEvidenceId": "postgres-blocking-pid-edge",
            }
        if name == "VerifyServiceHealth":
            return {
                "databaseReachable": True,
                "connectivityStatus": "healthy",
                "businessProbeSucceeded": not self._observation.confirmed,
                "businessProbeStatus": (
                    "degraded" if self._observation.confirmed else "healthy"
                ),
                "benchmarkEvidenceId": "live-postgres-health-probe",
            }
        raise McpClientError(f"Docker Live evidence tool is not available: {name}")

    async def get_langchain_tools(self) -> list[Any]:
        tools: list[Any] = []
        for definition in await self.discover_tools():
            async def invoke(
                _name: str = definition.name,
                **arguments: object,
            ) -> object:
                return await self.call_tool(_name, arguments)

            tools.append(
                StructuredTool(
                    name=definition.name,
                    description=definition.description,
                    args_schema=definition.input_schema,
                    coroutine=invoke,
                )
            )
        return tools


def build_live_evidence_client(
    *,
    observation: LiveFaultObservation,
    evidence_context: LiveEvidenceContext,
    cls_client: LiveMcpClient | None,
    component_client: LiveMcpClient | None = None,
) -> LiveCompositeEvidenceMcpClient:
    """Compose the production tool boundary selected by prepared evidence mode."""
    return LiveCompositeEvidenceMcpClient(
        postgres_client=component_client or LivePostgresEvidenceMcpClient(observation),
        cls_client=cls_client if evidence_context.source == "cls" else None,
        context=evidence_context,
    )


def _validate_live_evidence_arguments(
    name: str,
    arguments: Mapping[str, object],
) -> None:
    if name == "VerifyServiceHealth":
        allowed = {"target", "check_connection_pool"}
        valid = (
            set(arguments) <= allowed
            and arguments.get("target", "postgres_cluster") == "postgres_cluster"
            and isinstance(arguments.get("check_connection_pool", True), bool)
        )
    elif name == "InspectPostgresSessions":
        allowed = {"state_filter", "include_wait_events"}
        raw_states = arguments.get("state_filter", [])
        valid = (
            set(arguments) <= allowed
            and isinstance(raw_states, list)
            and all(
                isinstance(item, str)
                and item in {"active", "idle in transaction"}
                for item in cast(list[object], raw_states)
            )
            and isinstance(arguments.get("include_wait_events", True), bool)
        )
    elif name == "InspectPostgresLockGraph":
        allowed = {"detect_deadlocks", "analyze_blocking_chains"}
        valid = set(arguments) <= allowed and all(
            isinstance(arguments.get(key, True), bool) for key in allowed
        )
    else:
        return
    if not valid:
        raise McpClientError("Docker Live evidence tool arguments are invalid.")


class ApplicationLiveDiagnosticAdapter:
    """Run Live evidence through the existing production diagnostic workflow."""

    def __init__(
        self,
        *,
        repositories: MemoryRepositories,
        llm_provider: LlmProvider,
        retrieval_tool: KnowledgeRetrievalToolRunner,
        accessible_knowledge_base_ids: Sequence[str] = (),
        owner_user_id: str | None = None,
        cls_mcp_client: LiveMcpClient | None = None,
        component_evidence_factory: Callable[
            [LiveFaultObservation], LiveMcpClient
        ] = LivePostgresEvidenceMcpClient,
    ) -> None:
        self._repositories = repositories
        self._llm_provider = llm_provider
        self._retrieval_tool = retrieval_tool
        self._knowledge_base_ids = tuple(accessible_knowledge_base_ids)
        self._owner_user_id = owner_user_id
        self._cls_mcp_client = cls_mcp_client
        self._component_evidence_factory = component_evidence_factory

    async def diagnose(
        self,
        *,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        evidence_context: LiveEvidenceContext,
    ) -> RunArtifact:
        owner_user_id = self._owner_user_id or f"benchmark:{run_id}"
        task_id = f"diagnostic_{uuid4().hex}"
        task = await self._repositories.diagnostics.create_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="accepted",
            query=scenario.title,
            input_payload=build_live_diagnostic_input(scenario),
            result_payload={},
        )
        scope = evidence_context.cls_scope
        service = AiopsDiagnosticService(
            repositories=self._repositories,
            llm_provider=self._llm_provider,
            retrieval_tool=self._retrieval_tool,
            mcp_client=build_live_evidence_client(
                observation=observation,
                evidence_context=evidence_context,
                cls_client=self._cls_mcp_client,
                component_client=self._component_evidence_factory(observation),
            ),
            cls_region=scope.region if scope is not None else "docker-live",
            cls_topic_id=scope.topic_id if scope is not None else "local-postgres",
            trusted_tool_arguments=(
                {"SearchLog": build_cls_search_arguments(scope)}
                if scope is not None
                else None
            ),
            tool_policies=proposal_tool_policies_for_scenario(scenario),
        )
        async for _ in service.stream(
            task=task,
            accessible_knowledge_base_ids=self._knowledge_base_ids,
        ):
            pass
        completed = await self._repositories.diagnostics.get_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        if completed is None:
            raise RuntimeError("Live diagnostic task disappeared during execution.")
        steps = await self._repositories.diagnostics.list_steps(
            owner_user_id=owner_user_id, task_id=task_id
        )
        evidence = await self._repositories.diagnostics.list_evidence(
            owner_user_id=owner_user_id, task_id=task_id
        )
        reports = await self._repositories.diagnostics.list_reports(
            owner_user_id=owner_user_id, task_id=task_id
        )
        audits = (
            await self._repositories.tool_call_audits.list_for_diagnostic_task(
                owner_user_id=owner_user_id,
                diagnostic_task_id=task_id,
            )
            if self._repositories.tool_call_audits is not None
            else []
        )
        return append_live_evidence_context(
            build_run_artifact(completed, steps, evidence, audits, reports),
            context=evidence_context,
        )


def append_live_evidence_context(
    artifact: RunArtifact,
    *,
    context: LiveEvidenceContext,
) -> RunArtifact:
    """Append trusted, non-secret evidence scope after artifact assembly."""
    scope = context.cls_scope
    readiness = context.readiness
    return replace(
        artifact,
        live_evidence=LiveEvidenceAudit(
            source=context.source,
            region=scope.region if scope is not None else None,
            topic_id=scope.topic_id if scope is not None else None,
            from_ms=scope.from_ms if scope is not None else None,
            to_ms=scope.to_ms if scope is not None else None,
            run_id=scope.run_id if scope is not None else None,
            scenario_id=scope.scenario_id if scope is not None else artifact.scenario_id,
            incident_id=context.incident_id,
            expected_log_count=(
                readiness.expected_log_count if readiness is not None else None
            ),
            indexed_log_count=(
                readiness.indexed_log_count if readiness is not None else None
            ),
            attempts=readiness.attempts if readiness is not None else None,
        ),
    )


def append_live_outcome(
    artifact: RunArtifact,
    *,
    recovery: LiveRecoveryRecord,
    verification: LiveVerification,
) -> RunArtifact:
    """Append trusted executor facts without persisting a raw backend PID."""
    return replace(
        artifact,
        live_recovery=LiveRecoveryAudit(
            action=recovery.action,
            target_ref=recovery.target_ref,
            approved=recovery.authorized,
            executed=recovery.executed,
            verified=verification.passed,
            authorization_code=recovery.authorization_code,
        ),
    )
