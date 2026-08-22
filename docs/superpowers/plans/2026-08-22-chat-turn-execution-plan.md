# Chat Turn Execution 与持久确认 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Conversation Agent 增加 Direct Read、持久确认和有预算 ReAct，并用 required postcondition 判断真实任务完成。

**Architecture:** `ChatTurnExecutionService` 统一组合 Router、Execution Policy、ToolCatalog、owner-scoped Bridge 和 LangChain Agent。明确只读目标直接调用 Bridge；新写入创建 PostgreSQL Pending Chat Action；模糊请求才进入受 LangChain middleware 与项目 deadline 约束的 ReAct。

**Tech Stack:** Python 3.10、FastAPI、Pydantic 2、LangChain 1.3.12、SQLAlchemy 2、asyncpg、PostgreSQL 16、Vue 3、Pinia、TypeScript、Vitest、pytest。

## Global Constraints

- 不新增依赖；直接采用现有 `ModelCallLimitMiddleware` 和 `ToolCallLimitMiddleware`。
- Chat 不直接调用 CLS、诊断探针、恢复工具或绕过 AIOps Validator/Policy Gate。
- 新诊断和新恢复审批必须经 owner-scoped Pending Chat Action 确认；复用结果无需确认。
- LLM Explanation 可降级，不能改写结构化结果或安全字段。
- PostgreSQL 是确认、幂等和恢复的真相源；Redis 不是。
- 不保存或返回 reasoning、Prompt、凭据、原始异常或无界工具输出。
- 使用 TDD 和聚焦测试；不运行全量 pytest、真实 LLM、CLS 或 Docker Live。
- Migration revision 固定为 `202608220003`，down revision 为 `202608220002`。
- 配置继续来自 `config/project.json` 与 `config/user.project.json`，模板不得包含密钥。

## Reuse Assessment

- **Direct adoption:** LangChain `ModelCallLimitMiddleware`、`ToolCallLimitMiddleware`；现有 Bridge、Chat Run、Background Job、SSE replay。
- **Wrapped adoption:** `create_agent(model=model, tools=tools, middleware=middleware)` 和现有 Tool Execution fingerprint。
- **Reference only:** LangChain HITL、PydanticAI deferred tools、OpenAI Agents HITL；它们不兼容当前 Durable Chat Run 确认状态。
- **Custom:** Execution Policy、ToolCatalog、Postcondition、Pending Chat Action 和 Direct Read。
- **Dependencies:** 无新增依赖、服务、权限或原生二进制。

---

## File Structure

### Create

- `apps/backend/src/super_ai/chat/execution_policy.py`：执行模式、required capability、postcondition 和预算。
- `apps/backend/src/super_ai/chat/tool_catalog.py`：策略与运行时工具的严格编译。
- `apps/backend/src/super_ai/chat/execution.py`：Direct/confirmation/ReAct 统一执行。
- `apps/backend/src/super_ai/chat/pending_actions.py`：Pending Chat Action 应用逻辑与 fingerprint。
- `apps/backend/alembic/versions/202608220003_add_pending_chat_actions.py`：确认表及约束。
- `apps/backend/tests/test_chat_execution_policy.py`
- `apps/backend/tests/test_chat_turn_execution.py`
- `apps/backend/tests/test_pending_chat_actions.py`
- `apps/frontend/src/components/PendingChatActionCard.vue`
- `apps/frontend/tests/pendingChatActionCard.test.ts`

### Modify

- `apps/backend/src/super_ai/chat/intent.py`
- `apps/backend/src/super_ai/chat/streaming.py`
- `apps/backend/src/super_ai/chat/routes.py`
- `apps/backend/src/super_ai/chat/run_events.py`
- `apps/backend/src/super_ai/chat/evaluation.py`
- `apps/backend/src/super_ai/memory/models.py`
- `apps/backend/src/super_ai/memory/repositories.py`
- `apps/backend/src/super_ai/memory/sqlalchemy.py`
- `apps/backend/src/super_ai/api/app.py`
- `apps/backend/src/super_ai/error_catalog.py`
- `packages/api-contracts/src/chat.ts`
- `packages/api-contracts/src/sse.ts`
- `packages/api-contracts/src/openapi.ts`
- `apps/frontend/src/chat/chatClient.ts`
- `apps/frontend/src/stores/chat.ts`
- `apps/frontend/src/components/ChatTranscript.vue`

### Task 1: 真实联合 Eval 红灯与 Recovery 路由修复

**Files:**
- Modify: `apps/backend/src/super_ai/chat/evaluation.py`
- Modify: `apps/backend/src/super_ai/chat/intent.py`
- Modify: `apps/backend/tests/test_conversation_eval.py`
- Modify: `apps/backend/tests/test_chat_intent_router.py`

**Interfaces:**
- Consumes: `ChatIntentRouter.route(content)`、`allowed_tools_for(intent)`。
- Produces: `IntegratedConversationEvalRunner.evaluate(scenario)`，真实调用 Router/Policy，不读取 expected output 作为 observation。

- [ ] **Step 1: 写显式 Recovery ID 与非自证 Eval 失败测试**

```python
@pytest.mark.asyncio
async def test_explicit_recovery_request_beats_diagnostic_status_rule() -> None:
    route = await ChatIntentRouter(FailIfCalledRouter()).route(
        "为 diagnostic_owner_008 的恢复方案创建人工审批"
    )
    assert route.intent == "recovery_request"
    assert route.diagnostic_task_id == "diagnostic_owner_008"

@pytest.mark.asyncio
async def test_integrated_eval_observes_real_router_failure() -> None:
    result = await run_conversation_eval(
        load_scenarios(),
        runner=IntegratedConversationEvalRunner(
            router=AlwaysGeneralRouter(), bridge=SafeFakeBridge()
        ),
    )
    assert result.intent_accuracy < 1.0
    assert result.passed is False
```

- [ ] **Step 2: 运行红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py tests/test_conversation_eval.py -q`
Expected: recovery 被错误分类，`IntegratedConversationEvalRunner` 尚不存在。

- [ ] **Step 3: 实现规则优先级与异步联合 runner**

```python
_RECOVERY_WORDS = ("恢复", "审批", "approval", "recover", "remediation")

def _route_explicit_identifiers(content: str) -> ChatRoute | None:
    diagnostic = _DIAGNOSTIC_ID.search(content)
    if diagnostic is not None:
        intent: ChatIntent = (
            "recovery_request"
            if any(word in content.casefold() for word in _RECOVERY_WORDS)
            else "diagnostic_status"
        )
        return ChatRoute(intent, 1.0, "rule", diagnostic_task_id=diagnostic.group(1))
    incident = _INCIDENT_ID.search(content)
    if incident is None:
        return None
    intent: ChatIntent = (
        "start_diagnostic"
        if any(word in content.casefold() for word in _START_WORDS)
        else "incident_query"
    )
    return ChatRoute(intent, 1.0, "rule", incident_id=incident.group(1))
```

把 `ConversationEvalRunner.evaluate` 和 `run_conversation_eval` 改为 async；Integrated runner 必须从 Router 实际结果、Policy 实际工具和 fake Bridge 实际资源生成 observation。删除 `SafeFakeConversationRunner` 中复制 expected 字段的路径。

- [ ] **Step 4: 运行绿灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py tests/test_conversation_eval.py -q`
Expected: PASS，且错误 Router 注入能让 suite 失败。

- [ ] **Step 5: 提交**

```powershell
git add apps/backend/src/super_ai/chat/intent.py apps/backend/src/super_ai/chat/evaluation.py apps/backend/tests/test_chat_intent_router.py apps/backend/tests/test_conversation_eval.py
git commit -m "test: integrate conversation routing evaluation"
```

### Task 2: Execution Policy、ToolCatalog 与 Postcondition

**Files:**
- Create: `apps/backend/src/super_ai/chat/execution_policy.py`
- Create: `apps/backend/src/super_ai/chat/tool_catalog.py`
- Create: `apps/backend/tests/test_chat_execution_policy.py`
- Modify: `apps/backend/src/super_ai/chat/tool_policy.py`

**Interfaces:**
- Produces: `ExecutionMode`、`ChatExecutionPolicy`、`policy_for(route)`、`ToolCatalog.compile(policy, registry)`、`CompiledToolCatalog`。

- [ ] **Step 1: 写策略和缺失工具失败测试**

```python
def test_explicit_report_uses_direct_read_with_postcondition() -> None:
    policy = policy_for(ChatRoute("diagnostic_status", 1.0, "rule", diagnostic_task_id="diagnostic_1"))
    assert policy.mode == "direct_read"
    assert policy.required_capability == "diagnostic_report"
    assert policy.postcondition == "diagnostic_result"

def test_missing_required_tool_fails_compilation() -> None:
    with pytest.raises(RequiredToolUnavailable):
        ToolCatalog().compile(
            policy=policy_for(ChatRoute("knowledge_question", 0.9, "model")),
            registry={},
        )
```

- [ ] **Step 2: 运行红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_execution_policy.py -q`
Expected: modules 不存在。

- [ ] **Step 3: 实现不可变策略和严格目录**

```python
ExecutionMode = Literal["direct_read", "confirmation_required", "bounded_react"]

@dataclass(frozen=True, slots=True)
class ChatExecutionBudget:
    max_model_calls: int
    max_tool_calls: int
    deadline_seconds: float

@dataclass(frozen=True, slots=True)
class ChatExecutionPolicy:
    mode: ExecutionMode
    required_capability: str
    allowed_tools: frozenset[str]
    required_tools: frozenset[str]
    postcondition: str
    budget: ChatExecutionBudget

@dataclass(frozen=True, slots=True)
class CompiledToolCatalog:
    tools: tuple[StructuredTool, ...]
    names: tuple[str, ...]
    catalog_version: str
```

Direct Read 不把 Bridge tool 交给模型；confirmation policy 只允许预检；bounded ReAct 的 required tools 缺失必须抛稳定错误。

- [ ] **Step 4: 运行绿灯和原 allowlist 回归**

Run: `cd apps/backend && uv run pytest tests/test_chat_execution_policy.py tests/test_chat_intent_router.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/backend/src/super_ai/chat/execution_policy.py apps/backend/src/super_ai/chat/tool_catalog.py apps/backend/src/super_ai/chat/tool_policy.py apps/backend/tests/test_chat_execution_policy.py
git commit -m "feat: add chat execution policy and tool catalog"
```

### Task 3: 受限 ReAct 与重复调用停止

**Files:**
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Create: `apps/backend/tests/test_chat_react_budget.py`
- Modify: `apps/backend/tests/test_stream_rag_chat_api.py`

**Interfaces:**
- Consumes: `ChatExecutionBudget`、`CompiledToolCatalog`。
- Produces: `build_agent_middleware(budget)`、`RepeatedToolCallMiddleware`、`iterate_with_deadline(events, seconds)`。

- [ ] **Step 1: 写 middleware、重复调用和 deadline 失败测试**

```python
def test_budget_uses_langchain_limit_middleware() -> None:
    middleware = build_agent_middleware(ChatExecutionBudget(2, 2, 120.0))
    assert isinstance(middleware[0], ModelCallLimitMiddleware)
    assert isinstance(middleware[1], ToolCallLimitMiddleware)

@pytest.mark.asyncio
async def test_same_tool_and_arguments_stop_before_second_execution() -> None:
    runner = budgeted_runner(repeating_agent("knowledge_retrieval", {"query": "locks"}))
    with pytest.raises(RepeatedToolCallError):
        await collect(runner.stream(request()))
    assert runner.tool_calls == 1
```

- [ ] **Step 2: 运行红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_react_budget.py -q`
Expected: helpers 不存在。

- [ ] **Step 3: 直接采用官方限制 middleware 并包装 fingerprint guard**

```python
def build_agent_middleware(budget: ChatExecutionBudget) -> tuple[AgentMiddleware, ...]:
    return (
        ModelCallLimitMiddleware(run_limit=budget.max_model_calls, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=budget.max_tool_calls, exit_behavior="error"),
        RepeatedToolCallMiddleware(),
    )

class RepeatedToolCallMiddleware(AgentMiddleware):
    async def awrap_tool_call(self, request: ToolCallRequest, handler: ToolCallWrapper):
        fingerprint = canonical_tool_fingerprint(request.tool_call)
        if fingerprint in self._seen:
            raise RepeatedToolCallError
        self._seen.add(fingerprint)
        return await handler(request)
```

每次 `create_agent` 创建新的 guard instance。用 `await asyncio.wait_for(anext(iterator), remaining)` 实现 Python 3.10 兼容的 async iterator deadline。官方 limit 异常映射为 `CHAT_EXECUTION_BUDGET_EXHAUSTED`。

- [ ] **Step 4: 运行聚焦回归**

Run: `cd apps/backend && uv run pytest tests/test_chat_react_budget.py tests/test_stream_rag_chat_api.py -q`
Expected: PASS；模型/工具超预算、重复调用和 deadline 都产生类型化结果。

- [ ] **Step 5: 提交**

```powershell
git add apps/backend/src/super_ai/chat/streaming.py apps/backend/tests/test_chat_react_budget.py apps/backend/tests/test_stream_rag_chat_api.py
git commit -m "feat: bound chat react execution"
```

### Task 4: Direct Read、结构化结果与可降级 Explanation

**Files:**
- Create: `apps/backend/src/super_ai/chat/execution.py`
- Create: `apps/backend/tests/test_chat_turn_execution.py`
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Modify: `apps/backend/src/super_ai/chat/runs.py`
- Modify: `apps/backend/src/super_ai/chat/run_events.py`

**Interfaces:**
- Produces: `ChatTurnExecutionService.execute(request) -> AsyncIterator[ChatTurnEvent]`、`ChatTurnResult`。

- [ ] **Step 1: 写 Direct Read 与 Explanation 降级失败测试**

```python
@pytest.mark.asyncio
async def test_report_card_precedes_llm_explanation() -> None:
    events = await collect(service(model=SlowFakeModel()).execute(report_request()))
    assert event_types(events)[:2] == ["execution.mode_selected", "structured.result"]
    assert events[1]["diagnostic"]["executionPermitted"] is False

@pytest.mark.asyncio
async def test_model_failure_keeps_structured_success() -> None:
    result = await final_result(service(model=FailingModel()).execute(report_request()))
    assert result.status == "succeeded_with_degraded_explanation"
    assert result.structured_result["executionPermitted"] is False
```

- [ ] **Step 2: 运行红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_turn_execution.py -q`
Expected: execution module 不存在。

- [ ] **Step 3: 实现统一执行与 postcondition validator**

Direct Read 根据 capability 调用 Bridge 的显式方法；先持久化 `structured.result`，再向只接收 allowlisted DTO 的 Explanation model 发起一次调用。Explanation 错误转换成 `explanation.degraded`，Run 最终成功。Postcondition 不满足时必须失败，不能用模型文本补齐。

- [ ] **Step 4: 接入 ChatRunJobHandler 并运行恢复测试**

Run: `cd apps/backend && uv run pytest tests/test_chat_turn_execution.py tests/test_chat_runs_api.py tests/test_chat_runs_repository.py -q`
Expected: PASS；Worker retry 不重复 Direct Read 结构化事件或 assistant message。

- [ ] **Step 5: 提交**

```powershell
git add apps/backend/src/super_ai/chat/execution.py apps/backend/src/super_ai/chat/streaming.py apps/backend/src/super_ai/chat/runs.py apps/backend/src/super_ai/chat/run_events.py apps/backend/tests/test_chat_turn_execution.py apps/backend/tests/test_chat_runs_api.py
git commit -m "feat: add direct chat turn execution"
```

### Task 5: Pending Chat Action PostgreSQL 状态机

**Files:**
- Create: `apps/backend/alembic/versions/202608220003_add_pending_chat_actions.py`
- Create: `apps/backend/src/super_ai/chat/pending_actions.py`
- Create: `apps/backend/src/super_ai/chat/pending_action_jobs.py`
- Create: `apps/backend/tests/test_pending_chat_actions.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/chat/aiops_bridge.py`

**Interfaces:**
- Produces: `PendingChatActionRecord`、`PendingChatActionRepository.create_or_get/get_owned/list_pending/confirm_and_enqueue/cancel/mark_executed/mark_uncertain`、`PendingChatActionService`、job kind `pending_chat_action`。

- [ ] **Step 1: 写迁移、owner、过期和并发确认失败测试**

```python
@pytest.mark.asyncio
async def test_concurrent_confirm_executes_once(migrated_database_url: str) -> None:
    action = await service.preview_start(owner="a", incident_id="incident_1")
    first, second = await asyncio.gather(
        service.confirm(owner="a", action_id=action.id),
        service.confirm(owner="a", action_id=action.id),
    )
    assert first.background_job_id == second.background_job_id
    await run_claimed_jobs_until_terminal()
    assert scheduler.calls == 1

@pytest.mark.asyncio
async def test_confirmed_action_recovers_after_worker_crash() -> None:
    confirmed = await service.confirm(owner="a", action_id="action_1")
    await handler.fail_after_side_effect_once(confirmed.background_job_id)
    await runtime.recover_expired_lease_and_retry(confirmed.background_job_id)
    action = await repository.get_owned(owner_user_id="a", action_id="action_1")
    assert action.status == "executed"
    assert scheduler.calls == 1

@pytest.mark.asyncio
async def test_cross_owner_and_expired_actions_appear_not_found() -> None:
    with pytest.raises(PendingActionNotFound):
        await service.confirm(owner="b", action_id="action_a")
```

- [ ] **Step 2: 运行红灯**

Run: `cd apps/backend && uv run pytest tests/test_pending_chat_actions.py -q`
Expected: migration/model/repository 不存在。

- [ ] **Step 3: 实现表、active partial unique index 和原子确认调度**

表包含设计文档全部字段并增加 `background_job_id`。使用 active partial unique index：

```python
Index(
    "uq_pending_chat_actions_active_fingerprint",
    "owner_user_id", "action_fingerprint",
    unique=True,
    postgresql_where=text("status IN ('pending','confirmed')"),
)
```

`confirm_and_enqueue` 在一个事务中锁定 Action、重新预检、改为 `confirmed`，并插入稳定 ID
`job_chat_action_<action-id>` 的 Background Job。重复确认冲突后读取同一 Action/Job。HTTP
confirm 只返回 confirmed/queued，不在请求内执行副作用。

- [ ] **Step 4: 实现 leased handler 与不确定结果规则**

`PendingChatActionJobHandler` 调用现有幂等 Scheduler/Approval Repository，成功后写
`execution_result_id` 和 `executed`。Worker 在副作用后崩溃时，重试通过稳定 incident/approval
fingerprint 读取同一结果；若不能证明结果则 `manual_review`，不能补执行。

- [ ] **Step 5: 验证 migration、crash recovery 与 repository**

Run: `cd apps/backend && uv run alembic upgrade head && uv run pytest tests/test_pending_chat_actions.py tests/test_chat_aiops_bridge.py -q`
Expected: head 为 `202608220003`，全部 PASS。随后在空测试库执行 downgrade 到 `202608220002` 再 upgrade head。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/alembic/versions/202608220003_add_pending_chat_actions.py apps/backend/src/super_ai/chat/pending_actions.py apps/backend/src/super_ai/chat/pending_action_jobs.py apps/backend/src/super_ai/chat/aiops_bridge.py apps/backend/src/super_ai/memory apps/backend/tests/test_pending_chat_actions.py apps/backend/tests/test_chat_aiops_bridge.py
git commit -m "feat: persist pending chat actions"
```

### Task 6: 确认 API、SSE、前端卡片与聚焦验收

**Files:**
- Modify: `apps/backend/src/super_ai/chat/routes.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/error_catalog.py`
- Modify: `packages/api-contracts/src/chat.ts`
- Modify: `packages/api-contracts/src/sse.ts`
- Modify: `packages/api-contracts/src/openapi.ts`
- Modify: `apps/frontend/src/chat/chatClient.ts`
- Modify: `apps/frontend/src/stores/chat.ts`
- Create: `apps/frontend/src/components/PendingChatActionCard.vue`
- Create: `apps/frontend/tests/pendingChatActionCard.test.ts`
- Modify: `apps/frontend/src/components/ChatTranscript.vue`
- Modify: `apps/backend/tests/test_chat_runs_api.py`

**Interfaces:**
- Produces: confirm/cancel/list-pending endpoints、`confirmation.required/resolved`、`structured.result`、`explanation.degraded` contracts。

- [ ] **Step 1: 写 API/SSE/前端失败测试**

```python
async def test_confirm_is_owner_scoped_idempotent_and_replayed() -> None:
    response = await client.post(f"/chat/actions/{action_id}/confirm", headers=owner)
    duplicate = await client.post(f"/chat/actions/{action_id}/confirm", headers=owner)
    assert response.json()["data"] == duplicate.json()["data"]
    assert (await client.post(f"/chat/actions/{action_id}/confirm", headers=other)).status_code == 404
```

```ts
it("restores a pending action after reload and confirms once", async () => {
  await store.loadSession("session_1");
  expect(store.pendingActions).toHaveLength(1);
  await store.confirmPendingAction("action_1");
  expect(client.confirmAction).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: 运行红灯**

Run: `cd apps/backend && uv run pytest tests/test_chat_runs_api.py -q`
Run: `npm --workspace packages/api-contracts test && npm --workspace apps/frontend test -- pendingChatAction chatStore contracts`
Expected: routes/contracts/components 不存在。

- [ ] **Step 3: 实现 endpoints、事件 allowlist、typed client/store/card**

卡片必须展示动作、目标、15 分钟过期时间和“确认/取消”；不能显示恢复已批准。刷新后 `GET .../actions/pending` 恢复状态。按钮重复点击由 store loading 和 PostgreSQL 幂等共同保护。

- [ ] **Step 4: 运行完整聚焦验收**

Run: `cd apps/backend && uv run pytest tests/test_chat_intent_router.py tests/test_chat_execution_policy.py tests/test_chat_react_budget.py tests/test_chat_turn_execution.py tests/test_pending_chat_actions.py tests/test_chat_runs_api.py tests/test_chat_runs_repository.py tests/test_chat_aiops_bridge.py tests/test_conversation_eval.py tests/test_stream_rag_chat_api.py -q`
Run: `cd apps/backend && uv run ruff check src/super_ai/chat tests/test_chat_*.py tests/test_conversation_eval.py && uv run pyright`
Run: `npm --workspace packages/api-contracts test && npm --workspace packages/api-contracts run typecheck`
Run: `npm --workspace apps/frontend test -- pendingChatAction chatStore chatClient contracts chatComponents && npm --workspace apps/frontend run typecheck`
Expected: 全部 PASS，无 live marker、真实模型或 CLS 调用。

- [ ] **Step 5: 提交**

```powershell
git add apps/backend/src/super_ai/chat apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/error_catalog.py apps/backend/tests packages/api-contracts apps/frontend
git commit -m "feat: confirm durable chat actions"
```
