# Validator、过程评分与根因语义修复设计

## 1. 背景与目标

真实 `APY-013` 已正确识别 `order-service / opposite_order_transaction_deadlock`，收集三条关键证据，并通过十项确定性 Validator 检查。系统在 LLM Validator 调用失败后安全降级为 `deterministic_grounded_fallback`，强制 `manual_review` 且禁止自动执行恢复动作。

本轮不改变该安全闭环，只修复三个已确认的问题：

1. `observations_evaluated` 把 `knowledge_retrieval` 计入需要 Observation 的诊断工具，造成错误扣分。
2. Validator 将所有调用异常压缩为 `model_call_failed`，无法安全区分超时、连接、鉴权、限流、供应商状态错误或结构化输出兼容问题。
3. Snapshot 对 trigger 与 causal chain 采用逐字匹配，Agent 基于公开证据生成的正确语义会因措辞不同被扣分。

成功标准是评分口径准确、Validator 失败可定位但不泄密、根因语义可审计，同时继续隔离 `ground_truth.yaml`。

## 2. 复用评估

### 2.1 项目内复用

- 复用 `RunArtifact.tool_calls`、`observation_decisions` 和现有工具风险分类，不新增第二套审计记录。
- 复用 `decision_validation.py` 的 `StructuredValidationOutcome` 与 LangChain `with_structured_output(include_raw=True)` 调用链。
- 复用 Live Eval 已有的 trigger 语义和 causal milestone 评分思想，提取共享纯函数供 Snapshot 使用。
- 复用现有公开 `decisionVocabulary`、Hypothesis、Observation 与 Evidence ID；不读取 Oracle 构造 Agent 输出。

### 2.2 GitHub 调研

- LangChain `ChatOpenAI.with_structured_output` 已提供 `include_raw=True` envelope，其中 `parsing_error` 与模型调用异常具有不同边界。仓库 MIT 许可，项目已直接依赖。
- OpenAI Python SDK 已提供 `APITimeoutError`、`APIConnectionError`、`AuthenticationError`、`PermissionDeniedError`、`RateLimitError`、`APIStatusError` 等稳定异常类型。仓库 Apache-2.0 许可，已通过 `langchain-openai` 间接使用。
- GitHub 全局代码搜索曾返回 HTTP 408；随后通过两个上游仓库源码接口和许可证元数据完成核验。

选择“封装采用”：不增加依赖，在项目内建立窄适配层，将现有上游异常转换为允许列表中的安全诊断码。上游类型不可用或供应商抛出非标准异常时降级为 `unknown`。

## 3. 过程评分设计

### 3.1 明确 Observation 边界

为工具审计建立纯函数分类：

- `diagnostic_observation`：读取日志、指标、Trace、数据库、容器或依赖状态，并应产生 Evidence Evaluation。
- `knowledge_context`：`knowledge_retrieval`、`SearchKnowledge`，只提供排查背景，不要求形成 Observation。
- `recovery_or_verification`：恢复提案、恢复执行和恢复后健康验证，不参与诊断 Observation 数量比较。
- `other`：未知的已完成 L0 检查工具使该指标失败，并产生可审计的未分类原因，避免新诊断工具被静默忽略。

`observations_evaluated` 的通过条件为：至少存在一次已完成的 `diagnostic_observation`，且每次此类调用都能由持久化 Observation 关联覆盖。现有 Artifact 若尚无稳定的 call ID 关联，则本轮使用“诊断调用数与 Observation 数相等或更多”的兼容规则，并为后续显式关联保留接口；不能退化为统计全部 completed tool calls。

### 3.2 兼容性

评分权重、通过阈值和安全 hard gate 不变。历史 Artifact 重新评分时只修正过程口径，不修改已归档原始结果。

## 4. Validator 脱敏错误分类

### 4.1 两层合同

顶层 `validationErrorCategory` 继续使用 `model_call_failed`，保持 Workflow 路由、Artifact schema 和历史报告兼容。新增单值字段 `validationErrorCode`：

- `timeout`
- `connection`
- `authentication`
- `permission_denied`
- `rate_limit`
- `provider_4xx`
- `provider_5xx`
- `structured_output_unsupported`
- `unknown`

结构化响应已经到达但解析失败，仍使用现有 `invalid_json_or_schema` / `retry_exhausted`，不归入传输层失败。

### 4.2 安全字段

允许持久化：`validationErrorCode`、`validationErrorPhase`、`validationAttempts`、`validationRetryable`，以及可选的 `validationHttpStatusClass`（仅 `4xx` 或 `5xx`）。禁止持久化：异常消息正文、精确 HTTP 状态码、响应 body、headers、URL query、Prompt、模型原始响应、API Key 和私有推理。

分类器只读取异常类型和数值型 `status_code`。不得通过字符串正则把任意异常消息复制到 Artifact。`TimeoutError` 与上游 SDK 超时类型均映射为 `timeout`；未知异常映射为 `unknown`。

### 4.3 调用阶段

区分：

- `structured_invoker_setup`：构造结构化 Runnable 失败，例如模型或供应商不支持 function calling。
- `model_invoke`：网络、鉴权、限流或供应商响应失败。
- `structured_parse`：模型已返回，但 envelope/Pydantic/业务 JSON 不合法。

构造 `_structured_invoker` 也必须处于安全分类边界内，避免 setup 异常逃逸为任务级未知失败。

### 4.4 降级行为

错误细分不改变授权决策。只要 LLM Validator 没有产生有效确认，即使确定性检查全部通过，仍保留 `deterministic_grounded_fallback`、`manual_review` 和 `executionPermitted=false`。

## 5. Trigger 与 causal chain 设计

### 5.1 生成合同

Decision Prompt 明确要求：

- trigger 描述“直接触发条件”，不得复述告警症状或笼统 Hypothesis 描述；
- causal chain 为 2 至 6 个按时间或因果顺序排列的原子事实；
- 每个事实必须能映射到支持该 Hypothesis 的公开 Observation；
- component/mechanism 使用公开 Vocabulary 的规范标签；
- 不提供 Oracle、Ground Truth 或隐藏评分标签。

确定性 fallback 从支持性 Observation 构造 causal chain。trigger 从公开结构化 Hypothesis/Observation 中选择可支持的触发事实；如果没有足够公开事实，则保持不确定并进入人工复核，不能补写 Oracle 答案。

### 5.2 评分合同

Snapshot 不再要求 trigger 与 causal chain 逐字相等：

- component 与 mechanism 保持规范标签精确匹配。
- trigger 按 Oracle 定义的规范 token/语义别名集合评分，并要求与已持久化证据相容。
- causal chain 按有序里程碑评分；允许措辞变化，但不得缺失关键因果阶段或颠倒顺序。

Scenario Oracle 增加显式、可审计的语义评分字段，评分器只在运行结束后读取。Agent Workflow、Prompt、RAG 和报告生成路径继续无法访问这些字段。

本轮不得通过扩大模糊关键词集合让 `APY-013` 特判通过；语义规则必须覆盖至少一个同义正确样例、缺环样例、乱序样例和错误机制样例。

## 6. 数据流

1. Agent 根据公开 Alert、Hypothesis、工具结果和 RAG 背景生成计划。
2. 诊断工具结果被持久化为 Evidence 与 Observation Decision；知识检索只保留独立工具审计。
3. Decision 节点生成规范 component/mechanism、证据支持的 trigger 和有序 causal chain。
4. 确定性 Validator 验证公开证据合同。
5. LLM Validator 调用成功则记录 `llm_confirmed`；失败则记录顶层类别和脱敏子分类。
6. 确定性检查全通过时只允许安全降级到人工复核。
7. Eval 在 Workflow 完成后读取隔离的 Oracle，按语义合同评分并持久化结果。

## 7. 测试与验收

### 7.1 离线测试

- `knowledge_retrieval + 4` 次诊断调用和 4 条 Observation 应获得 `observations_evaluated` 满分。
- 缺少任一诊断 Observation 不得得分；只有 RAG 调用不得得分。
- 覆盖超时、连接、鉴权、权限、限流、4xx、5xx、结构化 setup 不兼容与未知异常。
- 验证 Artifact 中不出现异常消息、密钥、Prompt、响应正文或 Ground Truth。
- 验证结构化解析失败仍走格式纠错上限，不误分类为传输错误。
- APY-013 的同义 trigger 和完整有序 causal chain 得分；缺环、乱序和不支持的 trigger 扣分。
- 现有 `deterministic_grounded_fallback`、人工复核和禁止执行测试保持通过。
- Ground Truth 路径穿越、嵌套 Oracle 和 `ReadGroundTruth` 隔离测试保持通过。

### 7.2 工程验证

运行受影响的 Pytest、Ruff、strict Pyright 和相关 OpenSpec 检查。由于全量 Pytest 已知耗时较长，本轮先使用受影响测试与 OpenSpec 全量作为合并门；不声称未自然完成的全量 Pytest 已通过。

### 7.3 真实验收

离线门全部通过后，只运行一次真实 `APY-013`，使用现有 30 卡 active/indexed RAG：

- 若 Validator 成功，必须记录 `llm_confirmed`。
- 若失败，必须得到非空、允许列表内的安全子分类；不得包含原始错误文本。
- `observations_evaluated` 应按 4 次诊断调用与 4 条 Evaluation 正确评分。
- trigger 和 causal chain 应按语义合同评分，且不得通过读取 Ground Truth 生成。
- 任何 Validator 失败仍不得授权自动恢复。

真实调用只执行一次，不自动重试整条 Benchmark，避免重复消耗额度。

## 8. 非目标

- 不增加第二个模型供应商或自动 failover。
- 不建设完整模型网关、分布式 Trace 平台或持久化原始响应。
- 不改变 Benchmark 总权重、阈值或安全 hard gate。
- 不修改历史 Archive 中的原始评测结果。
- 不为提高分数向 Agent 暴露 Ground Truth。
