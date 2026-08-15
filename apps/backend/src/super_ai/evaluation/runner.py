"""Answer-isolated orchestration for deterministic Snapshot benchmark runs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from super_ai.aiops import AiopsDiagnosticService
from super_ai.evaluation.artifacts import RunArtifact, build_run_artifact
from super_ai.evaluation.domain import PublicScenario
from super_ai.evaluation.scenarios import load_public_scenario, load_scenario_oracle
from super_ai.evaluation.scoring import EvaluationResult, score_run
from super_ai.evaluation.snapshot import SnapshotMcpClient
from super_ai.llm import LlmProvider
from super_ai.mcp.cached_client import RuntimeMcpClient
from super_ai.memory.repositories import EvaluationFailureStatus, JsonDict, MemoryRepositories
from super_ai.retrieval import (
    DEFAULT_RETRIEVAL_TOP_K,
    MAX_RETRIEVAL_TOP_K,
    KnowledgeRetrievalToolInput,
    KnowledgeRetrievalToolResult,
    KnowledgeRetrievalToolRunner,
)


@dataclass(frozen=True, slots=True)
class AgentVersion:
    """Reproducible code identity stored with an evaluation run."""

    git_sha: str
    workflow_version: str

    def as_json(self) -> JsonDict:
        return {"git_sha": self.git_sha, "workflow_version": self.workflow_version}


class BenchmarkRunError(RuntimeError):
    """Safe public failure for a benchmark production boundary."""

    def __init__(self, status: EvaluationFailureStatus, category: str) -> None:
        super().__init__("Benchmark run failed at a classified production boundary.")
        self.status = status
        self.category = category


class NullKnowledgeRetrievalTool:
    """RAG-off boundary that never constructs or calls retrieval dependencies."""

    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> KnowledgeRetrievalToolResult:
        del owner_user_id, accessible_knowledge_base_ids
        top_k = input.top_k if input.top_k is not None else DEFAULT_RETRIEVAL_TOP_K
        bounded_top_k = min(max(top_k, 1), MAX_RETRIEVAL_TOP_K)
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=bounded_top_k,
            results=[],
            citations=[],
        )


class DiagnosticRunAdapter(Protocol):
    """Boundary that prevents diagnostic runtimes from receiving an oracle."""

    async def run(
        self,
        *,
        run_id: str,
        scenario: PublicScenario,
        mcp_client: RuntimeMcpClient,
    ) -> RunArtifact: ...


class BenchmarkEvaluator(Protocol):
    """Evaluator-only boundary that may load the private scenario oracle."""

    def __call__(self, artifact: RunArtifact, scenario_dir: Path) -> EvaluationResult: ...


class EvaluationPersistence(Protocol):
    async def create_run(
        self,
        *,
        run_id: str,
        scenario_id: str,
        mode: str,
        suite_version: str,
        agent_version: JsonDict,
        model_configuration: JsonDict,
    ) -> object: ...

    async def fail_run(
        self,
        *,
        run_id: str,
        status: EvaluationFailureStatus,
        failure_category: str,
    ) -> object: ...

    async def finalize_run(
        self,
        *,
        run_id: str,
        result_id: str,
        result: EvaluationResult,
        diagnostic_task_id: str | None,
    ) -> object: ...


def build_application_diagnostic_input(scenario: PublicScenario) -> JsonDict:
    """Build the production diagnostic input exclusively from public scenario data."""
    return {
        "query": scenario.title,
        "alert": dict(scenario.alert),
        "hypotheses": [
            {"id": item.id, "description": item.description}
            for item in scenario.hypotheses
        ],
        "benchmarkScenarioId": scenario.id,
        "benchmarkMode": "snapshot",
    }


class ApplicationDiagnosticAdapter:
    """Run a Snapshot scenario through the existing production diagnostic workflow."""

    def __init__(
        self,
        *,
        repositories: MemoryRepositories,
        llm_provider: LlmProvider,
        retrieval_tool: KnowledgeRetrievalToolRunner,
        owner_user_id: str | None = None,
        accessible_knowledge_base_ids: Sequence[str] = (),
    ) -> None:
        self._repositories = repositories
        self._llm_provider = llm_provider
        self._retrieval_tool = retrieval_tool
        self._owner_user_id = owner_user_id
        self._knowledge_base_ids = tuple(accessible_knowledge_base_ids)

    async def run(
        self,
        *,
        run_id: str,
        scenario: PublicScenario,
        mcp_client: RuntimeMcpClient,
    ) -> RunArtifact:
        owner_user_id = self._owner_user_id or f"benchmark:{run_id}"
        task_id = f"diagnostic_{uuid4().hex}"
        task = await self._repositories.diagnostics.create_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="accepted",
            query=scenario.title,
            input_payload=build_application_diagnostic_input(scenario),
            result_payload={},
        )
        service = AiopsDiagnosticService(
            repositories=self._repositories,
            llm_provider=self._llm_provider,
            retrieval_tool=self._retrieval_tool,
            mcp_client=mcp_client,
            cls_region="snapshot",
            cls_topic_id="snapshot",
        )
        async for _event in service.stream(
            task=task,
            accessible_knowledge_base_ids=self._knowledge_base_ids,
        ):
            pass

        completed = await self._repositories.diagnostics.get_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        if completed is None:
            raise RuntimeError(f"Diagnostic task disappeared during benchmark run: {task_id}")
        steps = await self._repositories.diagnostics.list_steps(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        evidence = await self._repositories.diagnostics.list_evidence(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        reports = await self._repositories.diagnostics.list_reports(
            owner_user_id=owner_user_id,
            task_id=task_id,
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

class SnapshotBenchmarkRunner:
    """Run one frozen scenario while keeping the answer key evaluator-only."""

    def __init__(
        self,
        *,
        scenario_root: Path,
        adapter: DiagnosticRunAdapter,
        persistence: EvaluationPersistence,
        suite_version: str,
        model_configuration: JsonDict,
        evaluator: BenchmarkEvaluator | None = None,
    ) -> None:
        self._scenario_root = scenario_root.resolve()
        self._adapter = adapter
        self._persistence = persistence
        self._suite_version = suite_version
        self._model_configuration = dict(model_configuration)
        self._evaluator = evaluator or _evaluate_snapshot

    async def run(
        self,
        scenario_id: str,
        *,
        agent_version: AgentVersion,
        run_id: str | None = None,
    ) -> EvaluationResult:
        scenario_dir = self._scenario_directory(scenario_id)
        try:
            scenario = load_public_scenario(scenario_dir)
            if "snapshot" not in scenario.modes:
                raise ValueError(f"Scenario {scenario.id} does not support Snapshot mode.")
            snapshot = SnapshotMcpClient.from_yaml(scenario_dir / scenario.snapshot_file)
        except Exception as exc:
            raise BenchmarkRunError("infra_failed", "scenario_error") from exc

        resolved_run_id = run_id or f"eval-{uuid4().hex}"
        try:
            await self._persistence.create_run(
                run_id=resolved_run_id,
                scenario_id=scenario.id,
                mode="snapshot",
                suite_version=self._suite_version,
                agent_version=agent_version.as_json(),
                model_configuration=self._model_configuration,
            )
        except Exception as exc:
            raise BenchmarkRunError("infra_failed", "persistence_error") from exc

        try:
            artifact = await self._adapter.run(
                run_id=resolved_run_id,
                scenario=scenario,
                mcp_client=snapshot,
            )
        except Exception as exc:
            await self._record_failure(
                run_id=resolved_run_id,
                status="agent_failed",
                category="adapter_error",
            )
            raise BenchmarkRunError("agent_failed", "adapter_error") from exc

        if artifact.scenario_id != scenario.id or artifact.mode != "snapshot":
            await self._record_failure(
                run_id=resolved_run_id,
                status="agent_failed",
                category="artifact_invalid",
            )
            raise BenchmarkRunError("agent_failed", "artifact_invalid")

        try:
            result = self._evaluator(artifact, scenario_dir)
        except Exception as exc:
            await self._record_failure(
                run_id=resolved_run_id,
                status="infra_failed",
                category="evaluation_error",
            )
            raise BenchmarkRunError("infra_failed", "evaluation_error") from exc

        try:
            await self._persistence.finalize_run(
                result_id=f"result-{resolved_run_id}",
                run_id=resolved_run_id,
                result=result,
                diagnostic_task_id=artifact.diagnostic_task_id,
            )
        except Exception as exc:
            await self._record_failure(
                run_id=resolved_run_id,
                status="infra_failed",
                category="persistence_error",
            )
            raise BenchmarkRunError("infra_failed", "persistence_error") from exc
        return result

    async def _record_failure(
        self,
        *,
        run_id: str,
        status: EvaluationFailureStatus,
        category: str,
    ) -> None:
        try:
            await self._persistence.fail_run(
                run_id=run_id,
                status=status,
                failure_category=category,
            )
        except Exception as exc:
            raise BenchmarkRunError("infra_failed", "persistence_error") from exc

    def _scenario_directory(self, scenario_id: str) -> Path:
        if not scenario_id or any(character in scenario_id for character in ("/", "\\")):
            raise ValueError("Scenario ID must be a single directory name.")
        path = (self._scenario_root / scenario_id).resolve()
        if path.parent != self._scenario_root:
            raise ValueError("Scenario ID resolves outside the benchmark root.")
        return path


def _evaluate_snapshot(artifact: RunArtifact, scenario_dir: Path) -> EvaluationResult:
    oracle = load_scenario_oracle(scenario_dir)
    return score_run(artifact, oracle)
