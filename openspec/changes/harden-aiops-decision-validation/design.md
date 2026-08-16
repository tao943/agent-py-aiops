## Context

真实 Run `eval-b6ddb7d6083342cd975ff5a8412fca5d` 已收集 PostgreSQL 40P01、相反资源
获取顺序和长度为 2 的 Wait Graph，并生成完整 grounded causal chain。Decision Validator 三次进入
统一异常分支后，Workflow 把验证器不可用写成 `missingEvidence`，错误调用
`GetDatabaseMetrics`，最终清空正确根因。

## Goals / Non-Goals

**Goals:**

- 区分 `candidate_missing`、`deterministic_gap`、`model_call_failed`、
  `invalid_model_output`、`model_rejected` 和 `retry_exhausted`。
- 只在公开证据合同完整通过时允许 `deterministic_grounded_fallback`。
- 确保验证器基础设施故障不触发证据 Replan。
- 将降级结论限制为人工审核恢复。
- 保留历史 `evidence-driven-v2` Run 的 Artifact 兼容性。

**Non-Goals:**

- 不改变 Ground Truth、评分权重、阈值或 canonical answers。
- 不让 RAG 知识引用成为故障成立证据。
- 不增加自动恢复权限、数据库 schema、外部服务或前端合同。

## Decisions

### 双层验证

第一层确定性验证只读取公开 hypotheses、公开 decision vocabulary、当前任务 Evidence IDs 和
Evidence-linked Observation decisions。第二层使用现有 LangChain/Qwen structured output 做语义
复核；格式错误最多纠正一次，模型调用失败不做格式重试。

### 确定性证据合同

候选必须同时满足：恰好一个 supported hypothesis、无 open 竞争根因、公开 component/mechanism
标签精确匹配、全部 candidate Evidence IDs 属于当前任务且属于支持该根因的 Observation Evidence
集合、至少两份不同正证据、至少两条支持 Observation、2 至 6 条 causal chain 全部来自这些
Observation summaries、trigger 非空、confidence 在 `[0,1]`。

Alert、Knowledge reference、Report、外部任务 Evidence 和不支持该 hypothesis 的 Evidence 不能
计入或混入降级候选。Validator 与 Agent 均不能访问 Ground Truth。

### 路由与重规划

Validator API、超时或格式故障只记录基础设施/格式错误，不写成 `missingEvidence`，不进入 Replanner。
确定性 gap 只有在 gap 属于可补证据类别，且当前 Sufficiency payload 提供已发现、未执行的
`recommendedTools` 时才能定向 Replan；完整性、标签、归属和 causal-chain gap 直接 fail-closed。
LLM 明确返回 invalid 时，只有非空 `missingEvidence` 或 `unsupportedFields` 才允许一次有界 Replan。

### 安全降级与审计

第一层完整通过且第二层不可用时，保存 `status=valid`、
`validationOrigin=deterministic_grounded_fallback`、`validationWarning`、错误类别、尝试次数和
allowlisted deterministic check codes。降级结论只能进入 `manual_review`，Policy Gate 保持
`executionPermitted=false`。不保存完整模型输出、Prompt、堆栈、凭据或原始敏感日志。

### Workflow 版本

新 Run 写入 `workflowVersion=evidence-driven-v3`。v3 Artifact 必须同时满足 `status=valid` 和
allowlisted validation origin；历史 v2 Artifact 继续使用原有 `status=valid` 合同，避免旧 Run 因
缺少新字段而失效。

## Risks / Trade-offs

- 确定性 fallback 比纯 LLM fail-closed 更可用，因此必须用正证据集合、竞争假设和人工恢复边界
  防止评分虚高。
- 一次格式纠错会增加一次调用，但仅针对解析失败；provider failure 不重复消耗额度。
- v3 增加审计字段，但仍保存在现有 JSONB 中，不提供新的公共 API 字段保证。
