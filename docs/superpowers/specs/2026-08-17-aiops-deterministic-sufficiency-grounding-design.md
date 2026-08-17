# AIOps 确定性 Sufficiency 与 Grounded Decision 规范化设计

## 背景

真实 `APY-013` Run `eval-cf9836b6fa4d4c7d8597505421d20474` 已正确生成
`order-service / opposite_order_transaction_deadlock`，并绑定 impact、mechanism、trigger 三类
正证据。Structured Decision 一次成功，不再出现 provider 4xx，但最终仍因
`deterministic_gap` 被拒绝。

失败来自两个不同边界：

1. Sufficiency 模型声称 `postgres_lock_wait` 和 `postgres_slow_query` 已被 refute，但持久化
   Hypothesis State 仍为 `open`。Workflow 过早进入 Decision，Validator 按持久化状态正确拒绝
   `no_open_competitor`。
2. LLM Candidate 的 trigger 和 causal chain 与 Observation 语义一致，但没有逐字复制
   Observation summary。Validator 的 `trigger_present` 和 `grounded_causal_chain` 精确合同因此失败。

本设计修复状态权威和文本规范化，不降低证据、安全或恢复门槛。

## 目标

- Sufficiency 的 supported/refuted/open 分类只由持久化 Hypothesis State 决定。
- 存在 open 竞争假设时，继续执行可验证这些假设的剩余只读 Plan Step，或在预算内 Replan。
- 保留 LLM 对缺失证据、推荐工具和公开摘要的建议能力，但不允许它覆盖状态事实。
- 在 Validator 前，把语义正确且证据完整的 trigger/causal chain 规范化到绑定 Observation。
- Validator 继续精确验证规范化后的结构，不引入 embedding 相似度或第二个语义裁判。
- 保持 Ground Truth 隔离、fail-closed、人工恢复限制和现有评分阈值。

## 非目标

- 不修改 Benchmark 答案、权重、阈值或 canonical labels。
- 不把 RAG Citation、Alert 文本或 Report 当作根因成立证据。
- 不自动把 open hypothesis 标记为 refuted。
- 不从错误 component/mechanism、外部 Evidence ID 或不完整角色证据生成规范化结论。
- 不新增数据库 schema、第三方依赖、外部服务或自动恢复权限。
- 实现完成后不自动消耗额度重跑真实 Benchmark；真实运行需要单独明确授权。

## 方案比较

### 方案 A：确定性状态权威与 Evidence 规范化（采用）

Hypothesis State 决定 Sufficiency 分类；LLM Candidate 在公开标签和 Evidence 边界通过后，使用
Observation role 与 summary 规范化 trigger/causal chain。Validator 保持严格。

优点是结果稳定、可审计且不依赖文本相似度；代价是必须维护清晰的状态派生与规范化合同。

### 方案 B：语义相似度 Validator

使用 embedding 或第二次 LLM 判断 Candidate 与 Observation 是否语义相同。表达更灵活，但增加
阈值漂移、模型调用、费用和不可重复性，不适合作为安全门。

### 方案 C：只加强 Prompt

要求 LLM 原样复制 Observation。实现简单，但模型仍可能改写，无法解决 Sufficiency 与持久化状态
不一致，也不能提供稳定合同。

## Reuse-first 评估

- 项目已有 `langgraph`、Pydantic v2、LangChain structured output、TypedDict State、条件边和
  Evidence/Hypothesis 数据结构，可直接复用，不新增运行时依赖。
- `langchain-ai/langgraph`（MIT）提供 StateGraph、partial state update、reducer 和 conditional
  routing，继续直接采用其编排原语；领域判定仍由项目内纯函数负责。
- `HolmesGPT/holmesgpt`（Apache-2.0）是活跃的运维调查 Agent，可作为调查流程参考，但没有可直接
  替换本项目持久化 Hypothesis/Evidence 合同的组件，因此仅参考。
- `salesforce/PyRCA`（BSD-3-Clause）提供 Bayesian causal graph RCA，但依赖拓扑、训练数据和概率
  推断，集成成本与当前 Evidence Workflow 不匹配，因此不采用。

结论为：直接复用现有 LangGraph/Pydantic 基础设施，针对本项目公开证据合同做小型自定义纯函数；
不复制外部实现，不新增许可证或供应链风险。

## 设计

### 1. 确定性 Sufficiency 投影

新增一个无 I/O 纯函数，从当前 Hypothesis State 和持久化 Evidence ID 派生：

- `supportedHypotheses`：状态为 `supported` 的公开 hypothesis；
- `refutedHypotheses`：状态为 `refuted` 的公开 hypothesis；
- `unresolvedHypotheses`：状态为 `open` 的公开 hypothesis；
- `status=sufficient`：恰好一个 supported，且不存在 open hypothesis；
- 其他情况为 `status=insufficient`。

LLM Sufficiency 输出仍先经过现有 schema 和 ID allowlist 解析，但其 status 和三类 hypothesis 列表
随后必须被确定性投影覆盖。LLM 的 `recommendedTools`、公开 `summary` 和 `missingEvidence` 仅作为
建议字段：

- 只有确定性状态为 insufficient 时才可保留 allowlisted recommended tools；
- 模型不得通过空 unresolved 列表提前进入 Decision；
- 模型调用或格式失败继续使用同一个确定性投影，不产生另一套状态语义。

### 2. 路由优先级

Sufficiency Gate 使用确定性投影和既有 causal coverage 共同路由：

1. 存在 open hypothesis 时，优先执行尚未运行、`testsHypotheses` 与 open 集合相交的剩余 Plan
   Step；本次 APY-013 会继续执行 `GetDatabaseMetrics`。
2. 没有匹配的剩余步骤且仍有预算时，进入有界 Replanner。
3. 恰好一个 supported、没有 open hypothesis、trigger/mechanism/impact 完整时，进入 Decision。
4. 预算耗尽时允许进入 Decision，但 Validator 保持 fail-closed，不能伪造竞争假设已排除。

路由审计继续保存 `nextRoute`、`refinementReason` 和公开状态列表，不保存模型原始输出。

### 3. Grounded Decision 规范化

Decision structured call 后执行一个确定性规范化步骤。只有同时满足以下条件才允许规范化：

- 恰好一个 supported hypothesis，且不存在 open competitor；
- Candidate component/mechanism 经现有 alias normalization 后与该 hypothesis 的公开 labels 一致；
- Candidate Evidence IDs 全部属于当前任务的 supporting Observation；
- 至少两条独立正 Evidence 和两条 supporting Observation；
- Candidate Evidence 覆盖用于规范化的 Observation Evidence；
- supporting Observation 中恰好一个 trigger，并包含 mechanism 和 impact；
- 角色顺序可组成 `trigger -> mechanism -> context? -> impact`，总长度为 2～6。

满足后只替换：

- `trigger`：唯一 trigger Observation summary；
- `causalChain`：按 trigger、mechanism、context、impact 排序并去重后的 Observation summaries。

保留 Candidate 的 component、mechanism、Evidence IDs 和 confidence，并将来源记为
`llm_grounded_normalization`。如果任何前置条件失败，不做部分修补，交给现有 Validator
fail-closed。

### 4. Validator 边界

Validator 的以下检查保持不变：唯一 supported、无 open competitor、公开标签、当前任务 Evidence、
supporting Evidence、独立正证据、supporting Observation、精确 grounded chain、精确 trigger 和
confidence 范围。

精确文本检查继续存在，但只检查已经完成 Evidence 规范化的公开决策，不再要求 LLM 自由文本偶然
逐字匹配。这样放宽的是表达生成，不是事实门槛。

### 5. 错误与安全处理

- Sufficiency 模型失败：使用确定性状态投影，继续安全路由。
- Open competitor 且无可用工具：在预算内 Replan；预算耗尽后 Candidate 仍会被 Validator 拒绝。
- 多 supported、无 trigger、多 trigger、缺 mechanism/impact：不规范化。
- 错误 public label、外部 Evidence ID、非 supporting Evidence：不规范化。
- LLM Validator 不可用：只有全部确定性检查通过时才允许既有
  `deterministic_grounded_fallback`，Recovery 仍为 manual review 且不得执行。
- 不持久化 Prompt、原始 LLM 响应、异常正文、Ground Truth、Oracle 或原始 CLS 日志。

## 测试设计

采用 RED-GREEN-REFACTOR：

1. LLM 返回 sufficient，但持久化状态仍有 open competitor：必须覆盖为 insufficient，并执行测试
   open hypothesis 的剩余 Plan Step。
2. Observation 正式 refute 所有竞争假设后：确定性状态变为 sufficient，角色完整时进入 Decision。
3. Sufficiency 模型调用失败：仍得到相同确定性分类和路由。
4. 正确 labels/Evidence、但 trigger 和 causal chain 为语义改写：规范化为 Observation summaries，
   随后的全部确定性检查通过。
5. Open competitor、错误 labels、外部或非 supporting Evidence、角色缺失/歧义：不得规范化。
6. Validator 现有逐项 fail-closed 测试保持通过，证明没有降低安全标准。
7. APY-013 Snapshot application 离线回归验证 Plan、Observation、Sufficiency、Decision、Validator、
   Recovery 和 Artifact 全链路；不读取 Ground Truth 到 Agent 路径。

## 验收标准

- Sufficiency payload 的三类 hypothesis 与持久化状态一致，LLM 不能提前关闭 competitor。
- 有相关剩余 Plan Step 时优先执行该步骤，而不是直接进入 Decision。
- 规范化 Candidate 通过原有精确 Validator；错误或证据不完整 Candidate 继续 fail-closed。
- 离线目标测试、Ruff、Pyright 和聚焦 OpenSpec strict 全部通过。
- 不运行全量 pytest，除非用户另行要求。
- 不自动重跑真实 APY-013；若后续授权，只运行一次并保存新的独立 Run。
