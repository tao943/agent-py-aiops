# Hypothesis-Coherent Causal Plan Repair Design

**日期：** 2026-08-20  
**范围：** AIOps `evidence-driven-v4` 诊断链路  
**触发 Run：** `order-pool-q3r-ab-single-01-20260820`

## 1. 问题

真实 Order Pool Live Run 成功执行四个只读诊断工具并收集 Runtime、PostgreSQL 与 CLS 证据，
但最终以 `recovery_denied/order_pool_decision_required` 结束。恢复门禁不是根因：系统没有产生
grounded root-cause decision，因此拒绝自动重启是正确行为。

Planner 生成的计划在全局上拥有 trigger、mechanism、impact，却把这些角色分配给不同候选
hypothesis。唯一收敛的 `order_connection_lifecycle_failure` 只得到 trigger 和一条独立正证据；
mechanism、impact 均属于其他候选。当前 `repair_plan_causal_coverage()` 只校验全局角色数量，
没有校验三个角色能否围绕同一个 hypothesis 闭环。

## 2. 目标

- 计划只有在至少一个公开 hypothesis 同时拥有 trigger、mechanism、impact 调查路径时才算完整。
- LLM 遗漏工具可验证的公开候选时，允许基于可信工具能力做有界补充。
- 工具执行完成后若仅剩元数据绑定缺口，复用持久化 Evidence，不重复调用工具。
- 保留 Deterministic Validator、LLM Validator Router、评分阈值、独立正证据门槛和恢复授权。
- 恢复 `qwen3.7-plus` 与 `qwen3-vl-rerank` 本地运行配置，控制后续 A/B 变量。

## 3. 非目标

- 不读取或复制 `ground_truth.yaml`、primary cause、required Evidence。
- 不把 `order_connection_lifecycle_failure` 或任何场景答案写入通用 causal-intent 模块。
- 不降低 `independent_positive_evidence >= 2`、causal-role coverage 或安全 hard gate。
- 不修改 `OrderPoolRecoveryService` 的授权条件。
- 不新增第三方依赖，不替换 LangGraph，不改聊天 Agent。
- 本变更不直接执行完整 3×3 A/B；先通过目标回归和一次真实 Single canary。

## 4. 方案比较

### 4.1 仅增强 Prompt

要求 Planner 将三个因果角色绑定到同一 hypothesis。实现简单，但模型仍可能遗漏绑定，不能作为
生产安全不变量。

### 4.2 Hypothesis-coherent 确定性门禁（采用）

复用现有公开工具能力、known hypotheses 和 causal-role registry。先规范 LLM 计划，再按单一
hypothesis 计算覆盖；只有可信公共合同允许时才补充 `testsHypotheses`。该方案不依赖模型稳定性，
且不读取 Oracle。

### 4.3 Live 场景完全确定性计划

能够稳定通过，但会绕过 Planner 能力并削弱 A/B 的真实性，容易形成“对着答案执行”的 Benchmark，
因此不采用。

## 5. 设计

### 5.1 可信工具调查能力

在 `super_ai.aiops.causal_intents` 增加公开的工具调查能力边界。能力只描述：

- 工具允许承担的 causal roles；
- 工具能够检验的公开 hypothesis IDs；
- 未登记工具只能保留模型已提供且通过 known-hypothesis 校验的绑定，不能自动扩展。

Order Pool 的能力来源于现有 `build_generic_live_plan()` 公共合同。例如数据库会话工具能区分连接
生命周期、数据库不可达、慢语句和锁等待；数据库可达性工具能区分连接生命周期与数据库不可达。
能力目录不得包含场景 ID、Oracle 字段、primary cause 或答案语义。

### 5.2 计划绑定规范化

Planner 输出完成既有 schema/tool/argument 校验后，对每个步骤执行：

1. 过滤未知 hypothesis；
2. 保留模型的合法绑定；
3. 仅对可信目录登记的工具，补充该工具可检验且存在于 `known_hypotheses` 的候选；
4. 保持稳定顺序并记录 `testsHypothesesOrigin=trusted_capability_repair`（仅发生补充时）。

该过程扩大的是候选检验范围，不指定哪个候选最终受支持。

### 5.3 按 hypothesis 的因果覆盖

`repair_plan_causal_coverage()` 不再只统计整份计划的角色。它必须寻找一个候选 hypothesis，使其
关联步骤可以在工具允许角色范围内覆盖：

```text
exactly one trigger + at least one mechanism + at least one impact
```

角色修复的优化顺序保持现有行为：最少修改模型角色，其次按既有 role priority 稳定选择。若没有任何
hypothesis 可闭环，返回 `complete=false` 和按候选计算的缺口，不能将不同候选的角色拼接为完整计划。

### 5.4 Evidence 复用与 Replanner

现有 Fact Adapter 的 `converged_causal_link` 只在当前步骤确实测试唯一受支持 hypothesis 时补充
mechanism/impact 链接；该安全条件保持不变。

若工具均已执行、唯一 hypothesis 已收敛、缺口仅来自旧计划绑定，Replanner 可以在可信能力允许的
范围内重新投影持久化 Evidence，并进行最多一次有界 Adjudication。不得重新调用工具、创建新 Evidence
ID 或执行恢复动作。若可信能力仍不能闭环，继续 `no_useful_step -> manual_review`。

### 5.5 恢复与失败语义

恢复层仍要求：

- root-cause decision 存在；
- component/mechanism 命中公开恢复合同；
- Live observation 已确认；
- recovery intent 幂等且仅作用于当前隔离服务。

任何计划、证据、Validator 或授权缺口继续 fail closed。之前的失败 Run 永久保留，不覆盖、不删除。

## 6. 测试

### 6.1 单元回归

- 角色齐全但分属不同 hypothesis 的计划必须 `complete=false`。
- 可信能力补充后，同一 hypothesis 可以覆盖 trigger/mechanism/impact。
- 未知工具、未知 hypothesis、非公开字段不得被补充。
- 通用 causal-intent 源码继续通过 Oracle/场景答案隔离扫描。

### 6.2 Workflow 回归

使用脱敏后的失败计划结构与公开工具 Evidence：

- 正确候选获得至少两条独立 Evidence；
- trigger/mechanism/impact 均来自持久化 Evidence；
- 竞争候选仍能被 refute；
- `decisionReady=true`，但不绕过 Validator 和 Policy Gate；
- Evidence 重投影不产生第二次工具调用或重复 Evidence。

### 6.3 真实验证

1. 运行 causal-intent、AIOps v4、Live adapter 与 Order Pool 目标 pytest。
2. 对修改文件运行 Ruff 与 Pyright；不运行全量 pytest。
3. 审计 30-card/180-chunk RAG、CLS、PostgreSQL、order-api。
4. 使用恢复后的 `qwen3.7-plus`、`qwen3-vl-rerank` 运行一个新 Run ID 的 Single canary。
5. 只有 canary 达到 `VALID_PASS`、verification/cleanup 均成功，才另行继续新 campaign 的 3×3 A/B。

## 7. Reuse Assessment

项目已有 LangGraph、causal-intent registry、generic Live plan、Fact Adapter、Hypothesis Adjudicator 与
PostgreSQL checkpoint，能够承载修复。GitHub 调研了 Tracer-Cloud/OpenSRE（Apache-2.0）、AWS
`sample-rca-deep-investigations`（MIT-0）和 Kubernetes AIOps Evidence Graph（MIT）：它们支持假设与
Evidence 绑定，但没有可直接复用的 hypothesis-role-tool 一致性算法。采用 reference-only，不复制代码、
不新增依赖。

## 8. 验收标准

- 本次真实失败计划被确定性回归测试捕获。
- 不同 hypothesis 的角色不能组成虚假的完整覆盖。
- 可信修复不包含 Oracle 或答案数据，未知工具不能扩大权限。
- 不降低任何 Validator、评分、Evidence 或恢复门禁。
- 本地有效模型配置为 `qwen3.7-plus` 与 `qwen3-vl-rerank`，配置文件继续被 Git 忽略。
- 目标测试、Ruff、Pyright 和一次真实 Single canary 提供新鲜证据。
