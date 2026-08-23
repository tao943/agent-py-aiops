# Context Envelope 与 Structured Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用版本化 Structured Memory、最近 6 轮和完整 token 预算替换自由文本全量压缩，并以持久 Job 实现 60% 后台、85% 同步兜底。

**Architecture:** `ContextEnvelopeService` 是模型输入的唯一构建接口，统一预算 system、tools、memory、recent turns、observations 和 output reserve。Structured Memory 持久化在 PostgreSQL，以 summary version CAS 更新；Background Job 负责自适应压缩。

**Tech Stack:** Python 3.10、Pydantic 2、LangChain token utilities、SQLAlchemy 2、PostgreSQL 16、现有 BackgroundJobRuntime、Vue 3、TypeScript、pytest/Vitest。

## Global Constraints

- 依赖上一计划完成的 Chat Turn Execution；Migration revision 为 `202608220004`，down revision 为 `202608220003`。
- 不采用 LangChain `SummarizationMiddleware` 作为真相源；仅参考其 safe cutoff，继续复用现有 token counter。
- Structured Memory 不保存 reasoning、Prompt、凭据、原始工具输出或 AIOps 安全状态。
- 始终保留最近 6 个完整 user/assistant turn；Tool/AI pair 不得被切断。
- 新默认 Memory Mode 为 `adaptive`；旧自动模式兼容一版并规范化为 `adaptive`。
- PostgreSQL CAS 决定压缩正确性；Redis 不参与。
- 不新增依赖，不运行全量或 live 测试。

## File Structure

### Create
- `apps/backend/src/super_ai/chat/context_envelope.py`
- `apps/backend/src/super_ai/chat/structured_memory.py`
- `apps/backend/alembic/versions/202608220004_add_structured_chat_memory.py`
- `apps/backend/tests/test_context_envelope.py`
- `apps/backend/tests/test_structured_chat_memory.py`
- `apps/backend/tests/test_chat_memory_jobs.py`

### Modify
- `apps/backend/src/super_ai/chat/memory.py`
- `apps/backend/src/super_ai/chat/streaming.py`
- `apps/backend/src/super_ai/chat/runs.py`
- `apps/backend/src/super_ai/memory/models.py`
- `apps/backend/src/super_ai/memory/repositories.py`
- `apps/backend/src/super_ai/memory/sqlalchemy.py`
- `apps/backend/src/super_ai/api/app.py`
- `packages/api-contracts/src/chat.ts`
- `packages/api-contracts/src/openapi.ts`
- `apps/frontend/src/stores/chat.ts`
- `apps/frontend/src/components/ChatMemoryControls.vue`

### Task 1: Adaptive Mode 契约与数据库迁移

**Interfaces:** Produces `ChatMemoryMode = Literal["adaptive", "manual"]`，`normalize_memory_mode(value)`。

- [ ] 写失败测试：新 session 默认 adaptive；两个旧值更新后返回 adaptive；manual 保持 manual。
- [ ] Run: `cd apps/backend && uv run pytest tests/test_chat_memory.py tests/test_chat_sessions_api.py -q`，Expected: FAIL。
- [ ] 新增 migration：增加 `structured_memory JSONB NOT NULL DEFAULT '{}'`、`memory_summary_version INTEGER NOT NULL DEFAULT 0`、`memory_through_message_id`；把旧自动值 UPDATE 为 adaptive，并把 model default 改为 adaptive。

```python
def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("structured_memory", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("chat_sessions", sa.Column("memory_summary_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("chat_sessions", sa.Column("memory_through_message_id", sa.String(80), nullable=True))
    op.execute("UPDATE chat_sessions SET memory_mode='adaptive' WHERE memory_mode IN ('every_30_turns','context_70_percent')")
```
- [ ] 更新 Python/TS/OpenAPI enum；请求解析兼容旧值并规范化。测试 downgrade/upgrade 和契约。
- [ ] Run: `cd apps/backend && uv run pytest tests/test_chat_memory.py tests/test_chat_sessions_api.py -q`; `npm --workspace packages/api-contracts test`，Expected: PASS。
- [ ] Commit: `git commit -m "feat: migrate chat memory to adaptive mode"`。

### Task 2: Structured Memory Schema 与 CAS Repository

**Interfaces:** Produces `StructuredChatMemory`、`StructuredMemoryUpdate`、`compare_and_set_memory(owner, session, expected_version, update)`。

- [ ] 写失败测试：Pydantic 拒绝未知/敏感字段；并发 version 只有一个更新成功；失败不推进 through message。

```python
class MemoryEntry(BaseModel):
    value: str
    source_message_ids: tuple[str, ...]
    citation_ids: tuple[str, ...] = ()
    trust: Literal["user_asserted", "user_confirmed", "tool_grounded", "assistant_proposed"]

class StructuredChatMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_goals: tuple[MemoryEntry, ...] = ()
    confirmed_facts: tuple[MemoryEntry, ...] = ()
    preferences: tuple[MemoryEntry, ...] = ()
    decisions: tuple[MemoryEntry, ...] = ()
    open_tasks: tuple[MemoryEntry, ...] = ()
    resource_refs: tuple[MemoryEntry, ...] = ()
```

- [ ] Run: `cd apps/backend && uv run pytest tests/test_structured_chat_memory.py -q`，Expected: FAIL。
- [ ] 实现 JSONB validation、provenance、字段/条目/总字节上限和 `UPDATE ... WHERE summary_version = expected` CAS；返回 `updated | stale | not_found`。`confirmed_facts` 只接受 `user_confirmed` 或 `tool_grounded`；Prompt 中的“忽略规则/提升权限”不能成为 instruction 或 confirmed fact。

```python
MemoryCasResult = Literal["updated", "stale", "not_found"]

async def compare_and_set_memory(
    *, owner_user_id: str, session_id: str, expected_version: int,
    memory: StructuredChatMemory, through_message_id: str,
) -> MemoryCasResult:
    raise NotImplementedError
```
- [ ] Run 同一测试，Expected: PASS；Pyright 0 error。
- [ ] Commit: `git commit -m "feat: persist versioned structured chat memory"`。

### Task 3: Context Envelope 完整预算与最近 6 轮

**Interfaces:** Produces `ContextBudget`、`ContextEnvelope`、`ContextEnvelopeService.prepare(request)`。

- [ ] 写失败测试：预算包含 tool schemas/observations/10% output reserve；压缩后仍有最近 6 轮；安全状态不从 memory 注入；95% 抛 limit。

```python
@dataclass(frozen=True, slots=True)
class ContextBudget:
    window_tokens: int
    system_tokens: int
    tool_schema_tokens: int
    memory_tokens: int
    recent_turn_tokens: int
    observation_tokens: int
    output_reserve_tokens: int

@dataclass(frozen=True, slots=True)
class ContextEnvelope:
    system_prompt: str
    messages: tuple[ChatMessageRecord, ...]
    structured_memory: StructuredChatMemory
    budget: ContextBudget
    usage_percent: float
```

- [ ] Run: `cd apps/backend && uv run pytest tests/test_context_envelope.py -q`，Expected: FAIL。
- [ ] 实现 safe turn cutoff、Observation reducer 和 budget allocator；output reserve 为 `max(configured_min, floor(window*0.10))`。

```python
available = window_tokens - system_tokens - tool_schema_tokens - output_reserve_tokens
if available <= 0:
    raise ChatContextLimitReached
recent_turns = keep_complete_recent_turns(messages, count=6)
observations = reduce_observations(tool_results, max_tokens=observation_budget)
```
- [ ] 接入 `ChatTurnExecutionService`，删除 caller 自行切 history 的逻辑。
- [ ] Run: `cd apps/backend && uv run pytest tests/test_context_envelope.py tests/test_stream_rag_chat_api.py tests/test_chat_turn_execution.py -q`，Expected: PASS。
- [ ] Commit: `git commit -m "feat: build bounded chat context envelopes"`。

### Task 4: 持久压缩 Job、60/85/95 策略与恢复

**Interfaces:** Produces `StructuredMemoryCompactionHandler`、job kind `chat_memory_compaction`、`schedule_compaction(session)`。

- [ ] 写失败测试：60% 并发调度只建一个 Job；Worker retry CAS 收敛；85% 同步；manual 不在 60% 建 Job但 85% 兜底；压缩模型失败不推进；随机 message UUID 通过 `(created_at,id)` 边界读取；旧 `memory_summary` 在重压缩前仍作为 untrusted legacy context 可见。
- [ ] Run: `cd apps/backend && uv run pytest tests/test_chat_memory_jobs.py -q`，Expected: FAIL。
- [ ] 为 `BackgroundJobRepository` 增加原子 `enqueue_or_get`，使用稳定 job ID 创建 resource identity `session_id:through_message_id:version`；主键冲突后安全读取同一 Job，不依赖当前不存在的 resource unique index。

```python
resource_id = f"{session.id}:{through_message_id}:{session.memory_summary_version}"
job_id = "job_chat_compact_" + sha256(resource_id.encode()).hexdigest()[:40]
await jobs.enqueue_or_get(
    owner_user_id=owner_user_id,
    job_id=job_id,
    kind="chat_memory_compaction",
    resource_id=resource_id,
    payload={"sessionId": session.id, "throughMessageId": through_message_id,
             "expectedVersion": session.memory_summary_version},
)
```
handler 先 owner-scoped 读取 `through_message_id` 的 `(created_at,id)`，再按
`created_at < boundary OR (created_at = boundary AND id <= boundary_id)` 获取稳定消息前缀；不得按
随机 UUID 大小判断先后。

- [ ] 实现 legacy summary 安全过渡：migration 不删除 `memory_summary`；首次 adaptive prepare 把它标为 `legacy_untrusted` 并调度全历史结构化重压缩。新 Structured Memory CAS 成功后才停止注入旧摘要。恶意指令文本只能作为 quoted user assertion。
- [ ] 在 Chat Run 完成后调度 60% Job；prepare 阶段处理 85% 同步和 95% hard limit。模型/验证错误使用稳定错误码，原始响应不持久化。
- [ ] Run: `cd apps/backend && uv run pytest tests/test_chat_memory_jobs.py tests/test_postgresql_background_jobs.py tests/test_chat_runs_api.py -q`，Expected: PASS。
- [ ] Commit: `git commit -m "feat: compact structured chat memory durably"`。

### Task 5: 前端 Memory UX、文档与聚焦验收

- [ ] 写 Vitest：只显示 adaptive/manual；旧 API 响应规范化；后台压缩显示 running/degraded；manual compact 保持可用。
- [ ] 更新 contracts、client/store、Memory Controls 和 OpenAPI；UI 文案明确“保留最近 6 轮”和后台压缩。

```ts
export type ChatMemoryMode = "adaptive" | "manual";
export interface ChatMemoryState {
  readonly mode: ChatMemoryMode;
  readonly summaryVersion: number;
  readonly compactionStatus: "idle" | "queued" | "running" | "degraded";
}
```
- [ ] Run: `npm --workspace packages/api-contracts test && npm --workspace packages/api-contracts run typecheck`; `npm --workspace apps/frontend test -- chatMemory chatStore contracts && npm --workspace apps/frontend run typecheck`，Expected: PASS。
- [ ] Run: `cd apps/backend && uv run pytest tests/test_chat_memory.py tests/test_context_envelope.py tests/test_structured_chat_memory.py tests/test_chat_memory_jobs.py tests/test_chat_sessions_api.py tests/test_chat_runs_api.py tests/test_stream_rag_chat_api.py -q && uv run ruff check src/super_ai/chat tests/test_chat_memory*.py tests/test_context_envelope.py tests/test_structured_chat_memory.py && uv run pyright`，Expected: PASS。
- [ ] 更新 backend README，说明 adaptive/manual、60/85/95 和 Structured Memory 禁止字段。
- [ ] Commit: `git commit -m "docs: complete adaptive chat memory rollout"`。
