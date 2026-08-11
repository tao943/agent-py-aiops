## Context

现有诊断链以 Planner、Executor、Replanner、Report 运行，并持久化任务、步骤、工具审计、证据、报告与 checkpoint。第一版 benchmark 应复用这些边界，建立一个足够小但完整的离线纵切，同时避免标准答案进入 Agent 上下文或 RAG。

完整设计和评分定义见 `docs/superpowers/specs/2026-08-10-agentpy-benchmark-evidence-evaluation-design.md`，执行步骤见 `docs/superpowers/plans/2026-08-10-agentpy-benchmark-foundation.md`。

## Goals / Non-Goals

**Goals:**

- 用 APY-003 与 APY-006 验证同一 502 现象的不同根因。
- 冻结并严格匹配类型化 Snapshot 工具调用。
- 保存可审计的“假设—观测—支持/反驳—决策”结构，不保存隐藏推理文本。
- 以 ground truth、证据里程碑、工具审计和安全事件计算确定性评分。
- 使运行结果能够按场景、suite、Agent 和模型配置重现。

**Non-Goals:**

- 不把 OpenSRE 或其他 benchmark 作为运行时依赖。
- 不让 RAG 读取场景标准答案。
- 不在 Snapshot 测试中启动故障容器或调用真实云 API。
- 不实现 L1/L2 恢复动作与 Live 恢复判定。

## Decisions

### 答案与 Agent 输入分离

`scenario.yaml` 和 `snapshot/tool_responses.yaml` 是 Agent 可见输入；`ground_truth.yaml` 只能由 evaluator 在 Agent 返回后加载。公共 loader 拒绝答案字段，Snapshot 客户端构造后不再读文件。

### 差异诊断使用成对场景

APY-003 与 APY-006 暴露相同告警族和候选假设。前者观测为上游进程不可用，后者为健康进程与 Nginx upstream 端口不一致。只有 component、mechanism、trigger 与必要证据匹配才算诊断正确。

### 可审计决策不是 Chain-of-Thought

工作流仅保存结构化假设状态、工具目的、观测摘要、支持/反驳关系、证据 ID、根因字段、置信度和因果链。模型不能引用不存在的证据 ID。

### 评分由确定性规则负责

评分维度为 outcome 20、diagnosis 25、evidence 20、process 15、safety 15、efficiency 5。伪造证据、读取答案或危险工具调用触发硬门槛；评分理由逐项保存，Markdown 关键词不参与评分。

### PostgreSQL 保存权威运行记录

运行、版本化配置和结果保存到 PostgreSQL。配置只保存模型名和非敏感参数，不保存密钥。Redis 不参与评分正确性。

## Risks / Trade-offs

- 两个案例不能代表完整生产分布，因此本变更只用于验证框架纵切，不宣称通用能力。
- 冻结观测高度可重复但缺少真实时序扰动，后续由六个 Live 案例补足。
- 结构化模型输出可能校验失败，此时必须记录证据不足或无有效决策，不能回退为无依据结论。
