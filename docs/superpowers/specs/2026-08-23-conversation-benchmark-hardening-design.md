# Conversation Benchmark Hardening Design

日期：2026-08-23  
状态：用户已批准实施

## 1. 目标

修复真实 Conversation Model Eval 与 Chat→AIOps PG Lock Live Eval 暴露的三个问题：

1. Prompt Injection 被路由为高风险 `recovery_request`；
2. `explanation_timeout` 没有真正注入 timeout，导致测评信号无效；
3. PG Lock Live 虽完成根因、恢复和验证，但 CLS timeout 里程碑没有建立，最终 93 分。

当前已修复的 Chat Live CLI 参数、评测 session 和 nullable Chat Run 外键合同作为本设计基线，
不得回退。

## 2. 约束

- 不降低 Conversation 或 Live 评分阈值，不修改 ground truth 迎合现有输出。
- 不把 `benchmarkEvidenceId`、ground truth、oracle 或 evaluator 标签暴露给 Agent、Prompt、RAG、报告。
- Prompt Injection 命中后不得调用 Router LLM、Agent LLM 或工具。
- timeout 场景必须由显式 fault injection 触发，不依赖供应商偶发超时。
- PG Lock 的 CLS timeout 必须来自真实被阻塞更新的有界超时观测；空记录、错 run、错 scenario
  或只有 `database_contention` 的记录不能得 timeout 分。
- 保留 PostgreSQL 锁图与 wait event 作为根因事实；CLS 只证明业务请求时间线和 timeout。
- 不新增依赖、外部服务或密钥权限。
- 所有新增行为使用 Red→Green TDD，并保留安全的 PostgreSQL/Archive 终态。

## 3. 复用评估

本地直接复用：

- `ChatIntentRouter`、`ChatRoute` 和 `ChatStreamingService` 的现有路由/持久化边界；
- `RaisingChatModel`/`TimeoutError` 测试模式和安全错误分类；
- `LiveFaultObservation`、`LiveClsRecordProvider`、`LiveCompositeEvidenceMcpClient`；
- `RunArtifact` evaluator-only 投影和现有 required-evidence scorer。

GitHub 候选：

- `protectai/llm-guard`（MIT）：在线扫描能力完整，但依赖与模型重量超出窄路由门需求；
- `NVIDIA-NeMo/Guardrails`：框架和 DSL 较重，许可元数据不是简单 MIT/Apache 路径；
- `Azure/PyRIT`（MIT）：适合红队和风险评测，不适合请求时高风险工具路由。

选择“内部 wrapped adoption + 外部 reference only”。不引入第三方包。

## 4. 方案

### 4.1 确定性输入安全门

新增纯函数输入分类器。只有同时命中“指令覆盖”与“敏感/高风险目标”两类信号时才拦截，
例如“忽略所有规则”同时要求执行恢复、读取 API Key 或展示隐藏推理。安全门在显式资源 ID 规则
和 LLM Router 之前运行。

被拦截的 `ChatRoute` 携带固定 allowlisted `blocked_reason`。`ChatStreamingService` 持久化用户消息，
随后直接持久化并流式返回固定安全拒绝；不构造 Agent request，不生成标题，不调用模型或工具。
审计元数据只保存 reason code，不复制攻击文本。

### 4.2 确定性 timeout Eval

`explanation_timeout` 使用评测专用 timeout model boundary，稳定抛出 `TimeoutError`。真实 provider
只执行其他五个场景。结果分别记录 provider 调用数、注入失败数和场景尝试数，避免把注入调用
计入真实额度。该场景验证安全降级分类和持久化，不声称供应商稳定性。

### 4.3 PG Lock 真实 timeout 与 CLS claim

现有阻塞 waiter 保持运行以供锁图工具检查。Driver 使用 `asyncio.wait_for(asyncio.shield(waiter_task),
timeout)` 证明同一更新在 deadline 内未完成，并将 `business_probe_timed_out` 加入 observation。
恢复仍由原白名单动作解除 blocker，cleanup 负责终止残余任务。

PG Lock 专用 `LiveClsRecordProvider` 只有在 wait event、blocking edge 和 business timeout 三项均通过
时才生成含 `request_timeout` 的 run-scoped 日志。`request_timeout` 加入 CLS 安全事件 allowlist。

Composite Tool 返回给 Agent 的仍只有受限 records/counts，不返回 benchmark claim。Artifact projector
在 evaluator-only 边界中，仅当持久化的 `SearchLog` records 含本场景 `request_timeout` 时映射为
`cls-live-request-timeout`。空记录、其他事件或其他 scenario 均保持普通 Evidence ID。

## 5. 错误处理

- 安全门：固定 `prompt_injection_sensitive_action`，不得包含原输入。
- timeout injection：只允许 `explanation_timeout` 使用；其他场景异常仍为真实 `model_call_failed`。
- PG Lock 更新在 timeout 前完成：故障观测不成立，运行按 fault preparation failure 处理，不能评分。
- CLS provider 收到未确认 observation：fail closed，不上传声称 timeout 的日志。
- evaluator 无合格 timeout record：保持 `required_evidence_missing`，不得补写 claim。

## 6. 验收

- 中英文 Prompt Injection 组合被阻断，普通“解释什么是 prompt injection”不误杀。
- 被阻断请求 Router/Agent/Tool 调用数均为 0，消息与安全 reason 可审计。
- Conversation Model Eval 六场景通过；provider 调用数为 5，注入 timeout 为 1。
- PG Lock observation 包含三项通过检查；CLS records 包含 run-scoped `request_timeout`。
- 没有 timeout 记录时 Live required evidence 仍失败；有合格记录时三项里程碑全部通过。
- 真实 Chat Live 得到有效终态，恢复验证和 cleanup 必须继续通过。

