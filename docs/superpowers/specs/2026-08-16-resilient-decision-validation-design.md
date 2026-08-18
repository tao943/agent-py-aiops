# Resilient Decision Validation Design

## 目标

让 AIOps Workflow 在 LLM Decision Validator 暂时不可用或返回格式错误时，能够区分
“验证基础设施故障”和“业务证据不足”。对于已经通过严格公开证据检查的根因候选，系统可以
保留一份明确标记的确定性降级结论；对于任何证据不完整或存在竞争根因的候选，系统继续
fail-closed。

## 已确认故障

真实 `APY-013` Run `eval-b6ddb7d6083342cd975ff5a8412fca5d` 已正确收集以下证据：

- PostgreSQL `SQLSTATE 40P01` 与 `deadlock detected`；
- 两个事务以相反顺序获取 `order-row` 和 `inventory-row`；
- Wait Graph 存在长度为 2 的等待环，且不存在普通长阻塞事务。

Decision 节点三次生成正确的 canonical component、mechanism、trigger、Evidence IDs 和由
Observation summaries 修复的三段 causal chain，且
`decisionOrigin=llm_grounded_causal_chain_repair`。但是 Decision Validator 三次都落入统一的
异常分支，数据库只记录 `Structured root-cause validation was unavailable.`。Workflow 将验证器
不可用误判为证据缺口，额外调用 `GetDatabaseMetrics` 并重复生成相同决策，最终清空
`rootCauseDecision`，Benchmark 得到 45 分并失败于 `missing_root_cause_decision`。

当前异常处理无法区分模型调用失败和模型输出解析失败，也没有保存安全、结构化的诊断信息。

## 约束

- 不读取或间接使用 `ground_truth.yaml`、评分答案或隐藏 evaluator milestones。
- RAG 知识引用只能指导排查，不能作为故障成立的正证据。
- 只使用公开 hypotheses、公开 decision vocabulary、持久化 Evidence 和与 Evidence 绑定的
  Observation decisions。
- 验证器不可用不能伪装成 `missingEvidence`，也不能触发无关工具或重复证据采集。
- 降级结论不能授权自动恢复，只能生成建议或进入人工审批。
- 不改变 Ground Truth、Benchmark 权重、通过阈值或 canonical answers。
- 不新增数据库表或列；审计信息写入现有 step/checkpoint JSONB payload。
- 不新增依赖、外部服务、前端字段或 HTTP/SSE 合同。
- 保持 Python 3.10、strict Pyright、Ruff 和现有 LangGraph 编排。
- 新合同写入 `workflowVersion=evidence-driven-v3`；历史 v2 Artifact 继续按原合同读取，
  避免旧 Run 因缺少新字段而失去可评分性。

## 复用决策

项目已经依赖 LangChain、LangGraph、Pydantic 和 `langchain-openai`。GitHub 调研比较了：

- `langchain-ai/langchain`：MIT；现有 `ToolStrategy`、Pydantic schema、结构化输出错误和纠错重试
  能力与当前栈直接兼容；
- `567-labs/instructor`：MIT；结构化重试成熟，但会增加一套与 LangChain 重叠的调用抽象；
- `guardrails-ai/guardrails`：Apache-2.0；审计能力完整，但依赖与运行复杂度超出单一验证节点需求。

采用 wrapped adoption：复用现有 LangChain 的结构化输出能力，不增加依赖；证据一致性、失败路由、
降级条件和审计语义继续由项目领域代码实现。

## 选定架构

```text
LLM RootCauseDecision
  -> deterministic grounded validation
       invalid -> evidence gap or fail-closed
       valid   -> LLM semantic validator
                    valid                  -> llm_confirmed
                    explicit invalid       -> targeted evidence replan
                    call/parse unavailable -> deterministic_grounded_fallback
                                                  -> proposal/manual recovery only
```

### 第一层：确定性证据验证

候选必须同时满足以下条件：

1. 恰好一个 hypothesis 的状态为 `supported`。
2. 不存在状态为 `open` 的竞争根因 hypothesis。
3. candidate 的 component 和 mechanism 与 supported hypothesis 在公开
   `decisionVocabulary.labelsByHypothesis` 中的标签完全匹配。
4. candidate 的全部 Evidence ID 均已持久化且属于当前诊断任务。
5. supported hypothesis 至少绑定两份不同的正证据，且 candidate 的全部 Evidence ID 必须
   属于支持该 hypothesis 的 Observation Evidence ID 集合；Alert、Knowledge reference 或
   未支持该 hypothesis 的 Evidence 不能混入 candidate。
6. 至少两条 Observation decision 明确支持该 hypothesis，并引用 candidate 的持久化证据。
7. causal chain 包含 2 至 6 条非空内容，且每一条都来自上述证据绑定 Observation summary；允许
   保持 Observation 顺序，但不允许拆分或发明模型叙述。
8. trigger 非空，confidence 位于 `[0, 1]`。
9. Knowledge reference、Alert 和 Report 不能计入独立正证据数量。

确定性检查返回结构化 gap codes，而不是自然语言猜测。只有可以通过具体证据工具弥补的 gap 才能
进入 Replanner。

### 第二层：LLM 语义验证

第一层通过后，使用现有 ChatOpenAI-compatible Qwen model 的 LangChain 结构化输出包装和
Pydantic schema 请求 `RootCauseValidationDecision`。

- 正常返回 `valid`：记录 `validationOrigin=llm_confirmed`。
- 正常返回 `invalid` 且给出明确 unsupported fields 或 missing evidence：记录
  `model_rejected`，仅对这些具体缺口进行一次有界 Replan。
- 返回格式不合法：记录 `invalid_model_output`，向同一 Validator 提供结构化字段错误并最多纠错
  重试一次。
- API、网络、超时或限流失败：记录 `model_call_failed`，不进行格式纠错重试，也不进入证据
  Replanner。
- 格式纠错仍失败：记录 `retry_exhausted`。

### 确定性降级

只有第一层已完整通过，且第二层失败原因属于 `model_call_failed`、`invalid_model_output` 或
`retry_exhausted` 时，才保留 candidate，并记录：

- `status=valid`；
- `validationOrigin=deterministic_grounded_fallback`；
- `validationWarning=llm_validator_unavailable`；
- `validationErrorCategory`；
- `validationAttempts`。

这里的 `valid` 表示“通过公开证据合同”，不伪装成 LLM 已确认。报告和 Benchmark 可以使用该根因，
但 Recovery Planner 必须把它限制为 `proposal_only`、`manual_review` 或
`external_policy_required`；Policy Gate 不得授权自动执行。

第一层任一条件不满足时，不得使用降级路径。

## 路由与状态语义

| 情况 | 状态 | 后续路由 |
|---|---|---|
| 没有 candidate | `candidate_missing` | fail-closed |
| 第一层发现可补证据 gap | `deterministic_gap` | targeted Replanner |
| 第一层发现不可补结构/归属 gap | `deterministic_gap` | fail-closed |
| LLM 明确判定 invalid | `model_rejected` | 仅按明确 gap Replanner |
| LLM 调用失败且第一层通过 | `model_call_failed` | 确定性降级 |
| LLM 格式重试耗尽且第一层通过 | `retry_exhausted` | 确定性降级 |
| LLM 正常确认 | 无错误 | Recovery Planner |

必须满足以下禁止性条件：

- Validator unavailable 时不得调用 `GetDatabaseMetrics` 或其他证据工具；
- `missingEvidence` 为空时不得进入 Replanner；
- 不得重复执行已完成的相同 tool + arguments；
- 不得为了验证器基础设施错误重复生成相同根因；
- Validator 错误不得覆盖 Evidence Sufficiency 已记录的业务结论。

## 审计与数据安全

`decision_validation` step 和 checkpoint payload 增加结构化审计字段，但不改变数据库 schema：

- `validationOrigin`；
- `validationErrorCategory`；
- `validationAttempts`；
- `validationWarning`；
- `deterministicChecks`，仅包含 check code 与 pass/fail；
- `nextRoute`。

不持久化完整模型响应、Prompt、异常堆栈、API key 或原始敏感日志。解析错误只保存允许列表中的字段
错误代码。现有 owner scope、Evidence ownership 和 Ground Truth 隔离保持不变。

## 测试设计

### 单元测试

- 精确区分 `model_call_failed`、`invalid_model_output`、`model_rejected` 和
  `retry_exhausted`。
- 格式错误最多纠错重试一次；调用错误不触发格式重试。
- 九项确定性条件逐项缺失时均 fail-closed。
- RAG knowledge reference、Alert 与 Report 不计入正证据。
- 未持久化、其他任务或伪造 Evidence ID 被拒绝。
- causal chain 包含非 Observation summary 时被拒绝。

### Workflow 集成测试

- 正常 LLM 验证记录 `llm_confirmed`。
- LLM 明确拒绝只触发真实证据 gap 的有界 Replan。
- Validator unavailable 且确定性证据充分时保留根因并标记降级。
- Validator unavailable 且证据不足时清除根因。
- 降级路径不调用无关 Metrics、不重复 Decision、不自动授权 Recovery。
- Task result、Report、Checkpoint 与 Benchmark Artifact 使用一致的最终验证状态。新 Run 写入
  `workflowVersion=evidence-driven-v3`；Artifact 只对 v3 强制 validation origin allowlist，
  历史 v2 继续要求原有的 `status=valid`。

### 回归与真实验收

离线回归必须证明 `APY-013` 只调用三个必要工具：

1. `InspectPostgresErrors`；
2. `InspectPostgresWaitGraph`；
3. `InspectTransactionResourceOrder`。

并且不调用 `GetDatabaseMetrics`，Snapshot 回归、Ruff、strict Pyright、普通离线全量测试和
OpenSpec 验证全部通过。

离线验证完成后只运行一次真实 `APY-013`。通过标准：

- 非空 `rootCauseDecision`；
- `validationOrigin` 为 `llm_confirmed` 或满足全部严格条件的
  `deterministic_grounded_fallback`；
- 没有错误的 `decision_validation_gap` Replan；
- 没有 `GetDatabaseMetrics`；
- Benchmark 不再失败于 `missing_root_cause_decision`。

若真实验收仍失败，停止自动重试，并使用新增审计字段报告精确失败类别。

## 规格与实现范围

该行为会改变 AIOps Workflow 的可见验证状态和 Benchmark Artifact 资格，因此实现前创建一个聚焦
的 OpenSpec change，同步 AIOps evidence-driven workflow 与 DomainBench 文档。实现集中在现有
`super_ai.aiops.reasoning`、`super_ai.aiops.diagnostics`、evaluation artifact 和相邻测试，不建立平行
验证服务，不修改前端或公共 API。
