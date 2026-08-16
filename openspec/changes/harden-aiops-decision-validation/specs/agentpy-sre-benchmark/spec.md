## ADDED Requirements

### Requirement: Evidence-driven-v3 artifacts require audited validation origin

Evaluator SHALL 只从持久化 Workflow steps 构建 scoreable root-cause decision，并 SHALL NOT 使用报告
文本、Ground Truth 或 Validator 错误消息补全 Agent 结论。

#### Scenario: LLM confirms a v3 decision
- **WHEN** 最新 v3 Decision Validation 为 `status=valid` 且 `validationOrigin=llm_confirmed`
- **THEN** Artifact MUST 导出对应的持久化 root-cause decision

#### Scenario: Strict deterministic fallback validates a v3 decision
- **WHEN** 最新 v3 Decision Validation 为 `status=valid` 且
  `validationOrigin=deterministic_grounded_fallback`
- **THEN** Artifact MUST 导出对应的持久化 root-cause decision

#### Scenario: V3 validation origin is absent or unknown
- **WHEN** 最新 v3 Decision Validation 缺少 allowlisted validation origin
- **THEN** Artifact MUST NOT 导出 root-cause decision

### Requirement: Historical v2 artifacts remain compatible

Evaluator SHALL 继续按照历史 v2 合同读取已持久化 Run，避免新审计字段使旧结果失效。

#### Scenario: Historical v2 valid decision has no origin
- **WHEN** 历史 `evidence-driven-v2` Run 的最新 Decision Validation 为 `status=valid` 且没有
  `validationOrigin`
- **THEN** Artifact MUST 继续导出该历史决策

### Requirement: Validator inputs remain answer isolated

Benchmark SHALL 保持 Agent、Validator、Prompt、RAG、报告与 evaluator-only 标准答案的物理和运行时
隔离。

#### Scenario: Benchmark validation executes
- **WHEN** Snapshot 或 Live Benchmark 运行 Decision Validation
- **THEN** Agent、LLM Validator、Prompt、RAG 和报告生成器 MUST NOT 读取 Ground Truth 或 oracle 字段
