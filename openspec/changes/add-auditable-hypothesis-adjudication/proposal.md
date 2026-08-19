## Why

当前 AIOps 工作流以 `open/supported/refuted + confidence` 表达假设，并在多个串行节点调用模型。
这既不能证明竞争根因被证据真正排除，也会在网络或 Worker 中断后重复调用模型、工具或恢复动作。
现有 checkpoint 是追加式审计快照，不足以作为安全续跑的执行真源。

## What Changes

- 将公开假设升级为 `supported/refuted/causally_inactive/unresolved` 四态裁决。
- 仅允许代码内受信 evidence-rule template 产生确定性裁决；真实歧义最多批量调用一次 Adjudicator。
- 始终执行 Deterministic Validator，仅在确定性风险条件成立时调用 LLM Validator。
- 将模型调用硬上限设为 8，并持久化调用计数、5 分钟软截止和 8 分钟硬截止。
- 以 PostgreSQL 唯一约束、稳定业务 ID 和 LangGraph checkpointer 支持中断后安全续跑。
- 新增 `aiops-diagnostic-v2` / `evidence-driven-v4`，保持 v2/v3 历史 Artifact 评分语义不变。

## Capabilities

### Modified Capabilities

- `aiops-diagnosis-tasks`
- `background-job-runtime`
- `agentpy-sre-benchmark`

## Impact

- 新增四态裁决、Fact Adapter、Validator Router、模型预算和执行协调模块。
- 新增 PostgreSQL 执行、LangGraph checkpoint/write 表及 Alembic migration。
- 稳定化诊断 Evidence、Step、工具审计、报告链接和审计 checkpoint 的业务 ID。
- 不增加第三方依赖、Redis 分布式锁、外部服务或 Ground Truth 访问能力。
- 不降低 Benchmark 阈值、required evidence、评分权重或恢复授权门禁。
