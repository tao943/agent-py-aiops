# Conversation Agent 与 AIOps Agent 关联设计

**日期：** 2026-08-22  
**状态：** 待用户审阅  
**目标版本：** 两阶段可靠闭环

## 1. 背景与目标

当前 Conversation Agent 已支持会话、记忆、知识库检索、Skill 和 MCP Tool Calling；
AIOps Agent 已支持 Alertmanager 自动接入、Incident、后台诊断、证据链、报告、Validator、
Policy Gate 和恢复提案。两者目前只共享 LLM、RAG、MCP 与 PostgreSQL，Conversation Agent
不能直接查询事故或管理诊断生命周期。

本设计把 Conversation Agent 定位为**交互与编排层**，把 AIOps Agent 保持为**诊断与安全
决策层**。用户可以通过自然语言查询活跃事故、启动诊断、跟踪状态、阅读报告与证据，并
提交人工恢复审批请求；Chat 不复制诊断逻辑、不读取隐藏推理、不绕过 Validator 或
Policy Gate，也不直接执行恢复动作。

目标闭环：

```text
Alertmanager -> Incident -> 用户在 Chat 中查询
  -> Intent Router -> AIOps Bridge Tool -> Diagnostic Task / Background Job
  -> LangGraph AIOps Agent -> Evidence / Report -> Validator / Policy Gate
  -> Chat 查询并解释公开结果
  -> 可选：创建人工审批请求（不执行恢复）
```

## 2. 范围与阶段

### 2.1 第一阶段：安全关联与交互性能

- Chat Intent Router；
- 按意图最小化暴露工具；
- AIOps Bridge Tools：查询活跃 Incident、启动诊断、查询诊断状态、报告和证据；
- `recovery_request` 只创建人工审批请求，不执行恢复；
- Conversation Agent 不再保存或展示模型原始 reasoning；
- SSE content delta 按小批量输出，不再逐字符拆帧；
- 前后端契约和聚焦测试。

### 2.2 第二阶段：持久运行、幂等与评测

- `chat_agent_runs` 和 `chat_run_events` 持久化；
- `client_request_id` 请求幂等；
- `chat_agent_run` Background Job；
- SSE 断线后按事件序号重订阅和回放；
- Worker 重启后恢复未完成 Run；
- Bridge Tool Calling 幂等；
- 稳定、无敏感细节的类型化失败；
- 小型 Conversation Eval。

### 2.3 非目标

- Chat 直接调用诊断底层 Runtime、CLS、数据库探针或恢复工具；
- Chat 修改 AIOps 根因、证据、评分、Validator 或 Policy Gate 结论；
- 自动批准或自动执行任何恢复动作；
- 把模型隐藏思维链保存为审计信息；
- 引入完整工单、排班、通知或审批平台；
- 使用 Redis 作为持久化真相源；
- 重写现有 AIOps LangGraph；
- 本阶段运行全量 Benchmark 或全量 pytest。

## 3. Reuse-first 评估

### 3.1 项目内复用

项目已有：

- LangChain `StructuredTool` 和 Chat Agent 流式事件适配；
- LangGraph AIOps 诊断链路及 PostgreSQL checkpoint；
- `DiagnosticTaskRepository`、证据链、诊断报告与 Tool Audit；
- Alertmanager Incident/Event 及原子 Diagnostic Task + Background Job 创建；
- `BackgroundJobRuntime` 的 lease、重试和 Worker 恢复；
- PostgreSQL、Redis、Outbox、SSE 与统一 API 错误封装。

因此 Bridge 应包装内部应用服务，而不是通过 HTTP 回调本进程，也不复制 Repository 或
诊断状态机。

### 3.2 GitHub 调研

| 候选 | 许可证/活跃性 | 结论 |
|---|---|---|
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | MIT，活跃 | 继续直接采用现有 LangGraph；参考 durable execution 和 checkpoint 语义 |
| [langchain-ai/langgraph-checkpoint-postgres](https://github.com/langchain-ai/langgraph-checkpoint-postgres) | MIT，活跃 | 功能重叠且会增加 psycopg 路径；当前已有 PostgreSQL checkpoint，不新增依赖 |
| [langchain-ai/langchain](https://github.com/langchain-ai/langchain) | MIT，活跃 | 继续使用现有 `StructuredTool` 包装高层 Bridge 契约 |

通用 GitHub 关键词未找到同时满足本项目 Incident、AIOps 安全门和现有 Repository 契约的
可直接采用实现。最终决策：

- **直接采用：** 现有 LangChain、LangGraph、PostgreSQL、Redis、Background Job、SSE；
- **包装采用：** 现有 Incident、Diagnostic Task、Evidence、Report Repository 与应用服务；
- **参考实现：** LangGraph durable execution/checkpoint 语义；
- **自定义实现：** 轻量 Intent Router、Bridge contract、Chat Run 状态机和 Conversation Eval；
- **新增依赖：** 无。

## 4. 组件边界

新增 `super_ai.chat.aiops` 和 `super_ai.chat.runs` 相关模块，避免继续扩大 `api/app.py`：

```text
chat/
  intent.py             意图类型、确定性候选规则、结构化 LLM 分类边界
  tool_policy.py        intent -> allowed tool names
  aiops_bridge.py       owner-scoped 高层应用服务与 StructuredTool 包装
  runs.py               Chat Run 状态机、执行编排、错误分类
  run_events.py         持久事件生成、批量 content delta、回放规则
  routes.py             Run 创建、查询、SSE 订阅与恢复审批路由
  evaluation.py         Conversation Eval runner/scorer
```

Repository 协议继续位于 `memory/repositories.py`，SQLAlchemy 实现在
`memory/sqlalchemy.py`，模型位于 `memory/models.py`。数据库迁移使用自动告警之后的
revision `202608220002`。

## 5. Intent Router 与工具策略

### 5.1 意图集合

```text
general_chat
knowledge_question
incident_query
start_diagnostic
diagnostic_status
recovery_request
```

Router 输出严格结构：

```json
{
  "intent": "diagnostic_status",
  "confidence": 0.91,
  "incidentId": null,
  "diagnosticTaskId": "diagnostic_...",
  "needsClarification": false
}
```

ID 只能来自用户明确输入或 owner-scoped 查询结果，不能由模型自由生成后直接信任。

### 5.2 路由策略

1. 先用确定性规则识别明确命令、Incident ID、Diagnostic Task ID 和状态询问；
2. 不能唯一判定时，调用主 Chat 模型做一次严格结构化分类；
3. 低置信度、缺少目标或多个候选时进入澄清，不暴露写入型工具；
4. Router 失败时安全降级到 `general_chat`，只暴露无副作用工具；
5. Router 结果持久化为公开决策元数据，只包含意图、置信度、规则/模型来源和目标 ID，
   不包含隐藏推理。

### 5.3 最小工具暴露矩阵

| Intent | 可用工具 |
|---|---|
| `general_chat` | `get_current_time`、`load_skill` |
| `knowledge_question` | `knowledge_retrieval`、`load_skill` |
| `incident_query` | `list_active_incidents`、`get_incident` |
| `start_diagnostic` | `list_active_incidents`、`get_incident`、`start_incident_diagnostic` |
| `diagnostic_status` | `get_diagnostic_status`、`get_diagnostic_report`、`get_diagnostic_evidence` |
| `recovery_request` | `get_diagnostic_status`、`get_diagnostic_report`、`create_recovery_approval_request` |

用户 MCP Tools 不再默认全部暴露。第一版仅在 `general_chat` 或显式配置的安全意图下加载，
且 AIOps 意图绝不混入任意 MCP 工具。现有 Skill 只提供 Prompt 知识，不能扩大工具权限。

## 6. AIOps Bridge 契约

Bridge 是 owner-scoped 应用服务，不是底层工具代理。每次调用都从已认证用户上下文注入
`owner_user_id`，模型参数不能覆盖租户。

### 6.1 `list_active_incidents`

输入：`limit`（默认 10，最大 50）。  
输出：Incident ID、alert name、service、severity、last seen、diagnostic task ID。  
约束：只返回当前 owner 的 active Incident，不返回原始 Webhook payload。

### 6.2 `get_incident`

输入：`incident_id`。  
输出：owner-scoped Incident 公开摘要和关联 diagnostic task ID。  
不存在和越权统一返回 `RESOURCE_NOT_FOUND`，避免枚举其他租户资源。

### 6.3 `start_incident_diagnostic`

输入：`incident_id`、可选公开补充说明。  
输出：`diagnostic_task_id`、Background Job ID、状态和 `reused`。  
行为：调用共享的 AIOps 应用服务，原子创建/复用 Diagnostic Task 与 Background Job；
不得直接实例化 LangGraph 或等待 LLM 完成。自动告警已经创建诊断时返回现有任务。

幂等键：

```text
sha256(owner_user_id + incident_id + diagnostic_generation)
```

同一 active Incident 的同一 generation 最多创建一个诊断。resolved 后再次 firing 产生新的
Incident/generation，不会复用旧任务。

### 6.4 `get_diagnostic_status`

输入：`diagnostic_task_id`。  
输出：queued/running/succeeded/failed、阶段、公开错误码、报告是否可用。  
不得返回 checkpoint 原始 state、模型原始响应或异常堆栈。

### 6.5 `get_diagnostic_report`

输入：`diagnostic_task_id`。  
输出：根因结论、置信信息、引用、恢复模式、`executionPermitted`、人工审批要求和降级状态。
输出必须直接来自持久化 AIOps Report，不允许 Chat 改写安全字段。

### 6.6 `get_diagnostic_evidence`

输入：`diagnostic_task_id`、可选 `limit`。  
输出：公开 Evidence Evaluation、来源、时间、支持/反驳关系和引用 ID。  
不返回 ground truth、内部 Prompt、checkpoint 或隐藏 reasoning。

### 6.7 `create_recovery_approval_request`

输入：`diagnostic_task_id`、用户确认的理由。  
前置条件：诊断已完成；报告存在恢复提案；`humanApprovalRequired=true`；请求属于当前 owner。
输出：approval request ID、`pending` 状态和提案摘要。

该工具只创建不可执行的人工审批记录，始终返回 `executionPermitted=false`。第一版不存在
“批准并执行”接口。若报告为 `no_action` 或没有恢复提案，返回类型化拒绝。

## 7. Chat Run 持久化模型

### 7.1 `chat_agent_runs`

- `id`：随机稳定业务 ID；
- `owner_user_id`、`chat_session_id`；
- `client_request_id`：客户端生成的 UUID；
- `user_message_id`、`assistant_message_id`；
- `intent`、`router_source`、`target_resource_id`；
- `status`：`queued | running | succeeded | failed | cancelled`；
- `attempt_count`、`error_code`；
- `background_job_id`、`diagnostic_task_id`；
- `last_event_sequence`；
- `created_at`、`started_at`、`completed_at`、`updated_at`。

唯一约束：

```text
(owner_user_id, chat_session_id, client_request_id)
```

相同键和相同规范化请求返回原 Run；相同键但请求指纹不同返回
`IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST`。

### 7.2 `chat_run_events`

- `run_id`、单调递增 `sequence` 组成主键；
- `event_type`；
- `public_payload`：经过 allowlist 的 JSONB；
- `created_at`。

只持久化可向用户展示的事件：run 状态、批量 content delta、公开 tool status、reference、
complete、类型化 error。禁止写入模型 reasoning、Prompt、凭据、原始工具输出和异常文本。

### 7.3 `aiops_recovery_approval_requests`

- `id`、`owner_user_id`、`diagnostic_task_id`；
- `status`：第一版固定 `pending`；
- `proposal_fingerprint`；
- `request_reason`（长度有界）；
- `created_by_chat_run_id`；
- `created_at`。

唯一约束：`(owner_user_id, diagnostic_task_id, proposal_fingerprint)`，重复请求返回现有记录。
该表没有执行状态、批准人或执行入口，避免误把第一版做成恢复执行系统。

## 8. Run 生命周期、Background Job 与恢复

### 8.1 创建和订阅 API

```text
POST /chat/sessions/{session_id}/runs
GET  /chat/sessions/{session_id}/runs/{run_id}
GET  /chat/sessions/{session_id}/runs/{run_id}/events
```

创建请求包含 `content`、`metadata` 和必填 `clientRequestId`，API 在一个 PostgreSQL 事务中：

1. 验证 owner/session；
2. 创建或复用 user message；
3. 创建或复用 `chat_agent_run`；
4. 创建或复用类型为 `chat_agent_run` 的 Background Job；
5. commit 后唤醒 Runtime；
6. 返回 `202`、run ID 和订阅 URL。

事件接口使用 SSE，支持标准 `Last-Event-ID`，也允许查询参数 `afterSequence`。服务先从
PostgreSQL 回放大于该序号的事件，再轮询/订阅新事件，直到终态。断开连接不取消 Run。

旧 `messages:stream` 在迁移期保留，但内部委托 Run API；前端切换完成后标记 deprecated。

### 8.2 Worker 重启

复用现有 Background Job lease/retry：

- queued job 可由新 Worker 获取；
- running job lease 过期后可被重新获取；
- Chat Run 终态时重复 handler 直接返回；
- 中断时已输出但未形成最终 assistant message 的内容属于 tentative draft；
- 新 attempt 先追加 `run.restarted`，前端清空 tentative draft，再重新生成；
- assistant message 只在成功提交时创建一次，并由 Run 唯一关联防重复。

恢复保证“请求最终可继续且不重复副作用”，不承诺从模型 token 中间位置续写。模型调用可能
重做，但已成功的 Bridge Tool 通过逻辑调用幂等记录复用。

## 9. Tool Calling 幂等

逻辑调用键：

```text
tool_call_key = sha256(
  chat_run_id + logical_step + tool_name + canonical_arguments
)
```

- 只读查询工具：完成后可安全复用缓存结果；短暂失败记录新 attempt；
- `start_incident_diagnostic`：除 tool key 外，依赖 AIOps 层 Incident generation 唯一约束；
- `create_recovery_approval_request`：依赖 proposal fingerprint 唯一约束；
- 状态不确定的写调用不得盲目补执行，先按业务唯一键查询结果；
- Tool Audit 记录逻辑调用、attempt、公开摘要和类型化错误，不吞掉持久化失败；审计保存失败
  会使当前 Run 安全失败或重试，不能伪装成成功。

PostgreSQL 唯一约束是最终正确性保障；Redis 只用于可选通知/缓存，不承担幂等真相源。

## 10. SSE 批量输出与前端行为

后端不再把模型 delta 拆成单字符。采用以下任一条件 flush：

- 累计达到 32 个字符；
- 距上次 flush 达到 50 ms；
- 收到 tool/reference/complete/error 或流结束。

每个 `content.delta` 是一个持久事件并有稳定 sequence。前端移除逐字符定时器，直接追加
批量 delta；重连时按 sequence 去重。`run.restarted` 清空未完成草稿，`complete` 用数据库
中的最终 assistant message 替换草稿。

事件 payload 和数据库 event 使用同一 allowlist serializer，避免实时路径和回放路径泄露
不同字段。

## 11. Reasoning 与审计安全

- 删除 Chat 对 `reasoning_content` / `reasoning` 的提取、SSE 发送和 message metadata 保存；
- 前端移除“深度思考”展示；
- 历史消息中已保存的 reasoning 不在 API payload 中返回；本迁移不破坏性清除旧数据；
- 可审计链只保留：意图结果、选择的工具、规范化参数摘要、工具公开结果、Evidence ID、
  Report ID、Validator/Policy 公开状态、耗时和类型化失败；
- 不记录 Prompt、模型隐藏思维链、完整工具原始输出、凭据或异常堆栈。

“可审计”指可验证输入、动作、证据和决策结果，不等于展示模型私有推理。

## 12. 类型化失败

公开错误至少包括：

```text
CHAT_RUN_NOT_FOUND
CHAT_RUN_CONFLICT
CHAT_ROUTER_FAILED
CHAT_TARGET_REQUIRED
INCIDENT_NOT_FOUND
INCIDENT_NOT_ACTIVE
DIAGNOSTIC_NOT_FOUND
DIAGNOSTIC_NOT_COMPLETE
DIAGNOSTIC_START_CONFLICT
RECOVERY_PROPOSAL_NOT_AVAILABLE
RECOVERY_APPROVAL_NOT_ALLOWED
BRIDGE_TOOL_TIMEOUT
BRIDGE_TOOL_TEMPORARILY_UNAVAILABLE
CHAT_AGENT_MODEL_FAILED
CHAT_RUN_RETRY_EXHAUSTED
```

Repository/供应商异常映射为稳定类别；原始异常只进入受控服务端日志并执行敏感字段清洗。
SSE error 事件包含 `code`、`retryable`、run ID，不包含 `str(exc)`。

## 13. Conversation Eval

第一版建立 12 个离线、确定性 Fixture 场景：

- 2 个 general chat / knowledge question；
- 2 个活跃 Incident 查询（含多候选澄清）；
- 2 个启动诊断（首次创建、重复请求复用）；
- 2 个状态/报告/证据查询；
- 2 个恢复请求（允许创建审批、无提案时拒绝）；
- 2 个安全场景（跨租户 ID、Prompt injection 要求调用恢复工具）。

指标：

- intent accuracy；
- required target extraction；
- allowed-tool precision（不得出现越权工具）；
- task completion；
- bridge result grounding（回答引用真实 Incident/Task/Report/Evidence ID）；
- idempotency correctness；
- cross-tenant isolation；
- recovery safety（自动执行次数必须为 0）；
- reasoning leakage（必须为 0）；
- SSE replay correctness。

硬门：跨租户隔离、越权工具、自动恢复执行或 reasoning 泄露任一失败，则整体不通过；不能
用其他得分抵消。Eval 使用 fake model/runner 和 PostgreSQL 集成库，不调用真实 LLM、CLS 或
Docker Live 环境。

## 14. 测试与验收

### 14.1 单元测试

- Router 规则、结构化输出、低置信度澄清和失败降级；
- intent-tool allowlist；
- Bridge 参数规范化、owner 注入和安全返回；
- content batching；
- public event serializer 拒绝 reasoning/secret/oracle 字段；
- 错误分类和 retryable 标记。

### 14.2 PostgreSQL 集成测试

- Run + user message + Background Job 原子创建；
- 相同 `client_request_id` 并发创建只产生一个 Run；
- 不同请求复用 idempotency key 被拒绝；
- Tool call、诊断启动和审批请求并发幂等；
- Worker lease 过期恢复、终态重复 handler no-op；
- assistant message 最多一个；
- SSE 回放无缺失、无重复、严格递增；
- owner/session/Incident/Diagnostic 跨租户隔离。

### 14.3 API 与前端测试

- Run 创建/查询/订阅契约；
- `Last-Event-ID` 断线重订阅；
- `run.restarted` 清除 tentative draft；
- complete 用持久消息收敛；
- Chat Transcript 不展示 reasoning；
- AIOps Report 安全字段原样呈现，不被 Chat 覆盖。

### 14.4 聚焦回归

至少运行 Chat session、memory、stream API、active alerts、alert ingestion、diagnostic API、
background jobs、tool audit 相关测试，以及新增 Conversation Eval。第一阶段和第二阶段分别
设验收点，不要求在本工作中运行全量 pytest。

## 15. 实施顺序与兼容性

1. 第一阶段先抽取 owner-scoped AIOps 应用服务和 Incident 查询 Repository；
2. 加入 Router、Tool Policy 和 Bridge Tools；
3. 移除 reasoning 输出并实现批量 SSE；
4. 完成第一阶段聚焦回归；
5. migration `202608220002` 加入 Run/Event/Approval 表；
6. 接入 `chat_agent_run` Background Job、幂等和恢复；
7. 新 Run API 与旧 stream adapter 并存，前端迁移；
8. 加入 Conversation Eval 和第二阶段验收；
9. 稳定后另开变更移除旧 stream endpoint。

迁移只新增表和索引，不修改现有消息、诊断或 Incident 数据。API 迁移期向后兼容；数据库
downgrade 只删除新表，不删除旧 Chat/AIOps 数据。

## 16. 成功标准

- 用户能从 Chat 查询活跃 Incident 并启动或复用一个持久诊断；
- 用户能在断线、刷新页面或 Worker 重启后继续查看同一 Run；
- 重试不会重复创建消息、诊断、Background Job 或恢复审批请求；
- Chat 回答能引用真实 Report/Evidence，并保留 AIOps 安全结论；
- Chat 无法看到其他 owner 的 Incident、Task、Report 或 Evidence；
- Chat 永远不能直接执行恢复动作或绕过 Validator/Policy Gate；
- 新产生的 SSE、消息 metadata、事件和 UI 中不存在模型原始 reasoning；
- 聚焦测试与 Conversation Eval 全部通过。
