## Context

当前后端已经依赖 LangGraph、SQLAlchemy、asyncpg 和 PostgreSQL，并持久化诊断 Step、Evidence、
工具审计、报告和追加式 graph checkpoint。新设计复用 LangGraph checkpoint 协议与现有仓储边界，
但把执行恢复真源与展示型审计事件分开。

完整设计见 `docs/superpowers/specs/2026-08-18-auditable-hypothesis-adjudication-design.md`，实施计划见
`docs/superpowers/plans/2026-08-19-auditable-hypothesis-adjudication.md`。

## Decisions

### 四态裁决是安全真源

每个公开假设必须处于 `supported`、`refuted`、`causally_inactive` 或 `unresolved`。关闭状态必须
引用当前任务的公开 Evidence；“另一个原因已经解释故障”本身不能关闭竞争假设。只有恰好一个
supported 且没有 unresolved 活跃竞争项时，系统才能形成单根因结论。

### 确定性规则必须来自受信模板

Planner 只能选择项目代码内的模板和有界参数，不能自行定义 fact 与 disposition 的因果映射。
未命中模板的观察进入一次批量 LLM Adjudicator；其输出仍须经相同的 Evidence 门禁。

代码拥有的复合受信模式可以组合多个只读工具的公开 Fact，但只能接受当前任务已经持久化的
Evidence ID。Nginx timeout 模式必须同时看到 HTTP 504、上游连接成功、读取 deadline 到期、独立
upstream/gateway 健康探针和 incident-scoped CLS timeout 事件；缺失、冲突或跨任务 Evidence 均保持
unresolved。该模式不读取 Scenario/Run ID、Oracle、Ground Truth、评分规则或 fixture 数值。

### Validator 由代码路由

Deterministic Validator 始终运行。仅当使用过 LLM Adjudicator、请求执行恢复、存在 L2/L3 动作、
跨组件因果链或高质量证据冲突等代码可计算条件时调用 LLM Validator。确定性校验失败后只允许
一次 targeted replan，否则生成确定性人工复核计划，不再调用 Recovery Planner 模型。

### PostgreSQL 保证幂等与恢复

LangGraph checkpoint 保存完整图状态；execution record 以稳定 key claim/reuse/wait。业务 Evidence、
Step、工具审计和报告链接使用稳定 ID 冲突安全写入。恢复动作结果未知时先探测系统状态，不能盲目
重放。Redis 不参与本轮正确性保证。

### 预算和 deadline 跨重启持续

模型调用总数、角色审计、Replanner 次数、首次启动时间和软硬 deadline 都属于 checkpoint 状态。
Worker 重启使用原值恢复，不能重新获得调用或时间预算。

### Replanner 仅在仍有可证明的搜索空间时调用模型

Replanner 先用现有工具 schema、参数合同、execution-owned 参数和 causal capability 计算可执行的
deterministic gap step。仅当代码不能证明所有可覆盖 gap 的有界调用都已执行时，才允许调用一次
Replanner 模型；空参数、const 或 execution-owned 固定参数工具均已耗尽时直接持久化
`no_useful_step`。RAG 的向量/BM25 并发和 owner/version 隔离 Redis cache 保持现状；Adjudicator 仍是
一次批量调用，主链与同步 Report 不并行。

## Risks / Trade-offs

- 受信模板覆盖率不足会增加一次 Adjudicator 调用，但比错误确定性关闭更安全。
- PostgreSQL checkpointer 增加存储与 migration 复杂度，但避免依赖新的外部协调服务。
- 供应商请求途中断网仍可能重复计费；系统只能限制次数并保证业务结果不重复生效。
- 当前 v4 仅支持单根因；多个 supported cause 必须 fail closed，不能借 compound 路由绕过门禁。
