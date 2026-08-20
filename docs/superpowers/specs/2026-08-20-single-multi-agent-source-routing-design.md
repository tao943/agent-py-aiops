# Single/Multi-Agent 数据源路由设计

**日期：** 2026-08-20

**状态：** 已确认，待用户审阅

**范围：** 在保留现有 AIOps 单 Agent 主链的前提下，增加可解释的 Single/Multi-Agent Strategy
Router；Multi-Agent v1 只启用 Knowledge、Runtime 和 Log 三类数据源 Investigator，Change
Investigator 仅保留不可用能力描述，不接入 GitHub。

## 1. 背景与当前实现

当前 `evidence-driven-v4` Workflow 已经具备多角色节点，但仍是单 Agent、单共享状态和顺序工具执行：

```text
Planner（先执行 RAG 并生成调查计划）
  -> Executor（每轮执行一个工具）
  -> Fact Adapter（工具输出转公开 Fact 和 HypothesisAssessment）
  -> Sufficiency Gate（继续取证、裁决、重规划或决策）
  -> Decision
  -> Deterministic Validator
  -> Recovery Planner
  -> Validator Router / LLM Validator
  -> Policy Gate
  -> Report
```

系统已经实现当前任务 Evidence 归属、四态假设裁决、因果角色覆盖、Checkpoint、幂等工具执行、
Validator 和恢复权限边界。缺少的是：当一个未知事故同时需要运行状态与事故日志时，Planner 生成的独立
步骤仍由同一个 Executor 顺序执行；系统也不能显式决定“继续单 Agent”还是“升级为数据源多 Agent”。

Nginx Live 正式验收已经证明，可信证据模式命中时单 Agent 可以在不调用 Adjudicator、Replanner 或
LLM Validator 的情况下达到 100 分。因此 Multi-Agent 不能成为默认路径，也不能替换确定性快路径。

## 2. 目标与非目标

### 2.1 目标

1. 由代码拥有的 Strategy Router 在 Single-Agent 与 Multi-Agent 之间做可解释、可审计选择。
2. 默认保持 Single-Agent；只有多个未完成数据源调查可以并行且证据复杂度达到阈值时才升级。
3. Multi-Agent v1 按数据源分工：Knowledge、Runtime、Log。
4. Runtime 与 Log 的独立调查可并行；所有结果通过结构化 EvidencePacket 确定性汇合。
5. 复用现有 Fact Adapter、Sufficiency Gate、Decision、Validator、Policy Gate、PostgreSQL
   Checkpoint 和审计链。
6. 允许 Benchmark 内部强制 Single/Multi 做同场景 A/B，对准确率、证据增益、耗时和成本进行量化。
7. 网络中断、Worker 重启、部分 Agent 超时和重复 Dispatch 不得造成重复模型调用、重复 Evidence 或
   越权恢复。

### 2.2 非目标

- 不让多个 Agent 自由对话、投票或共享私有推理。
- 不按 PostgreSQL、Redis、Nginx 等故障领域拆 Agent。
- 不接入 GitHub Commit、PR、Issue、Actions 或 Deployment API。
- 不启用 Change Investigator；只报告 `deployment_change_source_not_configured`。
- 不把恢复工具暴露给 Investigator。
- 不并行 Planner、Decision、Validator、Recovery Planner、Policy Gate 或 Report 等依赖主链。
- 不改变 Snapshot/Live 评分阈值、Ground Truth 隔离、Validator 标准或恢复权限。
- 不保证 Multi-Agent 必然提升；未通过 A/B 门槛时不得作为默认能力。

## 3. 方案选择

### 3.1 采用：Planner 后路由

第一版保留当前 Knowledge Retrieval 和 Lead Planner，在计划形成后决定 Single/Multi：

```text
START
  -> Knowledge Investigator（封装现有 RAG）
  -> Lead Planner
  -> Strategy Router
       -> Single: 现有 Executor 循环
       -> Multi: Runtime / Log Dispatch 并行
  -> Evidence Aggregator
  -> 现有 Fact Adapter / Sufficiency Gate
```

选择原因：

- Planner 已经拥有 Alert、RAG、公开候选假设、工具合同和 causal intent；路由所需信息更完整。
- Single-Agent 路径可以保持兼容，不要求重写现有稳定图。
- RAG 结果只生成一次，不会被 Planner 和 Knowledge Investigator 重复检索。
- Runtime 与 Log 是当前最有价值且真正独立的剩余数据源，可以获得实际并行收益。

### 3.2 不采用：Router 位于 START 后

该方案可同时启动 Knowledge、Runtime 和 Log，但必须把 RAG 和工具规划从当前 Planner 拆出，让每个
Investigator 独立规划，显著扩大状态、Checkpoint、去重和回归范围，第一版不采用。

### 3.3 不采用：自治 Supervisor

不让 Lead Agent 动态创建任意 Sub-Agent，也不让 Agent 自由委派。Investigator 类型、工具、并发、
deadline 和模型预算都由代码拥有的能力注册表控制。

## 4. 图结构与执行顺序

### 4.1 目标图

```text
Knowledge Investigator
        |
Lead Planner
        |
Strategy Router
   +----+----------------+
   |                     |
Single-Agent          Multi-Agent
   |                     |
现有 Executor       Dispatch Runtime / Log
   |                 +---+---+
Fact Adapter         |       |
   |              Runtime   Log
   |                 |       |
   |                 +---+---+
   |                     |
   |             Evidence Aggregator
   +----------+----------+
              |
       Sufficiency Gate
          +---+---+
          |       |
       Decision  下一轮路由/人工复核
          |
Validator -> Recovery -> Policy -> Report
```

### 4.2 Knowledge Investigator

Knowledge Investigator 是现有 RAG 调用的明确角色边界，继续使用 Milvus 向量召回、BM25L、RRF、
rerank、Citation、owner/knowledge-base 隔离和 Redis Retrieval Cache。默认不增加 LLM 调用。

只有多个高相关 SOP 相互冲突且确定性字段无法表达差异时，才允许一次受预算控制的模型归纳；该输出仍是
`reference`，不能单独支持根因或关闭竞争假设。

### 4.3 Lead Planner

Lead Planner 继续生成带工具合同、候选假设、causal intent 和 evidence rules 的计划。每个 Plan Step
新增代码推导或严格枚举的 `sourceDomain`：

```json
{
  "id": "step-1",
  "tool": "SearchLog",
  "sourceDomain": "log",
  "purpose": "Confirm incident-window error events.",
  "testsHypotheses": ["upstream_timeout"],
  "causalIntent": "trigger"
}
```

模型不能创建未知 source domain。缺失或非法 domain 由工具能力注册表重新计算；无法映射的步骤继续走
Single-Agent 串行路径并记录安全原因。

### 4.4 Runtime Investigator

Runtime Investigator 只能执行注册为 runtime source 的只读诊断工具，例如健康、连接、锁、延迟、容量
和资源状态。它不读取 RAG、CLS 或 GitHub，不持有恢复工具。

Planner 已提供合同有效步骤时直接执行，不新增 LLM。只有结构化结果无法被 Fact 规则解释、多个 runtime
结果语义冲突或需要形成新的公开候选假设时，才允许一次 LLM 调用。

### 4.5 Log Investigator

Log Investigator 只能调用用户启用的 CLS `SearchLog` 和允许的事故日志工具，必须保持 run、incident、
时间范围和 owner 作用域。结构化事件、错误码和时间线优先由代码提取。

只有非结构化日志、多错误模式或确定性时间线无法解析时，才允许一次 LLM 调用。当前状态与事故窗口状态
必须分开标记，不能用“当前健康”反驳事故时异常。

### 4.6 Change Investigator 占位

能力注册表保留 `change` 类型，但第一版状态固定为：

```json
{
  "investigatorType": "change",
  "available": false,
  "reasonCode": "deployment_change_source_not_configured"
}
```

Router 不得选择不可用 Investigator。第一版可额外记录 Git SHA、image digest 和 config fingerprint 作为
Benchmark provenance，但它们不构成 Change Investigator，也不参与根因裁决。

## 5. Strategy Router

### 5.1 三种策略状态

```text
deterministic_fast_path
single_agent
multi_agent
```

`deterministic_fast_path` 表示现有公开事实已经命中可信模式或 Decision 已充分；它不再发出 Investigator。
初始 Planner 后路由通常只能选择 Single/Multi；每轮 Evidence 汇合后的动态复评可以进入 fast path。

### 5.2 硬门禁

以下任一条件成立时不得升级 Multi-Agent：

- trusted pattern 已命中；
- `decisionReady=true`；
- 少于两个尚未完成且可以独立并行的数据源 Dispatch；
- 剩余时间不足以完成最慢 Investigator deadline 与 aggregation reserve；
- 剩余模型预算不足以保留强制 Lead/Report 调用；
- 对应 Investigator 不可用；
- 相同 dispatch key 已完成且 Evidence snapshot 未变化；
- Multi-Agent 被配置关闭。

以下情况可在安全/预算门禁通过后直接升级：

- 高质量证据冲突，且至少两个数据源可以验证冲突；
- 两个以上 causal component，同时需要三类以上数据源；
- P0/P1、故障类型未知且存在已配置的近期发布信号；第一版没有 Change source，因此该条件不会单独成立；
- Single-Agent 已执行两个有效工具，但没有 Hypothesis 状态或 causal coverage 增益。

### 5.3 可解释评分

分值只用于没有硬门禁时的初始选择和动态升级：

| 特征 | 条件 | 分值 |
| --- | --- | ---: |
| 多数据源需求 | 需要 2 类数据源 | +1 |
| 多数据源需求 | 需要 3 类及以上，与上一项互斥 | +3 |
| 跨组件故障 | 涉及 2 个及以上 causal component | +2 |
| 根因歧义 | unresolved hypothesis 不少于 3 个 | +1 |
| 最近变更 | 已配置来源证明事故窗口内存在相关部署 | +2 |
| 高质量冲突 | 直接证据对同一时空 claim 产生冲突 | +3 |
| 高严重度 | P0/P1 或 critical | +2 |
| 因果链缺口 | trigger/mechanism/impact 缺少 2 类及以上 | +2 |
| 调查停滞 | 2 个有效工具后没有假设状态变化 | +3 |
| 无知识命中 | 没有可信 SOP 或相似案例 | +1 |

第一版没有 Change/Deployment 数据源，因此“最近变更”特征固定为 0，不允许从 PR 标题、用户描述或模型
猜测中生成该分值。该特征只为后续兼容保留，不影响 v1 路由。

路由区间：

```text
0..3  -> single_agent
4..5  -> single_agent + escalation_watch
>= 6  -> multi_agent
```

阈值必须由版本化配置承载，初始值不是最优性声明。Router 只保存 reason code 和必要的公开计数，不能保存
私有推理。

### 5.4 动态复评

Router 至少在以下时机复评：

1. Planner 生成初始计划后；
2. Single-Agent 完成两个有效工具或计划耗尽后；
3. Multi-Agent fan-in 后；
4. 第二轮前或 soft deadline 临近时。

动态升级必须证明仍存在尚未执行、合同有效、能覆盖当前 Evidence gap 的其他数据源步骤。没有有效搜索
空间时进入 Decision 或人工复核，不能仅因分数高重复启动 Agent。

### 5.5 路由审计

每次选择保存：

```json
{
  "strategy": "multi_agent",
  "score": 8,
  "reasonCodes": [
    "three_evidence_domains_required",
    "causal_roles_missing",
    "investigation_stagnated"
  ],
  "selectedInvestigators": ["runtime", "log"],
  "rejectedInvestigators": {
    "knowledge": "already_completed",
    "change": "deployment_change_source_not_configured"
  },
  "policyVersion": "investigation-router-v1",
  "remainingTimeMs": 92000,
  "remainingModelCalls": 4
}
```

Router 禁止读取或匹配 Scenario ID、Run ID、Ground Truth、Oracle、评分规则或固定 fixture 值。

## 6. Investigator 与 EvidencePacket 合同

### 6.1 Dispatch 输入

```json
{
  "taskId": "diagnostic_xxx",
  "dispatchId": "dispatch_xxx",
  "investigatorType": "runtime",
  "objective": "Validate gateway and upstream runtime state.",
  "testsHypotheses": ["upstream_timeout", "gateway_pressure"],
  "missingCausalRoles": ["mechanism", "impact"],
  "allowedTools": ["ProbeLiveEvalUpstream", "InspectNginxRequestTimeline"],
  "existingEvidenceIds": [],
  "deadlineMs": 30000,
  "modelCallBudget": 1
}
```

目标、工具、假设和 Evidence snapshot 均来自当前任务公开状态。Investigator 不接收其他 Agent 的私有
推理、恢复工具或 evaluator 私有内容。

### 6.2 EvidencePacket 输出

```json
{
  "dispatchId": "dispatch_xxx",
  "investigatorType": "runtime",
  "status": "completed",
  "claims": [
    {
      "claimId": "upstream_connection_succeeded",
      "value": true,
      "quality": "direct",
      "causalRole": "mechanism",
      "supports": ["upstream_timeout"],
      "refutes": ["upstream_unavailable"],
      "evidenceIds": ["evidence_xxx"],
      "targetComponent": "live-eval-upstream",
      "observedAt": "2026-08-20T10:00:00Z",
      "timeScope": "incident_window"
    }
  ],
  "limitations": ["Only the requested runtime scope was inspected."],
  "toolCallIds": ["tool_call_xxx"],
  "modelCallsUsed": 0
}
```

允许 `completed`、`inconclusive`、`failed` 和 `timeout`。`inconclusive` 可以没有 claim；失败或缺失不能
被解释为反证。

### 6.3 证据质量

- runtime/log 工具产生且可验证的观测可以是 `direct`；
- Knowledge/RAG 固定为 `reference`；
- 未启用 Change 的相关性证据第一版不存在；
- Agent 不能自行提高工具结果质量；质量由工具能力和 Fact allowlist 决定。

## 7. Evidence Aggregator

Aggregator 是确定性代码，不调用 LLM：

```text
EvidencePacket[]
  -> owner/task/dispatch 校验
  -> Evidence ID 与 Tool Audit 校验
  -> Claim 规范化
  -> 去重
  -> 时空作用域冲突检测
  -> 现有 Fact Adapter
  -> HypothesisAssessment / causal coverage
```

各并行 Investigator 只写各自的 Dispatch checkpoint、工具审计、Evidence 和 Packet，不得直接更新共享
`diagnostic_facts`、`hypothesis_assessments` 或 `observation_decisions`。Aggregator 是 fan-in 后唯一的共享
状态写入者，按稳定 dispatch/type/evidence 顺序规范化 Packet，再一次性提交合并结果，避免并行 reducer
覆盖和到达顺序影响结论。

去重 fingerprint：

```text
claim_id
+ canonical_value
+ source_type
+ target_component
+ time_scope
+ evidence_ids_hash
```

同一 Evidence 被多个 Agent 引用不能重复计为独立证据。只有 claim、组件、时间范围和观测范围一致且值冲突
时，才形成高质量冲突；“事故时异常、当前健康”不是自动冲突。Aggregator 不能裁决冲突，继续使用现有
Adjudicator 或人工复核。

以下 Packet 必须拒绝并记录 `invalid_evidence_packet`：

- 跨 owner、跨 task 或不存在的 Evidence ID；
- 未授权工具或未完成 Tool Audit；
- Schema 不合法或包含禁止字段；
- 把 reference 伪装为 direct；
- 缺少必要时空作用域；
- 声称执行恢复动作。

## 8. 并发、预算与时间

建议初始配置：

```json
{
  "singleAgentMaxInitialSteps": 2,
  "multiAgentThreshold": 6,
  "escalationWatchThreshold": 4,
  "collectorConcurrency": 4,
  "llmInvestigatorConcurrency": 2,
  "maximumInvestigatorsPerWave": 3,
  "maximumInvestigationWaves": 2,
  "maximumRuntimeStepsPerDispatch": 3,
  "maximumLogQueriesPerDispatch": 2,
  "maximumKnowledgeQueriesPerDispatch": 2,
  "maximumModelCallsPerInvestigator": 1
}
```

第一版最多有三个数据源角色，但 Knowledge 已先完成，因此 Multi fan-out 通常只有 Runtime 与 Log 两个并行
Dispatch。确定性采集器可以并行；共享模型调用受 semaphore 限制为 2。单个 Investigator 内存在参数或
结果依赖的工具仍须顺序执行。

Multi-Agent 只在以下预算不等式成立时启动：

```text
remaining_time >= max(selected_agent_deadlines) + aggregation_reserve
remaining_model_budget >= optional_investigator_calls + mandatory_lead_calls
```

Trusted Pattern 或 `decisionReady` 成立后取消未开始 Dispatch。已经发出的外部请求允许完成，但完成态之后
到达的结果记录 `late_result_ignored`，不修改 Decision、Report 或评分 Artifact。

并行 Investigator 的内部完成顺序不构成公共 SSE 顺序。Coordinator 为 Dispatch 分配稳定 sequence；实时
进度事件携带 dispatch ID，fan-in、Decision、Report 和 `complete` 仍遵守现有任务级终止顺序。重放时按
持久化 sequence 发出，不能按协程完成时间重新排序终态事件。

## 9. 幂等与中断恢复

第一版不新增数据库表，复用现有 execution checkpoint、Diagnostic Step、Evidence、Tool Call Audit 和
ExecutionCoordinator。

稳定 Dispatch key：

```text
task_id
+ router_policy_version
+ investigator_type
+ objective_hash
+ evidence_snapshot_hash
```

要求：

- 同一 key 的 completed Dispatch 直接读取原 EvidencePacket；
- failed/timeout 重试记录新的 attempt，但属于同一逻辑 Dispatch；
- Agent 内工具继续使用现有 tool call 幂等键；
- Aggregator 可安全重放，但 Evidence 和 audit event 使用稳定 ID 去重；
- Worker 重启后只恢复未完成 Dispatch；
- PostgreSQL 唯一约束是最终正确性保障，不以 Redis 锁作为唯一保证。

新增审计阶段：

```text
knowledge_investigator
strategy_router
investigator_dispatch
runtime_investigator
log_investigator
evidence_aggregator
strategy_fallback
```

## 10. 失败与降级

### 10.1 部分失败

已完成 Packet 正常汇合；失败 Agent 记录安全错误分类。Sufficiency Gate 判断是否只重试缺失数据源、进入
第二轮、继续 Decision 或人工复核。失败绝不能被解释为“没有异常”。

### 10.2 全部失败

剩余时间和预算足够时记录 `fallback_to_single_agent` 并恢复现有 Executor；不足时进入 manual review，
不得生成无证据根因。

### 10.3 数据源不可用

Router 在选择前读取能力状态。CLS、RAG 或 runtime MCP 不可用时不生成虚假 Dispatch；若关键数据源缺失，
保存 `source_unavailable` 并由 Sufficiency/Policy fail closed。

### 10.4 模型失败

Investigator LLM 不是工具采集成功的必要条件。模型失败时保留已验证工具 Evidence，Packet 标记 limitation；
只有 Schema 合法且引用当前任务 Evidence 的模型增量可以进入 Aggregator。

## 11. 安全边界

- Investigator 工具由类型白名单和现有 schema constraint 双重限制；
- 不暴露 proposal-only、external-policy-required 或其他恢复工具；
- 所有数据库、Milvus、CLS、MCP 和审计读取保持 owner/task/tenant 作用域；
- Packet、Step、Checkpoint 和 SSE 不保存私有推理、Prompt、模型原文、凭据或原始敏感日志；
- Agent、RAG、Router、Aggregator 和 Report 均不能读取 Ground Truth/Oracle；
- Multi-Agent 不改变 Deterministic Validator、LLM Validator Router 或 Policy Gate 的执行权限；
- 普通诊断 API 不允许客户端强制 strategy；强制 Single/Multi 仅属于内部 Benchmark CLI。

## 12. 测试与 A/B Benchmark

### 12.1 Router 测试

- 0..3 Single、4..5 watch、>=6 Multi；
- hard gate 优先于评分；
- Change 不可用且不被选择；
- 相同公开输入产生相同 route 和 reason code；
- Router 不读取 Scenario/Run/Oracle/Ground Truth；
- 时间、模型预算和并行数据源不足时不升级；
- 动态停滞可从 Single 升级 Multi；
- Trusted Pattern/decisionReady 终止额外 Dispatch。

### 12.2 Investigator 与 Aggregator 测试

- 三类工具权限和 Evidence quality；
- timeout、retry、idempotency、部分/全部失败；
- 并发真实重叠而非顺序伪装；
- 顺序无关的 fan-in；
- 重复 Evidence 去重；
- 时空冲突检测和当前健康/事故异常区分；
- 跨任务、伪造 direct、恢复声明和非法 Packet 拒绝；
- Worker 重启、重复保存和 PostgreSQL unique conflict 安全恢复；
- late result 不修改已完成 Decision。

### 12.3 回归

现有 10 Snapshot、4 Live、Retrieval、答案隔离、Validator、恢复安全和结果持久化门禁不得降低。可信模式
场景应继续自动选择 Single/Fast Path，证明 Multi-Agent 不会无意义启动。

### 12.4 A/B 模式

生产固定 `strategy=auto`。内部 Benchmark CLI 允许：

```text
strategy=single
strategy=multi
```

覆盖不进入普通 API。A/B 必须固定场景、故障注入、模型配置、知识库版本、CLS 时间范围、工具白名单和
评分器，比较：

- Root Cause Top-1；
- Required Evidence Recall；
- Differential Diagnosis；
- trigger/mechanism/impact coverage；
- Citation Grounding；
- wall-clock duration；
- model/tool call count；
- new Evidence per Agent；
- duplicate Evidence rate；
- routing accuracy 和 fallback rate。

## 13. 验收标准

### 13.1 安全与兼容

- 现有 10 Snapshot、4 Live 不降分；
- Single-Agent 路径行为兼容，性能回退不超过 5%；
- 没有跨用户、跨任务 Evidence；
- Multi-Agent 不获得恢复权限；
- 答案隔离、Validator 和 Policy Gate 全部保持；
- 失败和网络恢复结果均持久化且可审计。

### 13.2 性能

```text
Multi-Agent P95 duration <= Single-Agent P95 * 1.5
extra model calls per diagnosis <= 2
duplicate Evidence rate <= 10%
```

两个确定性 Investigator 并行的目标是不超过同任务 Single-Agent 的 1.2 倍；该值是验收目标，不是未测试的
性能声明。

### 13.3 能力增益

在专门的跨数据源保留集上至少满足一项：

```text
Required Evidence Recall 提升 >= 10 个百分点
或 Root Cause Top-1 提升 >= 5 个百分点
```

同时不得降低安全得分。若未达到，Multi-Agent 只能保留为实验策略，不能默认启用，也不能在简历中写入
未验证的提升比例。

## 14. 实施顺序与变更管理

1. 在实现前完成 reuse-first：核对现有 LangGraph、ExecutionCoordinator、并发 reducer 和 GitHub
   参考实现；不新增依赖时优先 wrapped adoption 或 reference only。
2. 先固化 Router、Dispatch、EvidencePacket 和 Aggregator 的 OpenSpec 合同。
3. 先实现 `strategy=single` 兼容路径和 Router 审计，再实现 Runtime/Log fan-out。
4. 加入幂等、部分失败、超时、重放和安全测试。
5. 跑现有目标回归，再运行内部 Single/Multi A/B；不以降低阈值换取通过。
6. 实测达标后再决定是否默认启用 auto escalation。

实现必须在独立计划中拆分，不得与尚未完成的通用 Replanner 路由优化混为一个不可独立验收的提交。
私有配置、API Key、CLS 凭据、原始日志、Archive 和 `var/` 产物不得提交。
