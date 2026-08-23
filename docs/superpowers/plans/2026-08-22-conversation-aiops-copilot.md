# Conversation Agent 与 AIOps Agent 关联 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repository's approved execution mode is primary-agent inline execution; do not dispatch implementation subagents.

**Goal:** 让 Conversation Agent 以最小权限查询 Incident、启动和跟踪 AIOps 诊断、读取证据与报告，并通过持久 Chat Run 与 Tool Execution 实现请求幂等、断线重订阅和 Worker 恢复。

**Architecture:** Conversation Agent 保持交互与编排职责，使用 Intent Router 选择一组 owner-scoped AIOps Bridge Tools；Bridge 只包装现有 Incident、Diagnostic、Report、Evidence 和 Background Job 应用服务。第二阶段以 PostgreSQL `chat_agent_runs`/`chat_run_events` 为真相源，并复用现有数据库 lease Worker；Redis 不承担幂等或恢复正确性。

**Tech Stack:** Python 3.13、FastAPI、Pydantic 2、LangChain StructuredTool、SQLAlchemy 2、asyncpg、PostgreSQL 16、现有 BackgroundJobRuntime、Vue 3、Pinia、TypeScript、Vitest、pytest、Ruff、Pyright。

## Global Constraints

- 不增加依赖、外部服务、原生二进制或新的模型供应商。
- Chat 不得直接执行恢复动作，不得绕过 AIOps Validator 或 Policy Gate。
- Chat 不得暴露其他 owner 的 Incident、Diagnostic、Report、Evidence 或审批请求。
- 新数据、API、SSE 和 UI 不保存、不返回、不展示模型原始 reasoning。
- PostgreSQL 唯一约束是幂等最终保障；Redis 不是持久化真相源。
- 旧 `messages:stream` 在迁移期保持兼容，前端迁移后只标记 deprecated，不在本计划删除。
- 迁移 revision 固定为 `202608220002`，下游 revision 为 `202608220001`。
- 测试使用离线 fake runner/provider 和 PostgreSQL 集成库；不调用真实 LLM、CLS 或 Docker Live。
- 只运行聚焦测试、Ruff 和 Pyright，不运行全量 pytest。

---

## File Structure

### Backend files to create

- `apps/backend/src/super_ai/chat/intent.py`：意图值、结构化路由结果、规则优先 Router。
- `apps/backend/src/super_ai/chat/tool_policy.py`：意图到允许工具名称的唯一映射。
- `apps/backend/src/super_ai/chat/aiops_bridge.py`：认证 owner 闭包绑定的 Bridge records/service/StructuredTool factory。
- `apps/backend/src/super_ai/chat/runs.py`：Run 状态机、执行服务、错误分类和批量 delta。
- `apps/backend/src/super_ai/chat/routes.py`：Run 创建、查询和 SSE 订阅 Router。
- `apps/backend/src/super_ai/chat/evaluation.py`：Conversation Eval 场景、runner 和硬门评分。
- `apps/backend/alembic/versions/202608220002_add_chat_agent_runs.py`：Run/Event/Approval 表。
- `apps/backend/tests/test_chat_intent_router.py`：Router 和 Tool Policy 单测。
- `apps/backend/tests/test_chat_aiops_bridge.py`：Bridge 单元与租户安全测试。
- `apps/backend/tests/test_chat_runs_repository.py`：PostgreSQL 并发幂等和事件测试。
- `apps/backend/tests/test_chat_runs_api.py`：Run API、Background Job 和 SSE 恢复测试。
- `apps/backend/tests/test_conversation_eval.py`：12 场景离线评测和硬门测试。

### Backend files to modify

- `apps/backend/src/super_ai/chat/streaming.py`：接入 Router/Tool Policy，移除 reasoning，批量 delta。
- `apps/backend/src/super_ai/chat/__init__.py`：导出新增公开类型。
- `apps/backend/src/super_ai/alert_ingestion/repositories.py`：增加只读 Incident record/query protocol。
- `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`：实现 owner-scoped Incident 查询。
- `apps/backend/src/super_ai/memory/models.py`：增加 Run/Event/Approval ORM 模型。
- `apps/backend/src/super_ai/memory/repositories.py`：增加 records、repository protocols 和组合字段。
- `apps/backend/src/super_ai/memory/sqlalchemy.py`：实现 ChatRunRepository。
- `apps/backend/src/super_ai/memory/extended_sqlalchemy.py`：实现 Chat Tool Execution 与 Background Job 终态失败持久化。
- `apps/backend/src/super_ai/jobs/runtime.py`：区分可重试 attempt 失败与不可重试终态失败。
- `apps/backend/src/super_ai/memory/sqlalchemy.py`：在现有 `create_sqlalchemy_memory_repositories` 中组装新增 Repository。
- `apps/backend/src/super_ai/api/app.py`：挂载 Chat Router、注册 `chat_agent_run` handler、旧 endpoint 适配。
- `apps/backend/src/super_ai/error_catalog.py`：增加公开错误码。
- `apps/backend/tests/test_stream_rag_chat_api.py`：更新 reasoning 和 batching 契约。
- `apps/backend/tests/test_chat_sessions_api.py`：历史 reasoning API 过滤回归。

### Shared/frontend files to modify

- `packages/api-contracts/src/chat.ts`：Run 请求/响应和状态类型；移除新消息的 reasoning contract。
- `packages/api-contracts/src/sse.ts`：删除 `reasoning.delta`，增加 `run.status`/`run.restarted`。
- `packages/api-contracts/src/openapi.ts`：Run endpoints/schema。
- `packages/api-contracts/tests/api-contracts.test.ts`：契约测试。
- `apps/frontend/src/api/sseClient.ts`：解析 SSE `id:` 并支持 `Last-Event-ID`。
- `apps/frontend/src/chat/chatClient.ts`：Run 创建/查询/订阅 client。
- `apps/frontend/src/stores/chat.ts`：Run 生命周期、批量追加和重订阅。
- `apps/frontend/src/components/ChatTranscript.vue`：移除 reasoning UI。
- `apps/frontend/src/components/DiagnosticResultCard.vue`：确定性展示 AIOps 安全字段，不采用 LLM 改写值。
- `apps/frontend/src/chat/chatClient.test.ts`：client URL/header 测试。
- `apps/frontend/src/stores/chat.test.ts`：断线恢复和 draft 收敛测试。
- `apps/frontend/src/api/sseClient.test.ts`：SSE sequence 解析测试。

---

### Task 1: Intent Router 与最小工具策略

**Files:**
- Create: `apps/backend/src/super_ai/chat/intent.py`
- Create: `apps/backend/src/super_ai/chat/tool_policy.py`
- Create: `apps/backend/tests/test_chat_intent_router.py`
- Modify: `apps/backend/src/super_ai/chat/__init__.py`

**Interfaces:**
- Consumes: 当前 Chat message content；可选 `StructuredRouterModel.route(content) -> Mapping[str, object]`。
- Produces: `ChatIntent`, `ChatRoute`, `ChatIntentRouter.route(content)`, `allowed_tools_for(intent)`。

- [ ] **Step 1: 写 Router 和 Tool Policy 失败测试**

```python
@pytest.mark.asyncio
async def test_explicit_diagnostic_task_id_routes_without_model() -> None:
    model = FakeRouterModel(AssertionError("model must not run"))
    route = await ChatIntentRouter(model).route(
        "查看 diagnostic_abc123 的报告和证据"
    )
    assert route.intent == "diagnostic_status"
    assert route.diagnostic_task_id == "diagnostic_abc123"
    assert route.source == "rule"


def test_recovery_intent_only_exposes_approval_tool() -> None:
    assert allowed_tools_for("recovery_request") == frozenset({
        "get_diagnostic_status",
        "get_diagnostic_report",
        "create_recovery_approval_request",
    })
    assert "restart_service" not in allowed_tools_for("recovery_request")
```

- [ ] **Step 2: 运行测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py -q`  
Expected: collection FAIL，`super_ai.chat.intent` 不存在。

- [ ] **Step 3: 实现严格类型和规则优先 Router**

```python
ChatIntent = Literal[
    "general_chat", "knowledge_question", "incident_query",
    "start_diagnostic", "diagnostic_status", "recovery_request",
]

@dataclass(frozen=True, slots=True)
class ChatRoute:
    intent: ChatIntent
    confidence: float
    source: Literal["rule", "model", "fallback"]
    incident_id: str | None = None
    diagnostic_task_id: str | None = None
    needs_clarification: bool = False

class ChatIntentRouter:
    async def route(self, content: str) -> ChatRoute:
        explicit = _route_explicit_identifiers(content)
        if explicit is not None:
            return explicit
        try:
            return _validate_model_route(await self._model.route(content))
        except Exception:
            return ChatRoute("general_chat", 0.0, "fallback")
```

`_validate_model_route` 必须拒绝未知 intent、越界 confidence、非字符串 ID；模型结果置信度
低于 `0.70` 时设置 `needs_clarification=True`，且后续只能获得只读/无副作用工具。

- [ ] **Step 4: 运行 Router 测试确认绿灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交 Task 1**

```powershell
git add apps/backend/src/super_ai/chat apps/backend/tests/test_chat_intent_router.py
git commit -m "feat: add chat intent router and tool policy"
```

### Task 2: Owner-scoped Incident 查询与只读 Bridge

**Files:**
- Modify: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Create: `apps/backend/src/super_ai/chat/aiops_bridge.py`
- Create: `apps/backend/tests/test_chat_aiops_bridge.py`

**Interfaces:**
- Consumes: `AlertIncidentModel`、`DiagnosticMemoryRepository`、认证注入的 owner ID。
- Produces: `AlertIncidentQueryRepository.list_active/get_owned`，`AiopsBridgeService` 的五个查询方法。

- [ ] **Step 1: 写租户隔离和安全 payload 失败测试**

```python
@pytest.mark.asyncio
async def test_list_active_incidents_is_owner_scoped(migrated_database_url: str) -> None:
    owner_a, owner_b = await seed_two_incident_owners(migrated_database_url)
    service = build_bridge(migrated_database_url)
    items = await service.list_active_incidents(owner_user_id=owner_a, limit=10)
    assert [item.id for item in items] == ["incident_owner_a"]
    assert all("normalized_payload" not in asdict(item) for item in items)

@pytest.mark.asyncio
async def test_get_report_does_not_expose_checkpoint_or_reasoning() -> None:
    payload = await bridge.get_diagnostic_report("owner_a", "diagnostic_a")
    serialized = json.dumps(payload).lower()
    assert "reasoning" not in serialized
    assert "checkpoint" not in serialized

def test_structured_tool_schema_cannot_accept_owner_identity() -> None:
    tools = build_aiops_bridge_tools(owner_user_id="owner_a", service=bridge)
    schema = tools["get_incident"].args_schema.model_json_schema()
    assert "owner_user_id" not in json.dumps(schema)

@pytest.mark.asyncio
async def test_model_cannot_forge_owner_or_read_other_owner_incident() -> None:
    tool = build_aiops_bridge_tools(owner_user_id="owner_a", service=bridge)["get_incident"]
    with pytest.raises(TypeError):
        await tool.ainvoke({"incident_id": "incident_b", "owner_user_id": "owner_b"})
    with pytest.raises(BridgeResourceNotFound):
        await tool.ainvoke({"incident_id": "incident_b"})
```

- [ ] **Step 2: 运行 Bridge 测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_aiops_bridge.py -q`  
Expected: FAIL，查询协议和 Bridge 尚不存在。

- [ ] **Step 3: 增加 Incident record/query protocol 与 SQLAlchemy 实现**

```python
@dataclass(frozen=True, slots=True)
class AlertIncidentRecord:
    id: str
    owner_user_id: str
    status: str
    alert_name: str
    service: str
    severity: str
    last_seen_at: datetime
    diagnostic_task_id: str | None

class AlertIncidentQueryRepository(Protocol):
    async def list_active(self, *, owner_user_id: str, limit: int) -> list[AlertIncidentRecord]: ...
    async def get_owned(self, *, owner_user_id: str, incident_id: str) -> AlertIncidentRecord | None: ...
```

SQL 查询必须同时包含 `owner_user_id` 和 `status == "active"`（list），按 `updated_at DESC`
排序并将 limit 限制在 `1..50`。

- [ ] **Step 4: 实现 Bridge 的安全 DTO 和只读方法**

```python
class AiopsBridgeService:
    async def list_active_incidents(self, *, owner_user_id: str, limit: int = 10) -> tuple[IncidentSummary, ...]: ...
    async def get_incident(self, *, owner_user_id: str, incident_id: str) -> IncidentSummary: ...
    async def get_diagnostic_status(self, *, owner_user_id: str, task_id: str) -> DiagnosticStatus: ...
    async def get_diagnostic_report(self, *, owner_user_id: str, task_id: str) -> PublicDiagnosticReport: ...
    async def get_diagnostic_evidence(self, *, owner_user_id: str, task_id: str, limit: int = 20) -> tuple[PublicEvidence, ...]: ...
```

不存在和越权统一抛 `BridgeResourceNotFound`。Report 明确复制 `rootCause`、恢复模式、
`executionPermitted`、`humanApprovalRequired`、Validator 降级状态和 citation IDs；Evidence
只复制 id/kind/source/summary/createdAt 和允许的 Evaluation 公开字段。

`build_aiops_bridge_tools(*, owner_user_id, service)` 必须通过闭包或不可变 partial 注入认证
owner。每个 `args_schema` 只包含业务参数，不能包含 `owner_user_id`；即使模型在 tool input
中伪造 owner 字段，Pydantic `extra="forbid"` 也必须在调用 Bridge 前拒绝。

- [ ] **Step 5: 运行 Bridge 测试确认绿灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_aiops_bridge.py -q`  
Expected: PASS。

- [ ] **Step 6: 提交 Task 2**

```powershell
git add apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/chat/aiops_bridge.py apps/backend/tests/test_chat_aiops_bridge.py
git commit -m "feat: add owner scoped aiops bridge queries"
```

### Task 3: 诊断启动幂等与恢复审批安全边界

**Files:**
- Modify: `apps/backend/src/super_ai/chat/aiops_bridge.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Modify: `apps/backend/tests/test_chat_aiops_bridge.py`

**Interfaces:**
- Consumes: 已有 Incident 关联的 diagnostic task、`BackgroundJobRepository.find_for_resource`。
- Produces: `start_incident_diagnostic` 和 `create_recovery_approval_request` 的应用服务契约；审批持久化延迟到 Task 6。

- [ ] **Step 1: 写首次创建/重复复用/已解决拒绝测试**

```python
@pytest.mark.asyncio
async def test_start_diagnostic_reuses_incident_task() -> None:
    first = await bridge.start_incident_diagnostic(owner_user_id="owner", incident_id="inc_1")
    second = await bridge.start_incident_diagnostic(owner_user_id="owner", incident_id="inc_1")
    assert second.diagnostic_task_id == first.diagnostic_task_id
    assert second.background_job_id == first.background_job_id
    assert second.reused is True

@pytest.mark.asyncio
async def test_start_diagnostic_rejects_resolved_incident() -> None:
    with pytest.raises(IncidentNotActive):
        await bridge.start_incident_diagnostic(owner_user_id="owner", incident_id="resolved_1")
```

- [ ] **Step 2: 运行目标测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_aiops_bridge.py -q`  
Expected: FAIL，写入方法尚不存在。

- [ ] **Step 3: 抽取共享诊断调度应用服务**

把 `POST /aiops/diagnostics` 和 Alert ingestion 中“创建 Diagnostic Task + Background Job”的
共享逻辑抽成 owner-scoped 服务：

```python
@dataclass(frozen=True, slots=True)
class DiagnosticScheduleResult:
    diagnostic_task_id: str
    background_job_id: str
    reused: bool

class IncidentDiagnosticScheduler(Protocol):
    async def schedule_for_incident(
        self, *, owner_user_id: str, incident_id: str, note: str | None
    ) -> DiagnosticScheduleResult: ...
```

Repository 事务必须锁定 owned active Incident；若已有 `diagnostic_task_id`，查询其 job 并
返回 reused；否则创建任务和 job 并回写 Incident。不能从 Bridge 直接运行 LangGraph。

- [ ] **Step 4: 加入审批请求前置校验（暂用 Repository protocol fake）**

```python
async def create_recovery_approval_request(...):
    report = await self.get_diagnostic_report(...)
    if not report.human_approval_required or report.recovery_mode == "no_action":
        raise RecoveryApprovalNotAllowed
    return await self._approval_requests.create_or_get(
        owner_user_id=owner_user_id,
        diagnostic_task_id=task_id,
        proposal_fingerprint=fingerprint_public_proposal(report),
        request_reason=bounded_reason(reason),
        chat_run_id=chat_run_id,
    )
```

输出固定 `status="pending"`、`execution_permitted=False`；接口中不得出现 approve/execute。

- [ ] **Step 5: 运行 Bridge 与 Alert ingestion 回归**

Run: `cd apps/backend && uv run pytest tests/test_chat_aiops_bridge.py tests/test_postgresql_alert_ingestion.py tests/test_alert_ingestion_app.py -q`  
Expected: PASS，重复 firing 仍只创建一个诊断。

- [ ] **Step 6: 提交 Task 3**

```powershell
git add apps/backend/src/super_ai/chat/aiops_bridge.py apps/backend/src/super_ai/alert_ingestion apps/backend/tests
git commit -m "feat: add idempotent incident diagnostic bridge"
```

### Task 4: Chat Agent 工具路由、Reasoning 移除与 SSE 批量输出

**Files:**
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/test_stream_rag_chat_api.py`
- Modify: `apps/backend/tests/test_chat_sessions_api.py`

**Interfaces:**
- Consumes: Task 1 `ChatRoute/allowed_tools_for`，Task 2/3 Bridge tool factory。
- Produces: 每轮按 route 组装的 tools；不含 reasoning 的消息/SSE；32 chars/50 ms batching。

- [ ] **Step 1: 写工具最小暴露与 reasoning 泄露失败测试**

```python
@pytest.mark.asyncio
async def test_diagnostic_status_only_receives_status_tools() -> None:
    runner = RecordingRunner([ChatAgentContentDelta("done")])
    await collect(service.stream_message(content="查看 diagnostic_123 的状态", ...))
    assert runner.last_request.tool_names == (
        "get_diagnostic_status", "get_diagnostic_report", "get_diagnostic_evidence"
    )

@pytest.mark.asyncio
async def test_reasoning_event_is_never_emitted_or_persisted() -> None:
    events = await collect(stream_with(ChatAgentReasoningDelta("private"), ChatAgentContentDelta("public")))
    assert all(event["type"] != "reasoning.delta" for event in events)
    message = await repository.last_assistant_message()
    assert "reasoning" not in message.metadata
    assert "private" not in json.dumps(message.metadata)

@pytest.mark.asyncio
async def test_llm_cannot_override_structured_diagnostic_safety_result() -> None:
    events = await collect(stream_with(
        bridge_report(execution_permitted=False, recovery_mode="manual_review"),
        ChatAgentContentDelta("可以自动执行恢复"),
    ))
    result = next(event for event in events if event["type"] == "diagnostic.result")
    assert result["diagnostic"]["executionPermitted"] is False
    assert result["diagnostic"]["recoveryMode"] == "manual_review"
```

- [ ] **Step 2: 写 batching 失败测试**

```python
async def test_content_delta_is_not_split_into_characters() -> None:
    events = await collect(stream_with(ChatAgentContentDelta("abcdefghij" * 4)))
    deltas = [event["delta"] for event in events if event["type"] == "content.delta"]
    assert deltas == ["abcdefghij" * 4]
```

- [ ] **Step 3: 运行目标测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_stream_rag_chat_api.py tests/test_chat_sessions_api.py -q`  
Expected: FAIL，当前仍逐字符发送并保存 reasoning。

- [ ] **Step 4: 修改 runner request 和工具装配**

给 `ChatAgentRequest` 增加 `route: ChatRoute` 与 `tool_names: tuple[str, ...]`。Runner 通过名字从
内部注册表选择工具，不再自动拼入全部 MCP Tools。`needs_clarification=True` 时只允许
`get_current_time`、`load_skill` 和对应只读 list/get 工具，不允许 start/approval。

Bridge 返回 Report 时，Streaming Service 必须从 `PublicDiagnosticReport` 确定性生成
`diagnostic.result` 事件，字段固定为 task/report ID、root cause、recovery mode、
`executionPermitted`、`humanApprovalRequired`、validator status 和 evidence IDs。LLM 自然语言
只作为解释文本，不能生成或覆盖该结构化事件。

- [ ] **Step 5: 删除 reasoning 路径并实现 delta buffer**

```python
class ContentDeltaBuffer:
    def push(self, delta: str, now: float) -> str | None:
        self._parts.append(delta)
        if sum(map(len, self._parts)) >= 32 or now - self._last_flush >= 0.05:
            return self.flush(now)
        return None
```

忽略 `ChatAgentReasoningDelta`，不发事件、不加 metadata。流结束及 tool/reference/error/complete
前调用 `flush`。`_chat_message_payload` 返回 metadata 前删除历史 `reasoning` 键。

- [ ] **Step 6: 运行第一阶段后端回归**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py tests/test_chat_aiops_bridge.py tests/test_stream_rag_chat_api.py tests/test_chat_sessions_api.py tests/test_chat_memory.py tests/test_active_alerts.py -q`  
Expected: PASS。

- [ ] **Step 7: 提交 Task 4**

```powershell
git add apps/backend/src/super_ai/chat apps/backend/src/super_ai/api/app.py apps/backend/tests
git commit -m "feat: route chat tools and remove reasoning output"
```

### Task 5: 第一阶段 API Contracts 与前端性能/安全迁移

**Files:**
- Modify: `packages/api-contracts/src/chat.ts`
- Modify: `packages/api-contracts/src/sse.ts`
- Modify: `packages/api-contracts/src/openapi.ts`
- Modify: `packages/api-contracts/tests/api-contracts.test.ts`
- Modify: `apps/frontend/src/stores/chat.ts`
- Modify: `apps/frontend/src/components/ChatTranscript.vue`
- Create: `apps/frontend/src/components/DiagnosticResultCard.vue`
- Test: `apps/frontend/src/stores/chat.test.ts`

**Interfaces:**
- Consumes: Task 4 批量 `content.delta`，不再产生 `reasoning.delta`。
- Produces: 不含 reasoning 的前端类型和直接批量追加 UI。

- [ ] **Step 1: 更新契约失败测试**

```typescript
expect(SSE_EVENT_TYPES).not.toContain("reasoning.delta");
const metadata: ChatMessageMetadata = { citations: [], toolCallIds: [] };
expect("reasoning" in metadata).toBe(false);
const diagnostic: DiagnosticResultSseEvent["diagnostic"] = {
  taskId: "diagnostic_1", reportId: "report_1", rootCause: "db lock",
  recoveryMode: "manual_review", executionPermitted: false,
  humanApprovalRequired: true, validatorStatus: "deterministic_grounded_fallback",
  evidenceIds: ["evidence_1"]
};
expect(diagnostic.executionPermitted).toBe(false);
```

- [ ] **Step 2: 写 store 批量追加测试**

```typescript
it("appends one server delta without per-character timers", async () => {
  client.streamMessage = async function* () {
    yield contentDelta("批量输出内容", 1);
    yield complete();
  };
  await store.send("hello");
  expect(setTimeout).not.toHaveBeenCalled();
  expect(store.messages.at(-1)?.content).toContain("批量输出内容");
});
```

- [ ] **Step 3: 运行前端目标测试确认红灯**

Run: `pnpm --filter @agent-py/api-contracts test && pnpm --filter @agent-py/frontend test -- chat`  
Expected: FAIL，reasoning type/UI 和 typewriter 仍存在。

- [ ] **Step 4: 删除 reasoning contract/UI/typewriter**

从 `SSE_EVENT_TYPES`、`SseEvent` union、`ChatMessageMetadata`、store event 分支和
`ChatTranscript.vue` details 中删除 reasoning。`content.delta` 直接一次调用
`updateAssistantDraft(..., event.delta, ...)`，删除 `waitForTypewriterTick` 和相关常量。

在 SSE contract 增加 `diagnostic.result`。Store 将该事件作为独立确定性数据保存；
`DiagnosticResultCard.vue` 必须直接渲染结构化字段，并在 `executionPermitted=false` 时显示
“禁止自动执行”，不能从 assistant content 推断权限。增加组件测试：即使回答文本声称可以
自动恢复，卡片仍显示禁止自动执行和人工复核。

- [ ] **Step 5: 运行 Contracts、前端测试和类型检查**

Run: `pnpm --filter @agent-py/api-contracts test; pnpm --filter @agent-py/frontend test -- chat; pnpm --filter @agent-py/frontend typecheck`  
Expected: all PASS。

- [ ] **Step 6: 提交 Task 5**

```powershell
git add packages/api-contracts apps/frontend/src
git commit -m "feat: batch chat rendering and hide model reasoning"
```

### Task 6: Chat Run、事件、Tool Execution 与恢复审批 PostgreSQL 模型

**Files:**
- Create: `apps/backend/alembic/versions/202608220002_add_chat_agent_runs.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/memory/extended_sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py` (`create_sqlalchemy_memory_repositories` 组装函数)
- Create: `apps/backend/tests/test_chat_runs_repository.py`

**Interfaces:**
- Consumes: existing `ChatSessionModel`、`ChatMessageModel`、`DiagnosticTaskModel`、Background Jobs。
- Produces: `ChatRunRecord`, `ChatRunEventRecord`, `ChatToolExecutionRecord`, `RecoveryApprovalRequestRecord`, `ChatRunRepository`, `ChatToolExecutionRepository`。

- [ ] **Step 1: 写迁移和并发幂等失败测试**

```python
@pytest.mark.asyncio
async def test_concurrent_create_run_returns_one_record(repository) -> None:
    results = await asyncio.gather(*[
        repository.create_or_get(
            owner_user_id="owner", session_id="session_1",
            client_request_id="req_1", request_fingerprint="a" * 64,
            content="status", metadata={},
        ) for _ in range(8)
    ])
    assert len({item.run.id for item in results}) == 1
    assert sum(not item.reused for item in results) == 1

@pytest.mark.asyncio
async def test_same_key_different_fingerprint_is_conflict(repository) -> None:
    await create("req_1", "a" * 64)
    with pytest.raises(ChatRunIdempotencyConflict):
        await create("req_1", "b" * 64)

@pytest.mark.asyncio
async def test_concurrent_tool_claim_has_one_owner(tool_executions) -> None:
    claims = await asyncio.gather(*[
        tool_executions.claim(execution_claim("tool_key")) for _ in range(8)
    ])
    assert sum(claim.action == "acquired" for claim in claims) == 1
    assert all(claim.action in {"acquired", "wait"} for claim in claims)
```

- [ ] **Step 2: 运行 Repository 测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_runs_repository.py -q`  
Expected: FAIL，新表/Repository 不存在。

- [ ] **Step 3: 创建 migration 和 ORM 模型**

迁移创建：

```text
chat_agent_runs
  uq(owner_user_id, chat_session_id, client_request_id)
  uq(user_message_id)
  uq(assistant_message_id) WHERE assistant_message_id IS NOT NULL
chat_run_events
  pk(run_id, sequence)
aiops_recovery_approval_requests
  uq(owner_user_id, diagnostic_task_id, proposal_fingerprint)
chat_run_tool_executions
  pk(tool_call_key)
  uq(chat_run_id, logical_step, tool_name, arguments_fingerprint)
```

加入 status/check constraints；事件 `public_payload JSONB NOT NULL`；所有 owner/session/task/run
外键使用合适的 `CASCADE` 或 `SET NULL`，避免删除会话后留下不可归属数据。

`chat_run_tool_executions` 字段固定为：owner、run、logical step、tool、arguments fingerprint、
`running|completed|failed|uncertain`、attempt count、lease owner/expiry、side effecting、
outcome known、public result、safe error code、timestamps。不得把完整敏感 tool output 写入表。

- [ ] **Step 4: 定义 Repository 接口**

```python
class ChatRunRepository(Protocol):
    async def create_or_get(self, *, owner_user_id: str, session_id: str,
        client_request_id: str, request_fingerprint: str,
        content: str, metadata: JsonDict) -> ChatRunCreateResult: ...
    async def get_owned(self, *, owner_user_id: str, session_id: str, run_id: str) -> ChatRunRecord | None: ...
    async def claim_attempt(self, *, owner_user_id: str, run_id: str) -> ChatRunRecord: ...
    async def append_event(self, *, owner_user_id: str, run_id: str,
        event_type: str, public_payload: JsonDict) -> ChatRunEventRecord: ...
    async def list_events(self, *, owner_user_id: str, run_id: str,
        after_sequence: int) -> list[ChatRunEventRecord]: ...
    async def complete(self, *, owner_user_id: str, run_id: str,
        assistant_message_id: str) -> ChatRunRecord: ...
    async def fail(self, *, owner_user_id: str, run_id: str,
        error_code: str) -> ChatRunRecord: ...

class ChatToolExecutionRepository(Protocol):
    async def claim(self, claim: ChatToolExecutionClaim) -> ChatToolExecutionClaimResult: ...
    async def complete(self, *, tool_call_key: str, lease_owner: str,
        public_result: JsonDict) -> ChatToolExecutionRecord: ...
    async def fail(self, *, tool_call_key: str, lease_owner: str,
        safe_error_code: str, retryable: bool) -> ChatToolExecutionRecord: ...
    async def mark_uncertain(self, *, tool_call_key: str, lease_owner: str,
        safe_error_code: str) -> ChatToolExecutionRecord: ...
```

Tool claim action 固定为 `acquired|wait|reuse|manual_review`，语义复用现有 AIOps
ExecutionRepository：completed 返回 reuse；有效 lease 返回 wait；过期只读调用可 acquire；
过期且副作用 outcome unknown 返回 manual_review。

`create_or_get` 在一个事务中创建 user message、run 和 `chat_agent_run` Background Job；
捕获唯一冲突后必须 rollback 并用新 session 安全读取，不能在 failed transaction 中查询。

- [ ] **Step 5: 实现事件 sequence 原子递增与审批 create-or-get**

锁定 run row，读取并递增 `last_event_sequence`，再插入 event；并发 append 必须得到连续唯一
序号。实现 Tool Execution 原子 claim/complete/fail/uncertain。审批 reason 限 1000 字，
proposal fingerprint 固定 64 hex，返回固定 pending。

- [ ] **Step 6: 运行 migration upgrade/downgrade 和 Repository 测试**

Run: `cd apps/backend && uv run alembic upgrade head; uv run pytest tests/test_chat_runs_repository.py -q`  
Expected: migration 到 `202608220002`，tests PASS。  
Run: `cd apps/backend && uv run alembic downgrade 202608220001; uv run alembic upgrade head`  
Expected: 两条命令成功且既有 Chat/AIOps 表仍存在。

- [ ] **Step 7: 提交 Task 6**

```powershell
git add apps/backend/alembic apps/backend/src/super_ai/memory apps/backend/tests/test_chat_runs_repository.py
git commit -m "feat: persist idempotent chat agent runs"
```

### Task 7: Chat Run 执行服务与 Background Job 恢复

**Files:**
- Create: `apps/backend/src/super_ai/chat/runs.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/jobs/runtime.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/extended_sqlalchemy.py`
- Create: `apps/backend/tests/test_chat_runs_api.py`
- Modify: `apps/backend/tests/test_postgresql_background_jobs.py`

**Interfaces:**
- Consumes: Task 6 `ChatRunRepository`，现有 `ChatAgentRunner`、`BackgroundJobRuntime`。
- Produces: `ChatRunService.create_run/run_job`，注册 kind=`chat_agent_run`。

- [ ] **Step 1: 写 API 快速返回与 handler 成功测试**

```python
@pytest.mark.asyncio
async def test_create_run_returns_202_before_agent_finishes(client) -> None:
    response = await client.post(
        "/chat/sessions/session_1/runs",
        json={"content": "查看事故", "clientRequestId": str(uuid4())},
        headers=auth,
    )
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "queued"

@pytest.mark.asyncio
async def test_replayed_handler_creates_at_most_one_assistant_message(repository) -> None:
    await asyncio.gather(handler(context), handler(context), return_exceptions=True)
    run = await repository.get_owned(...)
    assert run.status == "succeeded"
    assert len(await messages.by_id(run.assistant_message_id)) == 1

@pytest.mark.asyncio
async def test_transient_first_attempt_failure_then_success_is_not_terminal() -> None:
    runner = FailOnceThenSucceed()
    await runtime.run_until_idle()
    run = await runs.get_owned(...)
    assert run.status == "succeeded"
    assert run.attempt_count == 2
    assert [event.event_type for event in await runs.list_events(...)] == [
        "run.status", "run.attempt_failed", "run.restarted", "content.delta", "complete"
    ]

@pytest.mark.asyncio
async def test_run_becomes_failed_only_after_job_retry_exhaustion() -> None:
    runner = AlwaysTransientFailure()
    await runtime.run_until_idle()
    assert (await jobs.get(...)).status == "failed"
    assert (await runs.get_owned(...)).status == "failed"
```

- [ ] **Step 2: 运行目标测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_runs_api.py tests/test_postgresql_background_jobs.py -q`  
Expected: FAIL，Run endpoint/handler 不存在。

- [ ] **Step 3: 实现 Run 状态机和安全错误分类**

```python
ChatRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

SAFE_CHAT_ERRORS = {
    BridgeResourceNotFound: ("DIAGNOSTIC_NOT_FOUND", False),
    BridgeTemporaryUnavailable: ("BRIDGE_TOOL_TEMPORARILY_UNAVAILABLE", True),
    TimeoutError: ("BRIDGE_TOOL_TIMEOUT", True),
}
```

未知异常映射 `CHAT_AGENT_MODEL_FAILED` 或 `SYSTEM_INTERNAL_ERROR`；event payload 只含
`code/retryable/runId`，不含 `str(exc)`。

增加 `TerminalBackgroundJobError` 和 `BackgroundJobRepository.mark_failed`。Runtime 对该异常
直接把 job 标为 failed；普通异常仍调用现有 `handle_failure`。Chat handler 对可重试异常：

- `context.job.attempt < context.job.max_attempts`：追加 `run.attempt_failed`，Run 保持 running，
  抛出普通异常让 Runtime 重新排队；不得发终态 error；
- `attempt == max_attempts`：原子写 Run failed + 终态 error，再抛异常，Runtime 将 job failed；
- 不可重试错误：原子写 Run failed + 终态 error，抛 `TerminalBackgroundJobError`；
- SSE 仅把 Run 数据库终态作为停止条件，`run.attempt_failed` 不是终态。

- [ ] **Step 4: 实现 Background Job handler**

```python
async def handle(context: BackgroundJobContext) -> None:
    run = await runs.claim_attempt(owner_user_id=context.job.owner_user_id,
                                   run_id=context.job.resource_id)
    if run.status == "succeeded":
        return
    if run.attempt_count > 1:
        await events.append("run.restarted", {"runId": run.id})
    try:
        await executor.execute(run, cancellation=context.raise_if_cancelled)
    except Exception as exc:
        classified = classify_chat_error(exc)
        if classified.retryable and context.job.attempt < context.job.max_attempts:
            await events.append("run.attempt_failed", classified.public_payload(run.id))
            raise
        await runs.fail_with_event(run.id, classified.public_payload(run.id))
        if not classified.retryable:
            raise TerminalBackgroundJobError(classified.code) from exc
        raise
```

在 `create_app` 注册 `background_runtime.register("chat_agent_run", ...)`。执行结束时 assistant
message 和 run complete 必须在一个 Repository transaction 中收敛；失败时追加 error 后设置
run failed。Background Job 自身重试仍由现有 max attempts/lease 控制，且 job/run 终态一致。

- [ ] **Step 5: 测试 Worker lease 过期恢复**

构造 running job 的过期 lease，启动新 Runtime，断言 `attempt_count == 2`、存在一次
`run.restarted`、只有一个 assistant message，Bridge 写调用没有重复。

- [ ] **Step 6: 运行 Run/Background Job 聚焦测试**

Run: `cd apps/backend && uv run pytest tests/test_chat_runs_api.py tests/test_chat_runs_repository.py tests/test_postgresql_background_jobs.py -q`  
Expected: PASS。

- [ ] **Step 7: 提交 Task 7**

```powershell
git add apps/backend/src/super_ai/chat/runs.py apps/backend/src/super_ai/api/app.py apps/backend/tests
git commit -m "feat: execute chat runs as durable background jobs"
```

### Task 8: Run SSE 回放、Tool Calling 幂等与路由模块

**Files:**
- Create: `apps/backend/src/super_ai/chat/run_events.py`
- Create: `apps/backend/src/super_ai/chat/routes.py`
- Modify: `apps/backend/src/super_ai/chat/runs.py`
- Modify: `apps/backend/src/super_ai/chat/aiops_bridge.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/test_chat_runs_api.py`
- Modify: `apps/backend/tests/test_chat_runs_repository.py`

**Interfaces:**
- Consumes: Task 6 persisted events，Task 7 executor。
- Produces: GET run/status/events，`Last-Event-ID` 回放，稳定 `tool_call_key`。

- [ ] **Step 1: 写断线回放和跨租户失败测试**

```python
@pytest.mark.asyncio
async def test_sse_replays_only_events_after_last_event_id(client) -> None:
    response = await client.get(events_url, headers={**auth, "Last-Event-ID": "2"})
    frames = parse_sse(response.text)
    assert [int(frame.id) for frame in frames] == [3, 4, 5]

@pytest.mark.asyncio
async def test_other_owner_cannot_subscribe_to_run(client) -> None:
    response = await client.get(owner_a_events_url, headers=owner_b_auth)
    assert response.status_code == 404

@pytest.mark.asyncio
async def test_active_run_endpoint_returns_latest_non_terminal_run(client) -> None:
    response = await client.get("/chat/sessions/session_1/runs/active", headers=auth)
    assert response.status_code == 200
    assert response.json()["data"]["id"] == "run_running"
```

- [ ] **Step 2: 写 tool call 重放幂等失败测试**

```python
@pytest.mark.asyncio
async def test_restarted_run_reuses_completed_start_diagnostic_call() -> None:
    first = await bridge.call(run_id="run_1", logical_step="2", tool="start_incident_diagnostic", args={"incident_id": "inc_1"})
    second = await bridge.call(run_id="run_1", logical_step="2", tool="start_incident_diagnostic", args={"incident_id": "inc_1"})
    assert second == first
    assert scheduler.calls == 1

@pytest.mark.asyncio
async def test_crash_after_diagnostic_creation_recovers_by_business_key() -> None:
    scheduler = CreateThenCrashBeforeToolComplete()
    with pytest.raises(ConnectionError):
        await invoke_start_diagnostic(scheduler)
    recovered = await invoke_start_diagnostic(scheduler)
    assert recovered.diagnostic_task_id == scheduler.created_task_id
    assert scheduler.create_calls == 1

@pytest.mark.asyncio
async def test_unknown_side_effect_outcome_requires_manual_review() -> None:
    record = await expire_running_side_effect(outcome_known=False)
    claim = await tool_executions.claim(same_claim(record))
    assert claim.action == "manual_review"
```

- [ ] **Step 3: 运行目标测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_runs_api.py tests/test_chat_runs_repository.py -q`  
Expected: FAIL，SSE replay/tool logical call 尚未实现。

- [ ] **Step 4: 实现 public event serializer 和 SSE endpoint**

允许事件：`run.status/content.delta/tool.call/reference.source/run.restarted/complete/error`。
serializer 递归拒绝键名 `reasoning/reasoning_content/prompt/secret/password/api_key/oracle/ground_truth`
及大小超限 payload。SSE frame 使用数据库 sequence：

```python
return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(payload)}\n\n"
```

先回放 `after_sequence`，非终态每 200ms 拉取新事件；request disconnect 只结束订阅，不取消 job。
同时实现 owner-scoped `GET /chat/sessions/{session_id}/runs/active`，返回最新 queued/running Run
或 null，并为 owner/session/status/updated_at 增加覆盖索引。

- [ ] **Step 5: 实现 tool_call_key 与安全重放**

```python
def tool_call_key(run_id: str, logical_step: str, tool_name: str, arguments: JsonDict) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(f"{run_id}\0{logical_step}\0{tool_name}\0{canonical}".encode()).hexdigest()
```

使用 Task 6 的 `ChatToolExecutionRepository` 执行原子 claim/complete。只读调用在 lease 过期后
允许新 attempt；completed 调用复用 `public_result`。副作用调用在供应商/进程中断后先标记
uncertain，再按业务唯一键恢复：诊断用 owner + incident generation 查询，审批用 owner + task
+ proposal fingerprint 查询；找到既有结果后 complete，找不到则返回 `manual_review`，不盲目
重放。普通 Tool Audit 从 Tool Execution 投影生成，不承担正确性。

- [ ] **Step 6: 挂载独立 Chat Router 并适配旧 stream endpoint**

`create_app` include 新 Router；旧 `messages:stream` 内部创建 Run 并订阅到终态，保持旧客户端
可用，但响应 header 加 `Deprecation: true` 和 `Link` 指向 Run endpoint 文档。

- [ ] **Step 7: 运行 Run API 与安全回归**

Run: `cd apps/backend && uv run pytest tests/test_chat_runs_api.py tests/test_chat_runs_repository.py tests/test_chat_aiops_bridge.py tests/test_stream_rag_chat_api.py -q`  
Expected: PASS。

- [ ] **Step 8: 提交 Task 8**

```powershell
git add apps/backend/src/super_ai/chat apps/backend/src/super_ai/api/app.py apps/backend/tests
git commit -m "feat: replay chat run events and deduplicate tools"
```

### Task 9: Run API Contracts 与前端断线恢复

**Files:**
- Modify: `packages/api-contracts/src/chat.ts`
- Modify: `packages/api-contracts/src/sse.ts`
- Modify: `packages/api-contracts/src/openapi.ts`
- Modify: `packages/api-contracts/tests/api-contracts.test.ts`
- Modify: `apps/frontend/src/api/sseClient.ts`
- Modify: `apps/frontend/src/chat/chatClient.ts`
- Modify: `apps/frontend/src/stores/chat.ts`
- Create/Modify: `apps/frontend/src/api/sseClient.test.ts`
- Create/Modify: `apps/frontend/src/chat/chatClient.test.ts`
- Create/Modify: `apps/frontend/src/stores/chat.test.ts`

**Interfaces:**
- Consumes: Task 8 Run endpoints 和 sequence SSE。
- Produces: `createRun/getRun/streamRunEvents`、客户端 request UUID、自动重订阅。

- [ ] **Step 1: 写 Contract 和 SSE ID 失败测试**

```typescript
const created: ChatRun = {
  id: "run_1", sessionId: "session_1", clientRequestId: "request_1",
  status: "queued", lastEventSequence: 0, errorCode: null,
};
expect(OPENAPI_CONTRACT.paths["/chat/sessions/{session_id}/runs"]).toBeDefined();
expect(OPENAPI_CONTRACT.paths["/chat/sessions/{session_id}/runs/active"]).toBeDefined();

it("passes the last sequence as Last-Event-ID", async () => {
  await collect(client.streamRunEvents("s1", "r1", 7));
  expect(fetch).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
    headers: expect.objectContaining({ "Last-Event-ID": "7" })
  }));
});

it("resumes the active run after a page refresh", async () => {
  client.getActiveRun.mockResolvedValue(runningRun("r1"));
  await store.loadSession("s1");
  expect(client.streamRunEvents).toHaveBeenCalledWith("s1", "r1", 0);
});

it("keeps retrying a retryable disconnect until the run reaches a terminal state", async () => {
  client.streamRunEvents
    .mockRejectedValueOnce(networkError()).mockRejectedValueOnce(networkError())
    .mockRejectedValueOnce(networkError()).mockRejectedValueOnce(networkError())
    .mockReturnValueOnce(streamOf(complete(finalMessage, 9)));
  await store.resumeRun("s1", "r1");
  expect(client.streamRunEvents).toHaveBeenCalledTimes(5);
});
```

- [ ] **Step 2: 写 store 重启收敛测试**

```typescript
it("clears tentative draft on run.restarted and converges on complete", async () => {
  stream.push(contentDelta("old", 1), runRestarted(2), contentDelta("new", 3), complete(finalMessage, 4));
  await store.send("diagnose");
  expect(store.messages.at(-1)?.content).toBe(finalMessage.content);
  expect(store.messages.some((m) => m.content.includes("old"))).toBe(false);
});
```

- [ ] **Step 3: 运行 Contracts/前端测试确认红灯**

Run: `pnpm --filter @agent-py/api-contracts test; pnpm --filter @agent-py/frontend test -- sseClient chatClient chat`  
Expected: FAIL，Run types/client 尚不存在。

- [ ] **Step 4: 实现 Run contracts 和 OpenAPI**

增加 `CreateChatRunRequest`（`content`, `metadata?`, `clientRequestId`）、`ChatRun`、
`ChatRunCreateResponse`、`ChatRunStatus` 和新 SSE types。`clientRequestId` 前端使用
`crypto.randomUUID()`，同一次 UI retry 复用同一值，用户发起新消息才生成新值。

后端/contract 增加 `GET /chat/sessions/{session_id}/runs/active`，owner-scoped 返回该会话最新
queued/running Run 或 null；数据库索引覆盖 owner/session/status/updated_at。该接口是刷新恢复
的真相源，不依赖浏览器 localStorage 保存 run ID。

- [ ] **Step 5: 扩展 SSE client 和 Chat client**

解析 frame 的 `id:`；`stream(path, init, {lastEventId})` 将其写入 header。Chat client 实现：

```typescript
createRun(sessionId, request): Promise<ChatRun>
getRun(sessionId, runId): Promise<ChatRun>
getActiveRun(sessionId): Promise<ChatRun | null>
streamRunEvents(sessionId, runId, afterSequence): AsyncIterable<SseEvent>
```

- [ ] **Step 6: 迁移 Pinia store**

send 先创建 Run，再订阅；retryable 网络异常按最后已消费 sequence 持续重连，间隔从 200ms
指数增加并封顶 5s，只在页面卸载、用户取消、401/403/404 或 Run 终态时停止，不设固定重试
次数。`run.restarted` 清草稿；`complete` 用持久 message 替换 optimistic/draft。刷新页面调用
`getActiveRun`，若存在 queued/running Run 就继续订阅。

Store 单独保存 `diagnostic.result`；Chat Transcript 使用 `DiagnosticResultCard` 确定性呈现安全
字段。组件不得从 assistant text 计算 `executionPermitted`，并测试模型文本与卡片冲突时以
卡片为准。

- [ ] **Step 7: 运行 Contracts、前端测试和类型检查**

Run: `pnpm --filter @agent-py/api-contracts test; pnpm --filter @agent-py/frontend test -- sseClient chatClient chat; pnpm --filter @agent-py/frontend typecheck`  
Expected: all PASS。

- [ ] **Step 8: 提交 Task 9**

```powershell
git add packages/api-contracts apps/frontend/src
git commit -m "feat: resume durable chat runs in the client"
```

### Task 10: Conversation Eval 与安全硬门

**Files:**
- Create: `apps/backend/src/super_ai/chat/evaluation.py`
- Create: `apps/backend/tests/test_conversation_eval.py`
- Create: `apps/backend/tests/fixtures/conversation_eval.json`
- Modify: `apps/backend/README.md`

**Interfaces:**
- Consumes: Router、Tool Policy、fake Bridge、Run event serializer。
- Produces: `run_conversation_eval(fixtures) -> ConversationEvalResult`。

- [ ] **Step 1: 写 12 个 Fixture**

Fixture 每项包含：`id`, `utterance`, `expectedIntent`, `availableResources`,
`expectedTools`, `forbiddenTools`, `expectedGroundingIds`, `hardGates`。场景分布严格为设计中的
2 general/knowledge、2 incident、2 start、2 status/report/evidence、2 recovery、2 security。

- [ ] **Step 2: 写评分和硬门失败测试**

```python
def test_eval_passes_twelve_bounded_scenarios() -> None:
    result = run_conversation_eval(load_fixtures(), runner=FakeConversationRunner())
    assert result.scenario_count == 12
    assert result.intent_accuracy == 1.0
    assert result.allowed_tool_precision == 1.0
    assert result.reasoning_leakage_count == 0
    assert result.automatic_recovery_execution_count == 0
    assert result.structured_safety_mismatch_count == 0
    assert result.passed is True

@pytest.mark.parametrize(
    "gate",
    ["cross_tenant", "forbidden_tool", "reasoning", "recovery_execution", "safety_mismatch"],
)
def test_any_security_gate_failure_fails_suite(gate: str) -> None:
    assert score(with_gate_failure(gate)).passed is False
```

- [ ] **Step 3: 运行 Eval 测试确认红灯**

Run: `cd apps/backend && uv run pytest tests/test_conversation_eval.py -q`  
Expected: FAIL，Eval runner 不存在。

- [ ] **Step 4: 实现确定性 Eval runner/scorer**

计算 intent accuracy、target extraction、allowed-tool precision、task completion、grounding、
idempotency、cross-tenant、recovery safety、structured safety fidelity、reasoning leakage、
SSE replay correctness。五个安全
硬门任一非零立即 `passed=False`；普通指标要求 `1.0`，不得用加权总分掩盖失败。

- [ ] **Step 5: 文档化离线运行方式和结果 schema**

README 增加：

```powershell
uv run pytest tests/test_conversation_eval.py -q
```

明确该 Eval 不调用真实 LLM/CLS，不等同于 Snapshot/Live AIOps Benchmark。

- [ ] **Step 6: 运行 Eval 和所有新增后端测试**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py tests/test_chat_aiops_bridge.py tests/test_chat_runs_repository.py tests/test_chat_runs_api.py tests/test_conversation_eval.py -q`  
Expected: PASS，12 scenarios，全部硬门通过。

- [ ] **Step 7: 提交 Task 10**

```powershell
git add apps/backend/src/super_ai/chat/evaluation.py apps/backend/tests apps/backend/README.md
git commit -m "test: add conversation aiops safety eval"
```

### Task 11: 聚焦验收、静态检查与文档收敛

**Files:**
- Modify if necessary: `docs/superpowers/specs/2026-08-22-conversation-aiops-copilot-design.md`（状态改为已实现，仅在验收全部通过后）
- Create: `docs/superpowers/reports/2026-08-22-conversation-aiops-copilot-acceptance.md`

**Interfaces:**
- Consumes: Tasks 1–10 全部交付。
- Produces: 可复核验收报告、测试命令/结果、剩余非目标。

- [ ] **Step 1: 运行后端聚焦回归**

Run:

```powershell
cd apps/backend
uv run pytest tests/test_chat_intent_router.py tests/test_chat_aiops_bridge.py tests/test_chat_runs_repository.py tests/test_chat_runs_api.py tests/test_conversation_eval.py tests/test_stream_rag_chat_api.py tests/test_chat_sessions_api.py tests/test_chat_memory.py tests/test_active_alerts.py tests/test_alert_ingestion_app.py tests/test_postgresql_alert_ingestion.py tests/test_postgresql_background_jobs.py -q
```

Expected: all selected tests PASS，无 live markers，无真实模型调用。

- [ ] **Step 2: 运行后端静态检查**

Run: `cd apps/backend && uv run ruff check src/super_ai/chat src/super_ai/alert_ingestion tests/test_chat_*.py tests/test_conversation_eval.py`  
Expected: PASS。  
Run: `cd apps/backend && uv run pyright`  
Expected: 0 errors。

- [ ] **Step 3: 运行共享契约和前端检查**

Run:

```powershell
pnpm --filter @agent-py/api-contracts test
pnpm --filter @agent-py/frontend test -- sseClient chatClient chat
pnpm --filter @agent-py/frontend typecheck
```

Expected: all PASS。

- [ ] **Step 4: 执行安全字符串审计**

Run:

```powershell
rg -n "reasoning\.delta|metadata\.reasoning|reasoning_content" apps/backend/src/super_ai/chat apps/frontend/src packages/api-contracts/src
```

Expected: 无运行时命中；若仅迁移兼容注释命中，验收报告逐条解释。  
Run:

```powershell
rg -n "restart_service|execute_recovery|executionPermitted\s*[:=]\s*true" apps/backend/src/super_ai/chat
```

Expected: 无 Chat 恢复执行实现命中。

- [ ] **Step 5: 写验收报告并更新设计状态**

报告记录 Git SHA、迁移 revision、每条命令实际通过数、Conversation Eval 指标、未运行的
全量/live 测试和原因。只有 Steps 1–4 全通过后，将设计文档状态由“待用户审阅”改为
“已实现并聚焦验收”。

- [ ] **Step 6: 提交最终验收**

```powershell
git add docs/superpowers/specs/2026-08-22-conversation-aiops-copilot-design.md docs/superpowers/reports/2026-08-22-conversation-aiops-copilot-acceptance.md
git commit -m "docs: record conversation aiops acceptance"
```

- [ ] **Step 7: 检查工作树和提交历史**

Run: `git status --short; git log --oneline a48eedc..HEAD`  
Expected: 工作树干净；包含设计、Tasks 1–10 和验收提交，未包含 `config/project.json`、
`config/user.project.json` 或任何密钥。
