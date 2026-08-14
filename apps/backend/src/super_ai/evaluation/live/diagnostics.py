"""Production diagnostic adapter for answer-isolated Docker Live evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

from langchain_core.tools import StructuredTool

from super_ai.aiops import AiopsDiagnosticService
from super_ai.evaluation.artifacts import (
    LiveRecoveryAudit,
    RunArtifact,
    build_run_artifact,
)
from super_ai.evaluation.live.domain import (
    LiveFaultObservation,
    LiveRecoveryRecord,
    LiveScenario,
    LiveVerification,
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
        "decisionVocabulary": {
            "componentAliases": {
                "postgres": "postgresql",
                "postgresql": "postgresql",
            },
            "mechanismAliases": {
                "postgres_lock_blocking": "row_lock_blocking",
                "row_lock_blocking": "row_lock_blocking",
                "postgres_slow_query_without_lock": "slow_query_without_lock",
                "slow_query_without_lock": "slow_query_without_lock",
                "postgres_connectivity_failure": "connectivity_failure",
                "connectivity_failure": "connectivity_failure",
            },
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
                "Inspect the current synthetic service health probe.",
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
                    "Lock" if self._observation.waiter_has_lock_event else None
                ),
                "benchmarkEvidenceId": "postgres-wait-event-lock",
            }
        if name == "InspectPostgresLockGraph":
            return {
                "edge": "blocker->waiter",
                "blockerEdgeConfirmed": self._observation.blocker_edge_confirmed,
                "benchmarkEvidenceId": "postgres-blocking-pid-edge",
            }
        if name == "VerifyServiceHealth":
            return {
                "probeSucceeded": not self._observation.confirmed,
                "status": "degraded" if self._observation.confirmed else "healthy",
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
    ) -> None:
        self._repositories = repositories
        self._llm_provider = llm_provider
        self._retrieval_tool = retrieval_tool
        self._knowledge_base_ids = tuple(accessible_knowledge_base_ids)
        self._owner_user_id = owner_user_id

    async def diagnose(
        self,
        *,
        run_id: str,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
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
        service = AiopsDiagnosticService(
            repositories=self._repositories,
            llm_provider=self._llm_provider,
            retrieval_tool=self._retrieval_tool,
            mcp_client=LivePostgresEvidenceMcpClient(observation),
            cls_region="docker-live",
            cls_topic_id="local-postgres",
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
        return build_run_artifact(completed, steps, evidence, audits, reports)


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
            target_ref="synthetic_blocker",
            approved=recovery.authorized,
            executed=recovery.executed,
            verified=verification.passed,
            authorization_code=recovery.authorization_code,
        ),
    )
