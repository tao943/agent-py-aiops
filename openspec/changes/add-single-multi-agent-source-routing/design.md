## Context

当前 v4 图按 Planner、Executor、Fact Adapter、Sufficiency、Decision、Validator、Recovery、Policy、
Report 串行运行，已经具备 PostgreSQL checkpoint、ExecutionCoordinator、稳定业务 ID、四态假设、
工具审计和答案隔离。本变更只深化调查阶段，不替换既有安全闭环。

完整设计见
`docs/superpowers/specs/2026-08-20-single-multi-agent-source-routing-design.md`，实施计划见
`docs/superpowers/plans/2026-08-20-single-multi-agent-source-routing.md`。

## Goals / Non-Goals

**Goals:** 保持 Single 默认；按公开状态确定性路由；并行独立 runtime/log 取证；确定性汇合；
checkpoint 安全恢复；支持可复现 A/B。

**Non-goals:** 不让 Agent 自由对话或委派；不接 GitHub/Deployment 数据源；不开放恢复工具；
不并行主链 LLM；不承诺 Multi 必然提升。

## Decisions

### 复用现有 LangGraph，不引入第二套运行时

直接复用 `StateGraph`、动态 `Send`、fan-in、checkpointer、ExecutionCoordinator、Fact Adapter 和审计。
AutoGen 与 OpenAI Agents SDK 只作参考，不新增依赖。

### Planner 后进行确定性路由

Knowledge Investigator 先运行一次现有 RAG，Planner 生成带 `sourceDomain` 的计划，随后 Router 根据
版本化规则、能力、预算、deadline 和公开证据选择 fast path、single 或 multi。LLM 不参与路由。

Router 分数为 0..3 single、4..5 single + watch、>=6 multi。trusted pattern、decision ready、少于
两个可并行数据源、预算不足、deadline 不足、能力不可用和已完成相同 dispatch 都是优先硬门禁。
Change Investigator 固定不可用，原因码为 `deployment_change_source_not_configured`。

### 工具必须显式证明只读

MCP discovery、描述和 schema 不能证明安全。只有项目代码内 `read_only=True` 的 capability descriptor
才能把工具暴露给 Runtime 或 Log Investigator；未知、可写、恢复和 proposal-only 工具全部 fail closed。

### EvidencePacket 隔离并行写入

Investigator 只生成自己 Dispatch 的 Packet、工具审计和 Evidence，不修改共享 Fact、Hypothesis 或
Observation。Aggregator 校验 owner/task、audit、Evidence、质量、时空范围并稳定排序、去重、标记冲突，
然后成为 fan-in 后唯一共享状态写入者。失败或超时不能解释为反证。

### 预算与失败降级

确定性 collector 最大并发4，可选 Investigator LLM 最大并发2。Knowledge 已先完成，因此首版 Multi
通常只有 Runtime 与 Log 两个 Dispatch；每个最多一次可选模型调用，最多两轮。部分失败保留有效 Packet；
全部失败在预算允许时回退 Single，否则人工复核。迟到结果只审计，不修改既有 Decision 或评分 Artifact。

### 新图使用独立 checkpoint 版本

新拓扑固定使用 `aiops-diagnostic-v3` 和 `aiops:{task_id}:aiops-diagnostic-v3`。历史或未完成 v2
checkpoint 只由旧拓扑读取；缺失 graph version 的历史任务视为 v2，不自动迁移 channel state。

### 强制策略只属于 Benchmark

内部 Live CLI 可传 `--strategy auto|single|multi`；普通 API 固定 auto 且受服务端开关约束。A/B 固定
场景、模型、知识库、CLS 窗口、工具和评分器。terminal envelope 只保存 root-cause correctness、
Evidence Recall、耗时、模型调用、重复 Evidence、fallback 和安全 hard gate 等定长聚合值，不保存
Oracle 标签、required Evidence ID、Prompt 或原始日志。

## Risks / Trade-offs

- 新图版本会让未完成 v2 任务继续走旧拓扑，但避免不兼容状态被错误续跑。
- 显式工具白名单会降低未知 MCP 覆盖率，但比推断只读属性安全。
- Multi 可能增加延迟和费用；未达到 A/B 门槛时保持 Benchmark-only。
- PostgreSQL 是正确性真源；不引入 Redis 分布式锁以避免双重一致性语义。
