# Order Pool Specialist Multi-Agent 设计

**状态：** 设计已确认，尚未开始实现

**日期：** 2026-08-21

**范围：** 以 `APY-LIVE-ORDER-POOL-LEAK-001` 为首个垂直切片，底层 Specialist 合同保持通用

## 1. 问题背景

PostgreSQL session scope 截断修复后，Order Pool Live 场景已经可以通过故障注入并进入生产
AIOps 诊断链路。修复后的首个 Single canary 完成了 4 次工具调用并提取 26 个公开 Fact，
但没有生成根因决策：

- 5 个公开候选假设全部保持 `unresolved`；
- `independentPositiveEvidenceCount=0`；
- trigger、mechanism、impact 三类因果角色全部缺失；
- LLM Adjudicator 两次尝试耗尽，未接受任何 assessment；
- Deterministic Validator 没有收到 grounded decision；
- Recovery Policy 以 `order_pool_decision_required` 正确拒绝恢复。

当前 Fact Adapter 只有在 `order_connection_lifecycle_failure` 已经是 `supported` 时，才能投影
完整的 Order Pool 因果链。现有 code-owned trusted compound pattern 只支持 Nginx timeout，
没有 Order Pool resolver。因此形成循环依赖：因果链要求 hypothesis 已 supported，而 hypothesis
能否 supported 又依赖 LLM Adjudicator。

项目当前已有步骤级 Strategy Router 和 Investigator dispatch，但没有持续存在的 Runtime/Log
Specialist、局部计划、隔离 checkpoint、有界 Evidence Analysis 和类型化聚合边界。

## 2. 目标

1. 在引入 Multi-Agent 前，先修复当前 Single-Agent 的确定性证据闭环。
2. 为 Order Pool 增加 opt-in Runtime Specialist 与 Log Specialist。
3. 每个 Specialist 具备有界的模型驱动 Local Planning 和 Evidence Analysis。
4. 多 Agent 共享不可变事故上下文和结构化证据，不共享私有推理或可变局部状态。
5. SpecialistResult 先经过确定性 Aggregator，再进入现有 Fact Adapter、Adjudicator、Decision、
   Validator 与 Recovery Policy。
6. 保留当前 Single 路径、评分、恢复授权、Oracle 隔离和不可变评测历史。
7. 在启用自动路由前，通过持久化的真实 Single/Multi A/B 验证能力收益。

## 3. 非目标

- 第一版不启用生产 `auto` Multi-Agent 路由。
- 不增加超过两个 Specialist。
- Specialist 不得执行或授权恢复。
- 不允许通过 Specialist 投票直接决定根因。
- 不暴露 ground truth、Prompt、原始模型响应、私有 Chain-of-Thought、凭据或未作用域化 CLS 日志。
- 第一版不接入所有 Live 场景。
- 不替换 LangGraph、PostgreSQL checkpoint、现有 Validator 或评分器。
- 不把 Redis 作为最终正确性或幂等边界。

## 4. 交付顺序

必须按以下顺序实施：

```text
修复 Order Pool Single 确定性闭环
  -> 一次成功的真实 Single canary
  -> 通用 Specialist 合同和持久化
  -> Runtime/Log Specialist
  -> 确定性 Evidence Aggregator
  -> forced Multi Order Pool
  -> 真实 Single/Multi A/B
  -> shadow routing
  -> 单独批准后才考虑生产 auto routing
```

不得通过绕过或放宽当前 `deterministic_gap` 来开始 Multi-Agent 实现。Single 修复是后续切片的
发布门禁。

## 5. 目标架构

### 5.1 当前链路

当前 Main Planner 生成完整工具步骤；Strategy Router 与 Executor 以 Plan Step 为单位执行，
每完成一步就返回中央 Fact Adapter 与 Sufficiency Gate：

```text
Main Planner
  -> Strategy Router
  -> 一个全局 Plan Step
  -> Executor / Investigator dispatch
  -> Fact Adapter
  -> Sufficiency Gate
  -> 下一步、Adjudicator、Replanner 或 Decision
```

这属于步骤调度，不等于拥有持续局部状态和完整 Investigation Result 的 Specialist Agent。

### 5.2 新链路

```text
Main Planner
  -> Strategy Router
       |-- single -> 现有 Single 链路
       |
       `-- multi
            |-- Runtime Specialist
            |     -> Local Planner
            |     -> Runtime Tools
            |     -> Evidence Analysis
            |     -> SpecialistResult
            |
            `-- Log Specialist
                  -> Local Planner
                  -> scoped CLS Tools
                  -> Evidence Analysis
                  -> SpecialistResult
                         |
                         v
                 Evidence Aggregator
                         |
                         v
                 现有 Fact Adapter
                         |
                         v
                 Deterministic Pattern
                         |
                         v
                 必要时 LLM Adjudicator
                         |
                         v
                 Decision -> Validator -> Recovery Policy
```

Strategy Router 在 Multi 模式下升级为调查编排器，但完整保留现有 Single 行为。

## 6. Slice 0：修复 Single 确定性闭环

新增 code-owned Order Pool compound evidence resolver。它只能消费公开、可信、run-scoped Fact，
不得读取 Scenario ID、Oracle、ground truth、预期分数或 evaluator-only 状态。

只有下列模式完整成立时，才能支持 `order_connection_lifecycle_failure`。

### 6.1 Trigger

- incident-scoped CLS 生命周期包含 `connection_checkout`；
- checkout 后出现 `order_update_failed`；
- 后续出现 `pool_acquire_timeout`；
- 被保留的 fault lifecycle 中没有匹配的成功 `connection_checkin`。

### 6.2 Mechanism

- `poolAtCapacity=true`；
- `freeConnections=0`；
- `waiterObserved=true`；
- 当前 Run 的 PostgreSQL sessions 存在。

### 6.3 Rule-out 与 Context

- PostgreSQL 仍可达；
- 没有 PostgreSQL lock wait；
- Business acquisition probe 超时。

完整模式成立时，resolver 必须：

- support `order_connection_lifecycle_failure`；
- refute `order_database_unreachable`；
- refute `order_database_lock_wait`；
- 没有独立正证据时不能支持 `order_slow_statement`；
- 区分连接未释放与普通流量容量压力；
- 使用相互独立的 Evidence ID 形成 trigger、mechanism、impact。

任一必要 Fact 缺失、冲突、不可信、跨 Run 或来自重复底层来源时，resolver 必须 fail closed。

## 7. 共享记忆与局部记忆

Multi-Agent 共享事实，不共享可变推理状态。

### 7.1 不可变 SharedRunContext

```python
SharedRunContext:
    run_id
    graph_version
    public_incident_input
    public_hypotheses
    decision_vocabulary
    owner_scope
    knowledge_base_scope
    global_deadline
    global_model_budget
    allowed_tools_by_specialist
```

Dispatch 后只读。

### 7.2 SpecialistState

```python
SpecialistState:
    assignment
    local_plan
    current_step
    local_observations
    local_hypothesis_signals
    unresolved_questions
    model_call_count
    deadline_state
    terminal_status
```

Runtime 与 Log Specialist 不能读取或修改对方的 local state。不得保存私有模型推理和原始响应。

### 7.3 Shared Evidence Store

每个已完成工具调用向 PostgreSQL 追加一条类型化 Evidence：

```python
SharedEvidenceRecord:
    evidence_id
    run_id
    specialist_role
    source_domain
    tool_name
    normalized_facts
    tested_hypotheses
    causal_intent
    source_fingerprint
    status
    created_at
```

稳定逻辑调用键为：

```text
run_id
+ graph_version
+ specialist_role
+ local_step_id
+ tool_name
+ canonical_arguments_hash
```

PostgreSQL 唯一约束和冲突安全读取是最终幂等保证。Redis 只能缓存已完成读取并发布进度事件，
不能成为唯一记忆、锁或正确性边界。

## 8. Specialist 合同

### 8.1 SpecialistAssignment

```python
SpecialistAssignment:
    role
    objective
    hypotheses_to_test
    required_causal_roles
    allowed_tools
    maximum_tool_steps
    model_call_budget
    deadline
```

Main Planner 分配调查目标，而不是为所有数据域提前写完整工具步骤。

### 8.2 SpecialistResult

```python
SpecialistResult:
    role
    terminal_status
    tested_hypotheses
    evidence_ids
    fact_candidates
    proposed_assessments
    unresolved_questions
    completed_steps
    model_call_count
    duration_ms
    result_checksum
```

`proposed_assessments` 只是 untrusted signal，不能直接改变中央 hypothesis 状态，也不能授权恢复。

### 8.3 真正的 Agent 行为

每个 Specialist 最多完成两次模型调用：

1. **Local Planning：** 在 allowlist 内选择最多 3 个工具步骤，确定顺序、tested hypotheses 和
   每一步 causal intent。
2. **Evidence Analysis：** 只解释本 Specialist 的公开工具结果，输出结构化 SpecialistResult。

每个结构化调用允许一次格式纠正重试。重试属于同一逻辑 role call，只新增 attempt，不能增加工具
或模型预算。

### 8.4 Runtime Specialist

允许：

- 检查 order-api pool state；
- 检查当前 Run 的 PostgreSQL session 与 lock wait；
- 验证 PostgreSQL reachability 与业务连接获取结果；
- 根据已有 Runtime Observation 自适应选择下一步。

禁止 CLS 和恢复工具。

### 8.5 Log Specialist

允许：

- 只查询 incident-scoped CLS window；
- 验证生命周期顺序和缺失 transition；
- 区分连接未释放与 traffic-only pressure；
- 标记日志证据缺失或冲突。

禁止 Runtime 和恢复工具；不得扩展可信 evidence preparer 给出的 Run、scenario、incident、owner、
topic 或时间范围。

## 9. Evidence Aggregator

Aggregator 完全确定性，不调用模型。

### 9.1 输出

```python
AggregatedInvestigation:
    specialist_statuses
    evidence
    normalized_facts
    hypothesis_signals
    conflicts
    source_groups
    missing_domains
    budget_usage
    aggregation_checksum
```

### 9.2 聚合规则

1. 相同 Evidence ID 只保留一次。
2. Evidence ID 不同但 source fingerprint 相同，只算一个独立来源。
3. 独立证据按 source group 统计，不按 Agent 或工具调用次数统计。
4. Specialist support/refute 仅作为 Fact Adapter 的 untrusted input。
5. 冲突必须记录，不允许多数投票。
6. 拒绝跨 Run、跨 owner、畸形、重复或不可信 Evidence。
7. 一个 Specialist 失败时保留另一个已完成的 Evidence。
8. Aggregator 不得生成 RootCauseDecision 或恢复提案。

稳定 aggregation identity：

```text
run_id
+ graph_version
+ sorted(SpecialistResult checksums)
```

## 10. Router 与发布模式

### 10.1 路由分数

```text
同时需要 Runtime 与 CLS                     +3
公开候选根因不少于 3 个                      +2
候选跨 component/mechanism domain            +2
必须建立跨数据源时间因果链                    +2
已有确定性证据足以直接决策                   -3
一个 evidence domain 已足够                  -3
剩余 deadline 不足                           -4
剩余模型预算不足                             -4
```

`score >= 5` 只表示 Multi candidate。第一版记录分数，但不在生产自动执行 Multi。

### 10.2 发布阶段

1. `strategy=single`：现有 Single。
2. `strategy=multi`：Benchmark forced Multi，失败仍保存为 Multi 失败。
3. `strategy=auto` shadow：保存建议路由，实际继续执行 Single。
4. 真正 `auto`：A/B 通过并单独批准后再设计发布。

持久化 requested/effective strategy、score、matched/rejected features、预算与 deadline 状态、
downgrade reason 和 release mode。

## 11. 模型与预算

第一轮 A/B 控制模型变量：

```text
Main Planner                 qwen3.7-plus
Runtime Local Planner       qwen3.7-plus
Runtime Evidence Analysis   qwen3.7-plus
Log Local Planner           qwen3.7-plus
Log Evidence Analysis       qwen3.7-plus
Shared Adjudicator          qwen3.7-plus
Decision                    qwen3.7-plus
Validator                   qwen3.8-max
Rerank                      qwen3-vl-rerank
Embedding                   qwen3.7-text-embedding
```

两个 Local Planner 并行，两个 Evidence Analysis 并行。与等价 Single 链路相比，Specialist 最多增加
4 次成功 role call。Shared Adjudicator 仅在确定性规则未收敛或 Specialist 冲突时调用。Validator
只验证中央最终 Decision，不分别验证 SpecialistResult。

所有调用共享一个 run budget。需要审计 role、model、duration、attempt、cache hit 与错误分类，
但不保存原始响应或私有推理。

## 12. Deadline 与并发

- 初始最多并行两个 Specialist。
- 每个 Specialist 最多 3 个工具步骤。
- Specialist 内有依赖的步骤保持串行。
- 相互独立的 Runtime 与 CLS 等待可以重叠。
- Specialist soft timeout：120 秒。
- Specialist hard timeout：180 秒。
- Global soft deadline：240 秒。
- Global hard deadline：360 秒。

Soft deadline 后禁止新 Local Plan 和 Replan，使用已有 Evidence 聚合；闭环不完整则
`manual_review`。Hard deadline 后取消未完成 Specialist，保留已完成 Evidence，并禁止自动恢复。

## 13. 失败语义

| 条件 | 分类 | 行为 |
| --- | --- | --- |
| Local Plan 纠正后仍无效 | `specialist_plan_failed` | 保存 role 失败和已完成 Evidence |
| Evidence Analysis 纠正后仍无效 | `specialist_analysis_failed` | 不伪造 assessment |
| 一个 Specialist hard timeout | `partial_specialist_timeout` | 聚合已完成 role，禁止不安全恢复 |
| 两个 Specialist 都失败 | `multi_investigation_failed` | 保存 Multi terminal failure |
| Specialist 证据冲突 | `specialist_evidence_conflict` | Shared Adjudicator 或人工复核 |
| 跨 Run/owner Evidence | security hard gate | 拒绝并 fail closed |
| Forced Multi 未完成 | Multi failure | 禁止用 Single 重跑覆盖 |
| Shadow/auto dispatch 前预算不足 | `budget_downgrade` | 执行 Single 并保存 downgrade |
| Router 异常 | `router_failed` | 生产 fail safe 到 Single，Benchmark 显式失败 |

Partial Multi 可以生成报告或提案，但自动恢复必须通过与 Single 相同的完整确定性和 Policy 门禁。

## 14. 审计链

必须持久化并展示：

- Routing Decision 与理由；
- Specialist role、终态、耗时、模型调用数和工具名；
- Local Plan step ID、tested hypotheses 与 causal intents；
- 每个 role 贡献的 Evidence ID 与 normalized public facts；
- 去重、source grouping、冲突和 missing domain；
- 中央 hypothesis transition、Decision、Validator origin 与 Recovery Policy；
- checkpoint replay、timeout、cancel、cleanup 和 idempotency event。

不得保存或展示隐藏 Chain-of-Thought、原始 Prompt、原始模型响应、原始 CLS 日志、凭据、Oracle
或 ground truth。

## 15. 验证策略

### 15.1 Slice 0 回归

从所有 Order Pool hypothesis 均 unresolved 开始，输入真实 canary 同形的公开 Fact，断言：

- exactly one supported lifecycle hypothesis；
- 必要 rule-out 成立；
- independent Evidence 完整；
- trigger/mechanism/impact 完整；
- 可以构造 grounded decision。

每个必要 Fact 都必须有缺失和冲突负例。

目标 unit、integration、Ruff、Pyright 和 Docker contract 通过后，只运行一次新的持久化 Single canary。
只有该 canary 形成 grounded decision 且 fixture clean，才允许开始 Multi-Agent 实现。

### 15.2 Specialist 与 Aggregator 测试

- Schema 与答案隔离；
- Specialist tool allowlist；
- model/tool budget；
- checkpoint replay 与稳定 checksum；
- 两个 Specialist 任意完成顺序；
- Evidence ID 与 source fingerprint 去重；
- 冲突不投票；
- partial timeout、cancel、worker restart；
- 跨 Run/owner 拒绝；
- Specialist 不暴露恢复工具；
- 不持久化原始推理和敏感内容。

### 15.3 真实 A/B

在相同 Order Pool 故障条件下，至少持久化：

```text
Single x 3
Multi  x 3
```

比较 Root Cause Top-1、Evidence Recall、因果链完整性、独立来源数、模型调用数、平均/P95 时延、
Specialist 失败率、安全门禁、恢复结果和 cleanup 状态。所有失败 Run 必须保留。

只有同时满足以下条件，Multi 才有资格进入 auto routing 的后续设计：

- 不降低根因准确率；
- 至少一个能力指标稳定提升；
- 不增加错误或不安全恢复；
- 平均成功模型调用数不超过 Single + 4；
- P95 时延不超过约定 deadline；
- 重复 Evidence 计分为 0；
- 安全和 cleanup 通过率不低于 Single。

## 16. 兼容与安全

- 除 Slice 0 正确性修复外，Single 行为保持不变。
- 第一版 Multi 只注册 Order Pool Live 场景。
- 外部身份与审计继续使用完整 Run ID，bounded PostgreSQL session label 只用于内部观察。
- owner、tenant、knowledge-base、CLS、Recovery 与 Artifact 隔离规则保持强制。
- PostgreSQL 对 checkpoint、SpecialistResult、Evidence、Aggregation、Evaluation terminal state
  和幂等冲突保持权威。
- Redis 只用于可选加速与进度事件。

## 17. 验收摘要

本设计是分阶段深化，不是一次性 Multi-Agent 重写：

1. 修复并用一次真实 canary 证明 Single 确定性闭环；
2. 增加可复用、隔离的 Specialist 合同；
3. Runtime/Log Specialist 分别执行有界 Local Planning 与 Evidence Analysis；
4. 确定性 Aggregator 接回不变的中央决策与安全链；
5. 使用相同模型和证据条件完成 forced Multi 与 Single A/B；
6. Shadow 与真实 auto routing 继续受能力收益和单独批准约束。
