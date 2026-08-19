# Nginx Live 确定性裁决与 LangGraph 路由优化设计

**日期：** 2026-08-20  
**状态：** 已确认，待实施  
**范围：** 先修复 `APY-LIVE-NGINX-TIMEOUT-001`，真实验收通过后再优化通用路由

## 1. 目标

本变更分为两个顺序执行、独立验收的切片：

1. 让 Nginx upstream timeout Live 场景仅依据真实公开证据稳定形成唯一根因，记录
   `proposal_only` 缓解提案，并通过真实 LLM、CLS、恢复合同、Verify 和 Cleanup 验收。
2. 在不改变同步报告语义、不并行工具、不降低评分阈值的前提下，减少已经被确定性证据闭合的
  任务进入 LLM Adjudicator，以及没有可执行补证步骤时进入 LLM Replanner 的无效调用。

本变更不把 Nginx 配置修改、重载或自动恢复加入白名单，不改变现有 Snapshot/Live 评分规则，
也不把 Benchmark Oracle、场景编号或固定运行数值暴露给 Agent。

## 2. 已证实的问题

最新 Nginx Live Run `accept-apy-live-nginx-timeout-001-1787147042` 的全部只读工具调用成功：

- `ReadNginxTimeoutSummary`：确认 gateway timeout 和 read deadline elapsed；
- `InspectNginxRequestTimeline`：确认 HTTP 504、请求持续时间和 upstream connect succeeded；
- `ProbeLiveEvalUpstream`：确认 upstream HTTP 200 且健康；
- `SearchLog`：确认同一 Run、Scenario、Incident 下的 `upstream_timeout`；
- `knowledge_retrieval`：返回通用 Nginx timeout 排查知识。

但 `_derive_nginx_timeout_observations` 只在某个假设已经成为唯一 `supported` 后才运行；而当前
唯一能把该假设推进为 `supported` 的路径又依赖 Adjudicator。该循环依赖导致四个候选假设均为
`unresolved`。Adjudicator 第一次返回未通过批量裁决合同，第二次 60 秒超时；Replanner 随后没有
产生新步骤，最终形成 `rootCauseDecision=null`、`recoveryPlan=no_action` 和
`proposal_denied`。

## 3. 复用评估

### 3.1 项目内部

直接复用现有组件：

- `DiagnosticFact` 和 secret-filtered Fact Adapter；
- `HypothesisAssessment` 四态模型；
- 代码拥有的 trusted evidence-rule template；
- `reduce_hypotheses`、`assess_sufficiency` 和 causal coverage；
- Deterministic Validator、Validator Router、Policy Gate；
- PostgreSQL execution/checkpoint 幂等协调器；
- Nginx proposal-only MCP 工具合同和 Live Recovery Service。

### 3.2 GitHub 检索

检索了以下候选：

- `langchain-ai/langgraph`：MIT、活跃维护，提供 conditional edge 和 `Send` fan-out；当前项目已直接
  使用。它适合保留图编排，但不负责 Nginx 领域证据的因果裁决。
- `venmo/business-rules`：MIT、包含测试，可运行 JSON 规则；会引入新的规则 DSL 和任意规则注入面。
- `jruizgit/rules`（durable rules）：MIT、包含 Python tests，面向状态化事件规则；对单次诊断的
  有界事实归约过重，并可能带来额外运行时和供应链成本。

仓库未检测到明确的项目许可证文件，因此本变更不复制外部实现，也不增加外部依赖。LangGraph
仅作为已采用的图能力继续直接使用；Nginx 裁决使用项目现有领域模型做自有扩展。

## 4. 设计原则

### 4.1 规则识别证据模式，不识别 Benchmark 身份

确定性规则只能消费规范化公开事实，不能读取或匹配：

- `scenario_id`、`run_id`、Benchmark ID；
- `ground_truth.yaml`、Oracle、评分规则；
- 本次运行的固定时长、PID、容器名或其他 fixture 特征；
- RAG 中的场景答案或场景专属标签。

允许使用公开候选假设 ID，因为候选集合本来就是 Agent 可见的诊断输入；禁止以候选 ID 的存在本身
作为支持证据。

### 4.2 只有完整的同请求证据模式才能确定性关闭

Nginx upstream response timeout 的受信模式要求以下事实同时存在：

1. gateway 对目标请求返回 HTTP 504；
2. gateway 已成功建立 upstream 连接；
3. gateway 明确观测到 read deadline elapsed；
4. upstream 独立健康探针返回 HTTP 200 且 healthy；
5. 同一 incident 的公开日志包含 `upstream_timeout`。

事实值可以来自不同工具，但必须由当前任务的持久化 Evidence ID 支撑。规则不比较固定毫秒数，
只使用 deadline 是否达到这一可信布尔事实。

完整模式成立时允许产生以下四态转换：

- `nginx_upstream_response_timeout` → `supported`；
- `nginx_route_mismatch` → `refuted`，依据 upstream connection succeeded；
- `nginx_upstream_unavailable` → `refuted`，依据独立 HTTP 200/healthy 探针；
- `nginx_gateway_pressure` → `causally_inactive`，依据同一请求已明确进入已连接 upstream 的
  read-deadline 等待阶段，且故障事件为 `upstream_timeout`。

每个关闭状态必须保存它自己的 Evidence ID；不能仅以“timeout 已解释故障”关闭竞争项。任一关键事实
缺失、证据不属于当前任务或存在相反的高质量事实时，模式不得触发，候选项保持 `unresolved` 或冲突，
继续走 Adjudicator/人工复核路径。

### 4.3 因果观察不再依赖 LLM 先选中答案

触发完整受信模式后，Fact Adapter 在同一确定性归约中生成：

- trigger：upstream 响应等待达到 proxy read deadline；
- mechanism/context：upstream connection succeeded，gateway 正在等待响应；
- impact：gateway 返回 HTTP 504，同时 upstream 健康端点仍可用。

这些 Observation 使用 `assessmentSource=deterministic` 和可信模板来源，不再标记为
`llm_adjudicated`。Sufficiency Gate 只有在恰好一个 supported、所有竞争项有独立关闭证据、
trigger/mechanism/impact 完整且证据数量满足现有门禁时，才直接路由到 Decision。

## 5. 阶段一：Nginx Live 修复

### 5.1 数据流

```text
真实 Nginx/Upstream/CLS 观测
  -> 工具输出
  -> secret-filtered DiagnosticFact
  -> 受信 Nginx timeout 模式
  -> 四态 HypothesisAssessment + 因果 Observation
  -> Sufficiency Gate
  -> deterministic Decision
  -> Deterministic Validator
  -> Recovery Planner
  -> 按风险调用 LLM Validator
  -> Policy Gate 记录 proposal-only 工具调用
  -> 同步 LLM Report
  -> Live proposal Verify + Cleanup + Score
```

### 5.2 恢复边界

`ProposeNginxTimeoutMitigation` 继续是唯一允许的 Nginx recovery tool，且策略固定为
`proposal_only`：

- `executionPermitted=false`；
- `humanApprovalRequired=true`；
- 不修改或重载 `nginx.conf`；
- 不执行 shell、Docker 或远程写操作；
- proposal 参数必须通过已发现的 MCP Schema；
- proposal tool audit、Evidence 引用和稳定 recovery intent ID 必须持久化。

### 5.3 失败行为

- 缺少任一关键事实：保持 unresolved，不生成确定性根因；
- 出现相反的健康、连接、状态或压力事实：保留冲突并 fail closed；
- proposal 结构不合法：Policy Gate 拒绝；
- LLM Validator 不可用：沿用现有安全降级，不扩大执行权限；
- Verify 或 Cleanup 失败：Live Run 失败并保存分类结果。

## 6. 阶段二：通用 LangGraph 路由优化（范围 A）

只有阶段一真实 Nginx 验收通过后才实施本阶段。

### 6.1 跳过不必要的 Adjudicator

Sufficiency Gate 在以下条件同时成立时直接进入 Decision：

- trusted reducer 已产生唯一 supported；
- 所有活跃竞争项均有当前任务 Evidence 支撑的关闭状态；
- causal coverage 和独立正向证据达到现有门禁；
- 没有高质量冲突。

Adjudicator 仅处理确定性规则未覆盖且仍有真正语义歧义的公开假设。不得仅因为工作流版本为 v4 或
存在多个初始候选项而调用 Adjudicator。

### 6.2 在调用 LLM 前拒绝无用 Replan

Replanner 调用前先由代码计算是否存在至少一个尚未执行、参数合同有效、能够覆盖当前 evidence gap 的
工具步骤。若没有候选步骤：

- 不调用 Replanner 模型；
- 保存 allowlisted `no_useful_step` 原因；
- 有 grounded candidate 时进入 Decision；
- 无 grounded candidate 时进入确定性 manual review。

该优化不降低最大步骤、最大 replan、模型预算或 deadline，也不把已执行工具重新加入计划。

### 6.3 明确不在本阶段做的事项

- 不并行诊断工具；
- 不异步化或跳过 LLM Report；
- 不改变任务完成和报告可见时机；
- 不更换 Planner、Adjudicator、Validator 或 Report 模型；
- 不改变 LLM Validator 的风险路由；
- 不降低 Live/Snapshot 评分阈值。

## 7. 测试设计

所有行为修改按 RED-GREEN-REFACTOR 实施，先观察目标测试因缺少能力而失败。

### 7.1 正向合同

- 跨多个 Evidence 的完整 Nginx timeout 模式产生唯一 supported 和三个有证据关闭的竞争项；
- trigger、mechanism、impact 均来自持久化 Evidence；
- Sufficiency 直接路由 Decision，模型审计中不存在 Adjudicator/Replanner；
- Recovery Planner 产生 `ProposeNginxTimeoutMitigation` proposal；
- Policy Gate 记录 proposal，保持 `executionPermitted=false`；
- Nginx Live 评分 100、`VALID_PASS`、Verify 和 Cleanup 均通过。

### 7.2 反事实与防过拟合

- HTTP 504 但 upstream connection failed：不得支持 response timeout；
- upstream probe 非 200 或 unhealthy：不得反驳 upstream unavailable；
- 没有 read deadline elapsed：保持 unresolved；
- 缺少 incident `upstream_timeout` 日志：保持 unresolved；
- 存在明确 gateway pressure 高质量事实：保留冲突并 fail closed；
- 改变 duration、Scenario ID、Run ID：裁决只随证据语义变化；
- 同一告警、不同证据组合：产生不同假设状态；
- Agent、Prompt、RAG、报告和工具均不可读取 Oracle；
- proposal 参数不符合 Schema：Policy Gate 拒绝。

### 7.3 回归范围

不运行全量 pytest。至少运行：

- v4 hypothesis/fact/sufficiency/decision 目标测试；
- Nginx proposal 与 Live 合同测试；
- Live diagnostic adapter 与评分目标测试；
- 与 PG Lock、PG Deadlock、Redis Maxclients 共享 reducer 的目标回归；
- Ruff 和 Pyright；
- OpenSpec strict/all 校验；
- 最后只运行一次正式 Nginx LLM+CLS Live 验收，失败立即 Cleanup 并停止。

## 8. 验收标准

### 阶段一

- 正式 Nginx Run 使用当前配置的真实主模型、独立 Validator、30 卡知识库和 CLS；
- 产生非空 grounded `rootCauseDecision`，mechanism 为
  `upstream_response_exceeded_proxy_read_timeout`；
- Recovery 为 `proposal_only`，proposal 已记录且要求人工审批，没有执行配置写入；
- 得分 100、`VALID_PASS`、Verify 和 Cleanup 均成功；
- Evaluation Run 关联 `diagnostic_task_id`，artifact checksum 存在；
- Nginx 配置在 Run 前后无差异；
- 结果进入 PostgreSQL 和外部 Archive，history audit 无 conflict/pending。

### 阶段二

- 阶段一证据模式不调用 Adjudicator 或 Replanner；
- 无可用 gap-targeted step 时不调用 Replanner 模型；
- 真正存在未解决语义歧义时仍调用 Adjudicator；
- 同步 Report 和现有 API/SSE 完成语义保持不变；
- PG、Redis 和 Snapshot 目标回归行为不变；
- 记录优化前后模型角色、模型调用次数和总耗时；绝对耗时只作供应商波动下的观测值，结构性减少
  Adjudicator/Replanner 调用是硬验收条件。

## 9. 文档与变更管理

实现时更新活动 OpenSpec change `add-auditable-hypothesis-adjudication` 的 design、delta spec 和 tasks，
使其反映实际完成状态；同步 `docs/aiops/agentpy-domainbench.md` 的 Nginx 最终验收记录、模型调用
角色和性能对比。真实日志、私有配置、API Key、CLS 凭据、Archive 和 `var/` 产物不得提交。
