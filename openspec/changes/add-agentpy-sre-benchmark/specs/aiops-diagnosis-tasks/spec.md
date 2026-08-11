## MODIFIED Requirements

### Requirement: Evidence-based execution and reporting

AIOps 诊断 SHALL 使用公开告警和可用 MCP 工具形成有界计划，保存每次工具调用的目的、观测以及对已知假设的支持或反驳关系，并 SHALL 只依据可解析的证据记录生成结构化根因决策和最终报告。

#### Scenario: Multiple hypotheses share one symptom

- **WHEN** 一个告警公开多个可能根因
- **THEN** 工作流 MUST 使用至少两个独立观测区分 primary cause，并 MUST 保存至少一个被证据排除的竞争假设。

#### Scenario: Evidence is insufficient

- **WHEN** 工具失败、观测冲突或没有足够证据支持唯一根因
- **THEN** 结构化根因决策 MUST 为空或明确表示证据不足，报告 MUST NOT 编造确定结论。

### Requirement: Diagnostic workflow writes evidence-chain records

LangGraph Planner、Executor、Evidence Evaluator、Replanner、Decision 和 Report 节点 SHALL 通过诊断证据链存储库持续保存计划、工具观测、结构化假设更新、根因决策和报告来源。

#### Scenario: Decision references evidence

- **WHEN** Decision 节点产生根因决策
- **THEN** 其中每个 evidence ID MUST 对应当前诊断任务已经持久化的证据记录。
