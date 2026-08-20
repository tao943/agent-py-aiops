from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from super_ai.aiops import AiopsDiagnosticService, DiagnosisCasePersistor
from super_ai.api.app import AiopsDiagnosticRunner, create_app
from super_ai.llm import LlmProvider, RerankResult
from super_ai.mcp_client import LocalMcpClient, McpClientError, McpToolDefinition
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.repositories import DiagnosticTaskRecord, TenantScopeError
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories
from super_ai.redis_runtime.rate_limit import RateLimitDecision
from super_ai.retrieval import KnowledgeRetrievalTool
from super_ai.vector_store import StoredVectorChunk, VectorSearchResult


class FakeEmbeddingModel:
    def __init__(self) -> None:
        self.inputs: list[list[str]] = []

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.inputs.append(texts)
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self, hits: list[VectorSearchResult]) -> None:
        self.hits = hits

    def search_chunks(
        self,
        *,
        query_vector: Sequence[float],
        tenant_id: str,
        knowledge_base_ids: Sequence[str],
        limit: int,
    ) -> list[VectorSearchResult]:
        return list(self.hits[:limit])

    def list_chunks(
        self,
        *,
        tenant_id: str,
        knowledge_base_ids: Sequence[str],
    ) -> list[StoredVectorChunk]:
        return [
            StoredVectorChunk(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                knowledge_base_id=hit.knowledge_base_id,
                owner_user_id=hit.owner_user_id,
                tenant_id=hit.tenant_id,
                content=hit.content,
                source=hit.source,
                created_at=hit.created_at,
                metadata=hit.metadata,
            )
            for hit in self.hits
        ]


class FakeRerankModel:
    async def arerank(
        self, *, query: str, documents: Sequence[str], top_n: int
    ) -> list[RerankResult]:
        return [
            RerankResult(index=index, relevance_score=0.95 - index / 100)
            for index in range(min(top_n, len(documents)))
        ]


REPORT_MARKDOWN = """# 告警分析报告

---

## 📋 活跃告警清单

| 告警名称 | 级别 | 目标服务 | 首次触发时间 | 最新触发时间 | 状态 |
|---|---|---|---|---|---|
| WorkerCpuHigh | critical | worker | 未获取 | 未获取 | 活跃 |

---

## 🔍 告警根因分析1 - WorkerCpuHigh

### 告警详情
- **告警级别**: critical

### 日志证据
- SearchLog 返回 cpu_pressure

### 根因结论
证据支持 CPU 压力症状，但不足以确认最终根因。

---

## 🛠️ 处理方案执行1 - WorkerCpuHigh

### 已执行的排查步骤
1. 查询 CLS 日志

### 处理建议
依据 SOP 继续检查 CPU 饱和度。

### 预期效果
确认 CPU 压力来源。

---

## 📊 结论

### 整体评估
存在需要继续核实的 CPU 压力告警。

### 关键发现
- CLS 返回 cpu_pressure 证据。

### 后续建议
1. 补充 CPU 指标。

### 风险评估
当前风险较高，但影响范围尚未获取。"""


class FakeChatModel:
    def __init__(
        self,
        *,
        report_response: str = f"```markdown\n{REPORT_MARKDOWN}\n```",
        report_error: Exception | None = None,
    ) -> None:
        self.report_response = report_response
        self.report_error = report_error
        self.inputs: list[object] = []

    async def ainvoke(self, input: object) -> object:
        self.inputs.append(input)
        if "AIOps 诊断报告生成器" in str(input):
            if self.report_error is not None:
                raise self.report_error
            return self.report_response
        return '{"steps": []}'


class FakeLlmProvider:
    def __init__(
        self,
        *,
        report_response: str = f"```markdown\n{REPORT_MARKDOWN}\n```",
        report_error: Exception | None = None,
    ) -> None:
        self.chat_model = FakeChatModel(
            report_response=report_response,
            report_error=report_error,
        )

    def create_chat_model(self) -> FakeChatModel:
        return self.chat_model

    def create_embedding_model(self) -> FakeEmbeddingModel:
        return FakeEmbeddingModel()

    async def check_readiness(self) -> object:
        return object()


class FakeMcpClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def discover_tools(self) -> list[McpToolDefinition]:
        return [McpToolDefinition("SearchLog", "Search real logs", {})]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> object:
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return [
            {
                "type": "text",
                "text": json.dumps(
                    [
                        {
                            "LogJson": json.dumps(
                                {
                                    "timestamp": "2026-07-11T06:00:00Z",
                                    "level": "ERROR",
                                    "service": "worker",
                                    "event": "cpu_pressure",
                                    "message": "Worker CPU exceeded threshold",
                                    "latency_ms": "2450",
                                    "secret": "must-not-enter-report-context",
                                }
                            )
                        }
                    ]
                ),
            }
        ]


class FakeCaseIndexScheduler:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, str]] = []

    def schedule(self, *, owner_user_id: str, task_id: str) -> None:
        self.scheduled.append((owner_user_id, task_id))


@pytest.mark.asyncio
async def test_diagnostic_runs_sop_first_persists_evidence_and_audits(
    migrated_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logging.getLogger("super_ai.aiops.diagnostics").disabled = False
    caplog.set_level(logging.INFO, logger="super_ai.aiops.diagnostics")
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(create_memory_session_factory(engine))
        task = await repositories.diagnostics.create_task(
            owner_user_id="user-a",
            task_id="diagnostic-a",
            status="accepted",
            query="Investigate worker CPU alarm",
            input_payload={"alert": {"severity": "critical"}},
        )
        embedding = FakeEmbeddingModel()
        retrieval = KnowledgeRetrievalTool(
            embedding_model=embedding,
            vector_store=FakeVectorStore([_sop_hit("user-a")]),
            rerank_model=FakeRerankModel(),
        )
        mcp = FakeMcpClient()
        scheduler = FakeCaseIndexScheduler()
        provider = FakeLlmProvider()
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(LlmProvider, provider),
            retrieval_tool=retrieval,
            mcp_client=cast(LocalMcpClient, mcp),
            cls_region="ap-guangzhou",
            cls_topic_id="topic-a",
            case_persistor=DiagnosisCasePersistor(
                repositories=repositories,
                index_task_scheduler=scheduler,
            ),
        )

        events = [
            event
            async for event in service.stream(
                task=task,
                accessible_knowledge_base_ids=("kb_user-a",),
            )
        ]
        persisted = await repositories.diagnostics.get_task(
            owner_user_id="user-a",
            task_id=task.id,
        )
        reports = await repositories.diagnostics.list_reports(
            owner_user_id="user-a",
            task_id=task.id,
        )
        checkpoints = await repositories.diagnostics.list_checkpoints(
            owner_user_id="user-a",
            task_id=task.id,
        )
        steps = await repositories.diagnostics.list_steps(
            owner_user_id="user-a",
            task_id=task.id,
        )
        evidence = await repositories.diagnostics.list_evidence(
            owner_user_id="user-a",
            task_id=task.id,
        )
        report_links = await repositories.diagnostics.list_report_evidence_links(
            owner_user_id="user-a",
            task_id=task.id,
        )
        audits = await cast(Any, repositories.tool_call_audits).list_for_diagnostic_task(
            owner_user_id="user-a",
            diagnostic_task_id=task.id,
        )
        cases = await repositories.diagnostics.list_cases(owner_user_id="user-a")
    finally:
        await engine.dispose()

    assert embedding.inputs == [["Investigate worker CPU alarm"]]
    assert mcp.calls[0][0] == "SearchLog"
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.result_payload["noSopMatched"] is False
    assert reports[0].payload["reportGeneration"] == "llm"
    assert reports[0].title == "告警分析报告"
    assert reports[0].content == REPORT_MARKDOWN
    assert "```" not in reports[0].content
    report_prompt = next(
        str(item) for item in provider.chat_model.inputs if "AIOps 诊断报告生成器" in str(item)
    )
    assert "## 📋 活跃告警清单" in report_prompt
    assert "## 🔍 告警根因分析1" in report_prompt
    assert "## 🛠️ 处理方案执行1" in report_prompt
    assert "## 📊 结论" in report_prompt
    assert '"severity":"critical"' in report_prompt
    report_evidence = cast(list[dict[str, object]], reports[0].payload["evidence"])
    assert report_evidence[0]["tool"] == "SearchLog"
    assert "cpu_pressure" in str(report_evidence[0]["summary"])
    assert "must-not-enter-report-context" not in str(report_evidence[0]["summary"])
    assert reports[0].payload["rootCauseDecision"] is None
    assert [checkpoint.checkpoint_ns for checkpoint in checkpoints] == [
        "planner",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "replanner",
        "decision",
        "decision_validation",
        "recovery_planning",
        "policy_gate",
        "report",
    ]
    assert [audit.tool_name for audit in audits] == ["knowledge_retrieval", "SearchLog"]
    assert [step.phase for step in steps] == [
        "planner",
        "executor",
        "evidence_evaluation",
        "sufficiency_gate",
        "replanner",
        "decision",
        "decision_validation",
        "recovery_planning",
        "policy_gate",
        "report",
    ]
    assert {item.kind for item in evidence} == {"alert", "knowledge_reference", "log"}
    assert all(link.report_id == reports[0].id for link in report_links)
    assert {link.evidence_id for link in report_links} == {item.id for item in evidence}
    assert set(cast(list[str], reports[0].payload["evidenceIds"])) == {
        item.id for item in evidence
    }
    assert [event["type"] for event in events][-2:] == ["task.status", "complete"]
    assert any(event["type"] == "reference.source" for event in events)
    assert len(cases) == 1
    assert cases[0].task_id == task.id
    assert cases[0].report_id == reports[0].id
    assert cases[0].document_id
    assert cases[0].index_task_id
    assert scheduler.scheduled == [("user-a", cases[0].index_task_id)]
    events = [
        json.loads(record.message) for record in caplog.records if record.message.startswith("{")
    ]
    assert {event["event"] for event in events} == {
        "agent.aiops.started",
        "agent.aiops.completed",
    }
    assert all(event["diagnosticTaskId"] == task.id for event in events)
    assert "Investigate worker CPU alarm" not in "\n".join(
        record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_diagnostic_no_sop_and_mcp_failure_are_explicit(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(create_memory_session_factory(engine))
        task = await repositories.diagnostics.create_task(
            owner_user_id="user-a",
            task_id="diagnostic-b",
            status="accepted",
            query="Investigate missing logs",
        )
        service = AiopsDiagnosticService(
            repositories=repositories,
            llm_provider=cast(
                LlmProvider,
                FakeLlmProvider(report_error=RuntimeError("report model unavailable")),
            ),
            retrieval_tool=KnowledgeRetrievalTool(
                embedding_model=FakeEmbeddingModel(),
                vector_store=FakeVectorStore([]),
                rerank_model=FakeRerankModel(),
            ),
            mcp_client=cast(LocalMcpClient, FakeMcpClient(error=McpClientError("CLS unavailable"))),
            cls_region="ap-guangzhou",
            cls_topic_id="topic-a",
            case_persistor=DiagnosisCasePersistor(
                repositories=repositories,
                index_task_scheduler=FakeCaseIndexScheduler(),
            ),
        )

        events = [
            event
            async for event in service.stream(
                task=task,
                accessible_knowledge_base_ids=("kb_user-a",),
            )
        ]
        persisted = await repositories.diagnostics.get_task(
            owner_user_id="user-a",
            task_id=task.id,
        )
        reports = await repositories.diagnostics.list_reports(
            owner_user_id="user-a",
            task_id=task.id,
        )
        cases = await repositories.diagnostics.list_cases(owner_user_id="user-a")
    finally:
        await engine.dispose()

    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.result_payload["noSopMatched"] is True
    assert reports[0].payload["reportGeneration"] == "fallback"
    assert reports[0].content.startswith("# 告警分析报告")
    assert "未检索到匹配的 SOP" in reports[0].content
    assert "诊断工具执行失败" in reports[0].content
    assert "证据不足，无法确认根因" in reports[0].content
    assert any(event["type"] == "error" for event in events)
    assert any(_is_failed_tool_event(event) for event in events)
    assert cases == []


@dataclass
class FakeAiopsRunner:
    calls: list[str] = field(default_factory=lambda: [])

    async def stream(
        self,
        *,
        task: DiagnosticTaskRecord,
        accessible_knowledge_base_ids: Sequence[str],
    ) -> AsyncIterator[dict[str, object]]:
        self.calls.append(task.id)
        yield {
            "id": "evt_1",
            "type": "task.status",
            "channel": "aiops",
            "timestamp": "2026-07-10T00:00:00+00:00",
            "task": {"id": task.id, "status": "running", "message": "Planner", "progress": 10},
        }


class UnavailableRedisClient:
    async def ping(self) -> bool:
        raise ConnectionError("Redis is unavailable")

    async def eval(
        self, _script: str, _numkeys: int, *_keys_and_args: object
    ) -> object:
        raise RedisError("Redis is unavailable")


class AllowAllRateLimits:
    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision:
        del owner_id, action
        return RateLimitDecision(
            allowed=True,
            remaining=1,
            retry_after_seconds=0,
            mode="local_fallback",
        )


@pytest.mark.asyncio
async def test_aiops_stream_requires_task_owner_and_falls_back_when_redis_is_unavailable(
    migrated_database_url: str,
) -> None:
    runner = FakeAiopsRunner()
    app = create_app(
        database_url=migrated_database_url,
        aiops_diagnostic_runner=cast(AiopsDiagnosticRunner, runner),
        redis_client=cast(Redis, UnavailableRedisClient()),
        vector_store=cast(Any, FakeVectorStore([])),
        rate_limit_service=AllowAllRateLimits(),
    )
    transport = httpx.ASGITransport(
        app=app
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        owner = await _register(client, "owner@example.com")
        other = await _register(client, "other@example.com")
        create_response = await client.post(
            "/aiops/diagnostics",
            headers=_auth_headers(owner["accessToken"]),
            json={"query": "Inspect CPU", "alert": {"severity": "high"}},
        )
        diagnostic_id = create_response.json()["data"]["id"]
        repositories = app.state.memory_repositories
        owner_user_id = owner["user"]["id"]
        background_jobs = repositories.background_jobs
        assert background_jobs is not None
        diagnostic_job = await background_jobs.find_for_resource(
            owner_user_id=owner_user_id,
            resource_type="aiops_diagnostic",
            resource_id=diagnostic_id,
        )
        assert diagnostic_job is not None
        assert diagnostic_job.max_attempts == 3
        stream = await client.post(
            f"/aiops/diagnostics/{diagnostic_id}:stream",
            headers=_auth_headers(owner["accessToken"]),
        )
        phases = (
            "planner",
            "validator_router",
            "sufficiency_gate",
            "decision_validation",
            "recovery_planning",
            "policy_gate",
        )
        for sequence, phase in enumerate(phases, start=1):
            payload: dict[str, object] = {"phase": phase}
            if phase == "planner":
                payload.update(
                    {
                        "workflowVersion": "evidence-driven-v4",
                        "modelCallCount": 2,
                        "modelCallAudits": [
                            {
                                "role": "planner",
                                "attempt": 1,
                                "durationMs": 120,
                                "cacheHit": False,
                                "safeErrorCode": None,
                                "prompt": "sentinel-private-prompt",
                            }
                        ],
                        "hypothesisAssessments": [
                            {
                                "id": "cause-a",
                                "disposition": "causally_inactive",
                                "evidenceIds": ["evidence-owner"],
                                "reasonCode": "not_in_active_path",
                                "assessmentSource": "deterministic",
                            }
                        ],
                        "rawResponse": "sentinel-private-response",
                    }
                )
            elif phase == "validator_router":
                payload.update(
                    {
                        "validationRequired": False,
                        "validationSkipped": True,
                        "validationReasonCodes": [],
                        "validationSkipReason": "deterministic_evidence_sufficient",
                    }
                )
            await repositories.diagnostics.create_step(
                owner_user_id=owner_user_id,
                step_id=f"step-owner-{sequence}",
                task_id=diagnostic_id,
                sequence=sequence,
                phase=phase,
                status="completed",
                payload=payload,
            )
            await repositories.diagnostics.save_checkpoint(
                owner_user_id=owner_user_id,
                checkpoint_record_id=f"checkpoint-owner-{sequence}",
                task_id=diagnostic_id,
                thread_id=f"aiops:{diagnostic_id}",
                checkpoint_ns=phase,
                checkpoint_id=f"checkpoint-id-{sequence}",
                checkpoint_payload={
                    "phase": phase,
                    "executionBlob": "sentinel-private-checkpoint",
                },
                metadata={"node": phase},
            )
        await repositories.diagnostics.create_evidence(
            owner_user_id=owner_user_id,
            evidence_id="evidence-owner",
            task_id=diagnostic_id,
            step_id="step-owner-1",
            kind="alert",
            source="test",
            summary="Owner-scoped evidence.",
        )
        await repositories.diagnostics.add_report(
            owner_user_id=owner_user_id,
            report_id="report-owner",
            task_id=diagnostic_id,
            title="Owner report",
            content="Owner-scoped report.",
        )
        denied = await client.post(
            f"/aiops/diagnostics/{diagnostic_id}:stream",
            headers=_auth_headers(other["accessToken"]),
        )
        history = await client.get(
            "/aiops/diagnostics",
            headers=_auth_headers(owner["accessToken"]),
        )
        owner_chain = await client.get(
            f"/aiops/diagnostics/{diagnostic_id}/evidence-chain",
            headers=_auth_headers(owner["accessToken"]),
        )
        denied_chain = await client.get(
            f"/aiops/diagnostics/{diagnostic_id}/evidence-chain",
            headers=_auth_headers(other["accessToken"]),
        )

    assert create_response.status_code == 202
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: task.status" in stream.text
    assert runner.calls == [diagnostic_id]
    assert history.status_code == 200
    assert [item["id"] for item in history.json()["data"]["items"]] == [diagnostic_id]
    assert owner_chain.status_code == 200
    assert owner_chain.json()["data"]["task"]["id"] == diagnostic_id
    assert [item["phase"] for item in owner_chain.json()["data"]["steps"]] == list(phases)
    assert [
        item["checkpointNamespace"] for item in owner_chain.json()["data"]["checkpoints"]
    ] == list(phases)
    execution = owner_chain.json()["data"]["execution"]
    assert execution == {
        "graphVersion": "aiops-diagnostic-v2",
        "workflowVersion": "evidence-driven-v4",
        "modelCallCount": 2,
        "modelCalls": [
            {
                "role": "planner",
                "attempt": 1,
                "durationMs": 120,
                "cacheHit": False,
                "safeErrorCode": None,
            }
        ],
        "validator": {
            "required": False,
            "skipped": True,
            "reasonCodes": [],
            "skipReason": "deterministic_evidence_sufficient",
        },
        "resumeCount": 0,
    }
    assert all("payload" not in item for item in owner_chain.json()["data"]["checkpoints"])
    assert "sentinel-private" not in owner_chain.text
    planner_payload = owner_chain.json()["data"]["steps"][0]["payload"]
    assert planner_payload["hypothesisAssessments"][0]["status"] == "refuted"
    assert (
        planner_payload["hypothesisAssessments"][0]["disposition"]
        == "causally_inactive"
    )
    assert denied_chain.status_code == 403
    assert denied_chain.json()["error"]["code"] == "AUTH_FORBIDDEN"
    other_user_id = other["user"]["id"]
    with pytest.raises(TenantScopeError):
        await repositories.diagnostics.list_steps(
            owner_user_id=other_user_id, task_id=diagnostic_id
        )
    assert await repositories.diagnostics.list_checkpoints(
        owner_user_id=other_user_id, task_id=diagnostic_id
    ) == []
    with pytest.raises(TenantScopeError):
        await repositories.diagnostics.list_evidence(
            owner_user_id=other_user_id, task_id=diagnostic_id
        )
    assert await repositories.diagnostics.list_reports(
        owner_user_id=other_user_id, task_id=diagnostic_id
    ) == []


@pytest.mark.asyncio
async def test_diagnosis_case_api_lists_only_owner_cases_and_denies_direct_access(
    migrated_database_url: str,
) -> None:
    app = create_app(
        database_url=migrated_database_url,
        vector_store=cast(Any, FakeVectorStore([])),
        rate_limit_service=AllowAllRateLimits(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        owner = await _register(client, "case-owner@example.com")
        other = await _register(client, "case-other@example.com")
        repositories = app.state.memory_repositories
        task = await repositories.diagnostics.create_task(
            owner_user_id=owner["user"]["id"],
            task_id="diagnostic-case-owner",
            status="succeeded",
            query="Investigate checkout timeout",
        )
        report = await repositories.diagnostics.add_report(
            owner_user_id=owner["user"]["id"],
            report_id="report-case-owner",
            task_id=task.id,
            title="Checkout timeout report",
            content="Evidence-backed report.",
        )
        document = await repositories.documents.create_document(
            owner_user_id=owner["user"]["id"],
            document_id="document-case-owner",
            knowledge_base_id=f"kb_{owner['user']['id']}",
            filename="checkout-case.md",
            size_bytes=22,
            mime_type="text/markdown",
            content_hash="sha256:case-owner",
            metadata={"knowledgeType": "diagnostic-case"},
        )
        index_task = await repositories.document_index_tasks.create_task(
            owner_user_id=owner["user"]["id"],
            task_id="index-case-owner",
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
        )
        case = await repositories.diagnostics.create_case(
            owner_user_id=owner["user"]["id"],
            case_id="case-owner",
            task_id=task.id,
            report_id=report.id,
            document_id=document.id,
            index_task_id=index_task.id,
            alert_name="CheckoutTimeout",
            service="checkout",
            keywords=["checkout", "timeout"],
            root_cause="",
            remediation="",
            summary="Evidence-backed report.",
            evidence_ids=[],
        )
        owner_list = await client.get(
            "/aiops/diagnostic-cases",
            headers=_auth_headers(owner["accessToken"]),
        )
        owner_detail = await client.get(
            f"/aiops/diagnostic-cases/{case.id}",
            headers=_auth_headers(owner["accessToken"]),
        )
        denied_detail = await client.get(
            f"/aiops/diagnostic-cases/{case.id}",
            headers=_auth_headers(other["accessToken"]),
        )

    assert owner_list.status_code == 200
    assert [item["id"] for item in owner_list.json()["data"]["items"]] == [case.id]
    assert owner_detail.status_code == 200
    assert owner_detail.json()["data"]["id"] == case.id
    assert denied_detail.status_code == 403
    assert case.id not in denied_detail.text


@pytest.mark.asyncio
async def test_diagnostic_evidence_repository_rejects_cross_tenant_step(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    try:
        repositories = create_sqlalchemy_memory_repositories(create_memory_session_factory(engine))
        task_a = await repositories.diagnostics.create_task(
            owner_user_id="user-a",
            task_id="diagnostic-owner-a",
            status="accepted",
            query="Owner A",
        )
        task_b = await repositories.diagnostics.create_task(
            owner_user_id="user-b",
            task_id="diagnostic-owner-b",
            status="accepted",
            query="Owner B",
        )
        step_a = await repositories.diagnostics.create_step(
            owner_user_id="user-a",
            step_id="step-owner-a",
            task_id=task_a.id,
            sequence=1,
            phase="planner",
            status="completed",
        )

        with pytest.raises(TenantScopeError):
            await repositories.diagnostics.create_evidence(
                owner_user_id="user-b",
                evidence_id="evidence-cross-tenant",
                task_id=task_b.id,
                step_id=step_a.id,
                kind="ticket",
                source="ticket-system",
                summary="Must not be written.",
            )
    finally:
        await engine.dispose()


def _sop_hit(owner_user_id: str) -> VectorSearchResult:
    return VectorSearchResult(
        chunk_id="chunk_sop",
        document_id="document_sop",
        knowledge_base_id=f"kb_{owner_user_id}",
        owner_user_id=owner_user_id,
        tenant_id=owner_user_id,
        content="Check CPU saturation and query worker logs before mitigation.",
        source="worker-cpu-sop.md",
        created_at=1,
        metadata={"kind": "sop"},
        score=0.92,
    )


async def _register(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post(
        "/auth/register",
        json={
            "email": email,
            "displayName": email.split("@")[0],
            "password": "correct horse battery staple",
        },
    )
    return cast(dict[str, Any], response.json()["data"])


def _auth_headers(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _is_failed_tool_event(event: dict[str, object]) -> bool:
    tool_call = event.get("toolCall")
    if not isinstance(tool_call, Mapping):
        return False
    tool_call_payload = cast(Mapping[str, object], tool_call)
    return bool(
        event.get("type") == "tool.call" and tool_call_payload.get("status") == "failed"
    )
