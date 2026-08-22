# Chat Turn Execution、上下文与 ReAct 优化设计

**日期：** 2026-08-22
**状态：** 已批准，待实施
**前置设计：** `2026-08-22-conversation-aiops-copilot-design.md`

## 1. 目标

在不替换现有 LangChain `create_agent`、AIOps LangGraph、owner-scoped Bridge 和 PostgreSQL
持久化的前提下，完成以下优化：

- 明确目标的 AIOps 查询不再依赖模型选择工具；
- 新诊断和新恢复审批写入必须经过可恢复、可幂等的用户确认；
- ReAct 具有模型/工具调用预算、重复调用停止和宽松总时限兜底；
- 对话压缩改为结构化记忆并保留最近 6 轮原始对话；
- Conversation Eval 真正穿过 Router、Policy、Bridge 和事件序列化；
- 复用现有 Live Benchmark 验证 Chat → AIOps → CLS 完整闭环。

## 2. 非目标

- 不让 Conversation Agent 直接调用 CLS、数据库诊断探针或恢复工具；
- 不把整个 Conversation Agent 重写成新的 LangGraph；
- 不改变 AIOps Agent 的 Planner、Specialist、Evidence、Validator 或 Policy Gate；
- 不把 LLM Explanation 作为业务成功或安全字段的事实来源；
- 不把 Conversation Model Eval 与真实故障诊断准确率混成一个分数；
- 不引入 Redis 作为确认、上下文或幂等的真相源。

## 3. 当前问题

### 3.1 意图与完成条件分离

`chat/intent.py` 对显式 `diagnostic_*` 先固定返回 `diagnostic_status`，因此“为
diagnostic_x 创建恢复审批”可能被误路由。`tool_policy.py` 只返回允许工具集合，
`streaming.py` 再把存在的工具交给 `create_agent`；没有 required capability 或 postcondition，
模型可以不调用必要工具却仍生成回答。

当前 Conversation Eval 的安全 fake runner 直接复制 Fixture 期望值，能验证 scorer 和硬门，
但不能证明 Router、Tool Policy、Bridge 与 Public Event Serializer 已联合工作。

### 3.2 Context Envelope 不完整

当前 token 估算只包含 system prompt、memory summary 和 user/assistant messages，不包含工具
Schema、Skill、工具 Observation 与输出预留。压缩后只保留新的 user message，最近原始轮次
全部被摘要替换。摘要为自由文本，并被提示为“真实会话上下文”，缺少字段级 provenance 和
并发版本比较。

### 3.3 ReAct 依赖框架默认限制

当前 `create_agent(...).astream_events(...)` 没有项目级模型次数、工具次数、重复调用 fingerprint、
Observation token 上限和总时限。策略要求的工具如果未注册会被列表推导静默忽略。

## 4. 方案比较

### 4.1 局部条件分支

在 Router、`streaming.py` 和 `memory.py` 中继续增加判断。改动较少，但执行规则、预算和完成
条件继续分散，测试必须理解多个调用顺序，不采用。

### 4.2 深层 Chat Turn Execution（采用）

建立 `ChatTurnExecution`、`ContextEnvelope`、`ChatExecutionBudget` 和 `ToolCatalog`，现有
Router、Bridge、LangChain Agent 与 Repository 作为内部 Adapter。接口集中，能以同一测试面
验证 direct、confirmation、ReAct、degraded explanation 和 postcondition。

### 4.3 全量显式 LangGraph

把 Router、确认、工具、解释和压缩全部改成图节点。控制力高，但与现有 `create_agent` 重叠，
迁移和维护复杂度超过当前需求，不采用。

## 5. 总体架构

```text
Durable Chat Run
  → ContextEnvelope.prepare
  → Intent Router
  → ChatTurnExecution.decide
       ├─ direct_read
       ├─ confirmation_required
       └─ bounded_react
  → Structured Result
  → optional LLM Explanation
  → Postcondition Validator
  → persisted public Run Events
```

`ChatTurnExecution` 是调用方使用的统一接口。调用方不再自行组合路由、工具集合、确认状态、
任务完成和解读降级。

建议的核心结果：

```python
@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    route: ChatRoute
    mode: Literal["direct_read", "confirmation_required", "bounded_react"]
    status: Literal[
        "succeeded", "succeeded_with_degraded_explanation",
        "awaiting_confirmation", "failed", "manual_review"
    ]
    structured_result: Mapping[str, object] | None
    pending_action_id: str | None
    postcondition: str
    explanation_status: Literal["not_requested", "running", "succeeded", "degraded"]
    safe_error_code: str | None
```

该类型只表达公开状态，不保存模型 reasoning。

## 6. 执行模式

### 6.1 Direct Read

具有明确 owner-scoped Incident/Diagnostic 目标的查询直接调用 Bridge：

- 查询 Incident；
- 查询 Diagnostic 状态；
- 查询 Report；
- 查询 Evidence。

Bridge 成功后立即产生结构化卡片和 `structured.result` 事件。LLM Explanation 随后生成；
它失败时 Chat Turn 为 `succeeded_with_degraded_explanation`，用户可单独重试解读，不重复查询
或写入。

LLM Explanation 只能读取允许列表内的结构化字段和 citation。`executionPermitted`、
`humanApprovalRequired`、`recoveryMode`、Validator 状态和 Evidence ID 始终直接来自 Bridge。

### 6.2 Confirmation Required

启动诊断或创建恢复审批前先只读预检：

- 已有关联 Diagnostic 时直接返回复用任务，不确认；
- 已有相同恢复审批时直接返回复用审批，不确认；
- 只有确实产生新写入时创建 Pending Chat Action。

确认后只执行 Action 中冻结的业务意图，并在执行前重新验证 owner、目标状态和 fingerprint。

### 6.3 Bounded ReAct

普通聊天、知识问题、目标不明确的 Incident 探索和多步解释继续进入 LangChain
`create_agent`。ReAct 只能获得 Execution Policy 编译后的工具，并受 Chat Execution Budget
约束。

## 7. Execution Policy 与 ToolCatalog

每个意图的策略必须声明：

- execution mode；
- required capability；
- allowed tools；
- required postcondition；
- Chat Execution Budget；
- safe error/degradation mode。

`ToolCatalog.compile(policy, runtime_tools)` 返回实际选中工具、缺失 required tools、目录版本和
有界 Schema token 成本。required tool 缺失立即产生类型化失败，不能静默删除后继续回答。

Postcondition 示例：

- Incident 查询：owned Incident 或统一 not-found；
- 启动诊断：复用任务、Pending Chat Action 或新 Diagnostic ID；
- Report 查询：结构化 `diagnostic.result`；
- 恢复请求：复用审批、Pending Chat Action 或类型化拒绝；
- 知识回答：声明来自知识库的事实时必须有 citation。

模型生成文本本身不构成 postcondition。

工具 Observation 统一缩减为：资源 ID、关键事实、citation、安全字段、有界摘要、状态和安全
错误码。完整日志、原始模型响应和无界 payload 不进入 Conversation Agent 上下文。

## 8. Chat Execution Budget

预算以调用次数为主，总时限只作为防止永久悬挂的宽松兜底：

| 模式 | 模型调用 | 工具调用 | 总时限 |
|---|---:|---:|---:|
| Direct Read + Explanation | 1 | 固定 Bridge 调用 | 单次模型 timeout |
| 普通/知识 ReAct | 最多 2 | 最多 2 | 120 秒 |
| 探索性 ReAct | 最多 4 | 最多 6 | 180 秒 |
| 确认后写入 | 0 | 1 个固定 Bridge 调用 | 30 秒 |

相同 `tool_name + canonical_arguments_hash` 在同一逻辑步骤再次出现时优先复用 PostgreSQL Tool
Execution；Agent 再次请求同一结果时以 `repeated_tool_call` 停止，不继续消耗预算。

只读工具超时可安全重试一次。写入型工具超时且结果不确定时进入 `manual_review`，不能自动
补执行。总时限值进入现有项目配置模板，可调但必须有上限。

## 9. Context Envelope 与 Structured Memory

模型输入统一由以下部分组成：

```text
System Prompt
+ Structured Memory
+ 最近 6 轮原始对话
+ 当前用户消息
+ 当前工具 Schema
+ 有界工具 Observation
+ 输出 token 预留
```

Structured Memory 字段：

- `userGoals`；
- `confirmedFacts`；
- `preferences`；
- `decisions`；
- `openTasks`；
- `resourceRefs`；
- `citations`；
- `throughMessageId`；
- `summaryVersion`。

每条记忆必须携带 source message IDs、citation IDs 和 trust 分类；`confirmedFacts` 只接受用户
明确确认或工具证据支持的条目。现有自由文本 `memory_summary` 在首次结构化重压缩前作为
`legacy_untrusted` 兼容输入，不能作为 system instruction；结构化 CAS 成功后才停止注入。

禁止进入 Structured Memory：reasoning、Prompt、完整日志、工具原始输出、凭据和可能变化的
AIOps 安全状态。安全状态每次从最新 Report 读取。

预算先扣除 system prompt、工具 Schema 和至少 10% 输出预留，再分配 Structured Memory、
最近轮次与 Observation。达到 60% 后在本轮完成时创建持久化压缩 Job；达到 85% 且后台结果
尚未可用时同步压缩一次；达到 95% 仍不能满足预算才返回
`CHAT_CONTEXT_LIMIT_REACHED`。

后台压缩身份为 `(session_id, through_message_id, summary_version)`。Repository 使用版本比较
更新；旧 Job 不能覆盖新摘要。失败时不推进 `throughMessageId`，原始消息不删除。

Memory Mode 收敛为：

- `adaptive`：新默认值，使用 60% 后台、85% 同步和 95% 硬限制；
- `manual`：不在 60% 自动创建 Job，但达到 85% 时仍执行安全兜底压缩；
- `every_30_turns`、`context_70_percent`：兼容期输入别名，统一映射为 `adaptive`。

数据库迁移将现有两个自动模式改为 `adaptive`。共享契约和前端只展示 `adaptive` 与 `manual`；
后端在一个兼容版本内继续接受旧值并返回规范化后的 `adaptive`，随后通过独立变更删除别名。

## 10. Pending Chat Action

新增 PostgreSQL `pending_chat_actions`：

- `id`、`owner_user_id`、`session_id`、`chat_run_id`；
- `action_type`、`target_resource_id`、`public_arguments`；
- `action_fingerprint`；
- `status`；
- `expires_at`、`confirmed_at`、`execution_result_id`；
- `created_at`、`updated_at`。

状态：

```text
pending → confirmed → executed
       ↘ cancelled
       ↘ expired
       ↘ manual_review
```

默认 15 分钟过期。确认接口重新检查 owner、目标当前状态和 fingerprint；状态变化时旧 Action
失效，返回新的预检结果。重复或并发确认通过 PostgreSQL 唯一约束和行锁返回同一结果。

确认事务只把 Action 改为 `confirmed` 并原子创建稳定 ID 的 `pending_chat_action` Background
Job，不在 HTTP 请求内执行副作用。leased Worker 执行幂等 Scheduler/Approval 写入；崩溃重试
读取同一业务结果，无法证明结果时进入 `manual_review`。active fingerprint 使用 partial unique
index，不让 cancelled/expired 历史阻塞新 Action。

```text
POST /chat/actions/{action_id}/confirm
POST /chat/actions/{action_id}/cancel
GET  /chat/sessions/{session_id}/actions/pending
```

跨 owner 统一返回 404。恢复类 Pending Chat Action 只确认“创建人工审批请求”，不代表批准或
执行恢复。

## 11. 事件与错误语义

新增或收敛公开事件：

- `execution.mode_selected`；
- `structured.result`；
- `confirmation.required`；
- `confirmation.resolved`；
- `explanation.delta`；
- `explanation.degraded`；
- `budget.exhausted`；
- `complete`。

事件继续使用 PostgreSQL sequence 和 Last-Event-ID 回放。LLM Explanation 的 tentative delta
在 Run 重试时遵循现有 `run.restarted` 收敛规则。

错误必须分类为目标缺失、required tool unavailable、确认过期、目标状态变化、预算耗尽、
解读降级和写入结果不确定。公开错误不包含异常原文、Prompt、reasoning 或供应商响应。

## 12. 三层 Eval

### 12.1 Conversation Offline Eval

CI 使用真实 Router、Execution Policy、ToolCatalog、Postcondition Validator 和 Public Event
Serializer；Bridge 与 ReAct 使用受控 fake Adapter。Fixture 不向 runner 复制期望输出。

### 12.2 Conversation Model Eval

手动运行真实主模型与 fake Bridge，验证模糊路由、结构化结果解读、解读超时降级和 Prompt
injection。它不调用 CLS，因为这些能力不需要日志，且需要隔离模型问题与外部诊断问题。
Evaluation Artifact 使用向后兼容的 v2 schema；reader/import/audit 继续接受现有 v1，并为
`conversation_model` 与 live 的独立 `conversationMetrics` 增加严格 allowlist。

### 12.3 Chat → AIOps Live Eval

复用现有 Live Benchmark 场景、CLS 日志、Ground Truth、AIOps Scorer、Evaluation Artifact、
PostgreSQL 保存和 Single/Multi 模式。只增加 Chat 入口 Adapter：

```text
Live Alert / Incident
  → Chat 查询 Incident
  → Chat 请求启动并得到 Pending Chat Action
  → 用户确认后幂等启动或复用 Diagnostic
  → AIOps Agent 调用 CLS/诊断工具
  → Evidence / Report / Validator / Policy Gate
  → Chat 查询并解读 Report
```

CLS 仍由 AIOps Agent 调用，Conversation Agent 不直接获得 CLS Tool。

Conversation 指标包括路由、目标、执行模式、required tool、postcondition、确认安全、结构化
结果 grounding、调用预算、首卡时延、总时延和安全字段一致性。现有 AIOps 根因与证据评分
保持不变，不用 Conversation 分数覆盖。

安全硬门新增：未经确认写入、required tool 缺失却成功、预算耗尽后继续调用、过期/跨 owner
Action 执行、LLM Explanation 改写安全字段。

## 13. 测试范围

- Router：显式 recovery ID、模糊意图、低置信度和目标缺失；
- Execution Policy：三种模式和 required postcondition；
- ToolCatalog：required tool 缺失、目录版本、Schema token 成本；
- Chat Execution Budget：模型/工具次数、重复 fingerprint、宽松总时限；
- Context Envelope：结构化字段、最近 6 轮、工具 Schema、输出预留；
- Memory Mode：旧值迁移、兼容映射、`adaptive` 默认和 `manual` 85% 兜底；
- 压缩：后台幂等、版本冲突、85% 同步兜底、失败不推进；
- Pending Chat Action：过期、取消、跨 owner、并发确认、状态变化；
- Direct Read：结构化结果先返回，Explanation 失败降级；
- PostgreSQL：迁移 upgrade/downgrade、唯一约束与安全恢复；
- SSE/前端：确认卡、结构化卡、解读降级、断线回放；
- Conversation Offline Eval：真实联合链路与全部硬门；
- Conversation Model Eval：手动、结果持久化；
- Chat → AIOps Live：复用现有 Live 场景，不复制 Ground Truth。

## 14. 实施顺序

1. 修复真实联合 Conversation Eval，使当前行为问题先可见；
2. 引入 Execution Policy、ToolCatalog 和 Postcondition Validator；
3. 实现 Direct Read 与可降级 LLM Explanation；
4. 迁移 Pending Chat Action 和确认接口/前端；
5. 加入 Chat Execution Budget 与重复调用停止；
6. 实现 Context Envelope 和 Structured Memory；
7. 加入持久压缩 Job 与版本比较；
8. 扩展 Conversation Model Eval；
9. 为现有 Live Benchmark 增加 Chat 入口 Adapter；
10. 完成聚焦回归、性能对比和安全验收。

## 15. 成功标准

- 明确 AIOps 查询不依赖 ReAct 工具选择；
- 新写入未经有效 Pending Chat Action 确认时执行次数为 0；
- 相同确认、工具调用和压缩 Job 重试不产生重复副作用；
- Direct Read 的结构化结果不因 LLM 超时而失败；
- ReAct 不超过策略调用预算，重复调用能类型化停止；
- 压缩后保留最近 6 轮，Structured Memory 具有版本与 provenance；
- 安全字段不能由摘要或 Explanation 改写；
- Offline Eval 真正覆盖 Router → Policy → Bridge → Event；
- Chat → AIOps Live 复用现有场景并由 AIOps Agent 调用 CLS；
- 聚焦测试、静态检查、共享契约和前端检查全部通过。

## 16. 实施前复用门

实施前必须按仓库 reuse-first 规则完成项目依赖与相邻实现检查，并搜索 GitHub 中 LangChain/
LangGraph 的 middleware、tool call limit、summarization、message trimming 和 human-in-the-loop
参考实现。评估必须明确 direct adoption、wrapped adoption、reference only 或 custom；在该门完成
前不开始代码实现，也不新增依赖。
