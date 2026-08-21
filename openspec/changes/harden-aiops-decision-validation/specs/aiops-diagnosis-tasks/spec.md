## ADDED Requirements

### Requirement: Decision validation failures remain distinct from evidence gaps

后端 SHALL 先根据当前任务的公开 Hypothesis、Decision Vocabulary、持久化 Evidence 和
Evidence-linked Observation 验证根因候选，再调用 LLM Validator，并 SHALL 分开保存业务拒绝、
模型调用失败和模型格式失败。

#### Scenario: Validator provider failure after grounded checks pass
- **WHEN** candidate 通过全部公开确定性证据检查且 LLM Validator 调用失败
- **THEN** Workflow MUST 以 `validationOrigin=deterministic_grounded_fallback` 保留 candidate
- **AND** Workflow MUST NOT 进入证据 Replanner
- **AND** Recovery Policy MUST 要求人工审核或外部策略且 MUST NOT 允许执行

#### Scenario: Validator explicitly rejects supported fields
- **WHEN** LLM Validator 返回合法结构化 `invalid` 结果和具体 unsupported fields 或 missing evidence
- **THEN** Workflow MUST 将其分类为 `model_rejected`
- **AND** Workflow MAY 只执行一次有界、去重的 gap-targeted Replan

#### Scenario: Deterministic evidence contract fails
- **WHEN** candidate 缺少唯一支持、公开标签、当前任务正证据、独立 Observation 或 grounded causal chain
- **THEN** Workflow MUST fail closed
- **AND** Workflow MUST NOT 使用 deterministic fallback

#### Scenario: Candidate is absent
- **WHEN** Decision 节点没有生成可解析 candidate
- **THEN** Workflow MUST 保存 `candidate_missing`
- **AND** Workflow MUST NOT Replan 或授权恢复

### Requirement: Deterministic fallback uses only positive public evidence

确定性 fallback SHALL 只接受属于唯一 supported hypothesis 的 Observation Evidence IDs，且 SHALL
拒绝 Alert、Knowledge reference、Report、其他任务 Evidence 和未支持该 hypothesis 的 Evidence。

#### Scenario: Available but non-supporting evidence is cited
- **WHEN** candidate 引用了当前任务中存在、但没有被 supporting Observation 绑定的 Evidence ID
- **THEN** 确定性验证 MUST 拒绝 candidate

#### Scenario: Ground Truth is isolated
- **WHEN** Decision Validator 或确定性验证运行
- **THEN** 其输入和可调用工具 MUST NOT 包含 `ground_truth.yaml`、oracle 字段或隐藏评分答案

### Requirement: Resilient validation is auditable and recovery restricted

`evidence-driven-v3` Workflow SHALL 在现有 step/checkpoint JSONB payload 中保存 validation origin、
allowlisted error category、尝试次数、warning 和 deterministic check results，且 SHALL NOT 保存完整
模型输出、Prompt、异常堆栈或凭据。

#### Scenario: Deterministic fallback reaches recovery planning
- **WHEN** v3 Workflow 保留 deterministic fallback candidate
- **THEN** Recovery Plan MUST 使用 `manual_review`
- **AND** Policy Gate MUST 返回 `executionPermitted=false`

### Requirement: Structured-output steering follows model capability

后端 SHALL 从当前 Chat Model 的 capability profile 选择 LangChain structured-output 方法，且 SHALL
以不含凭据、真实日志或隐藏答案的最小 Live readiness 验证该请求形态。

#### Scenario: Model profile selects JSON mode
- **WHEN** 当前模型 capability 声明 `structuredOutputMethod=json_mode`
- **THEN** Decision 与 Validator MUST 使用 `json_mode` 构造 structured invoker
- **AND** Workflow MUST 保留既有 schema 校验、一次格式纠正和安全错误分类

### Requirement: Semantic validation uses an independently configured model

后端 SHALL 允许 Decision Validator 使用与主 Agent 不同的 Chat Model，并 SHALL 复用同一
Provider 的凭据、Base URL、timeout 与 retry 设置。未配置独立 Validator 的历史配置和测试
Provider SHALL 回退主 Chat Model；只有 Decision Validator 节点可使用该独立模型。

#### Scenario: Dedicated Validator is configured
- **WHEN** `validatorModel=qwen3.8-max` 且其 capability 声明 `structuredOutputMethod=json_mode`
- **THEN** Decision Validator MUST 使用 `qwen3.8-max` 和 `json_mode`
- **AND** Planner、Executor、Evidence Evaluation、Sufficiency、Decision、Recovery 与 Report MUST 继续使用主 Agent 模型

#### Scenario: Validator model profile is missing
- **WHEN** `validatorModel` 指向没有 capability profile 的模型
- **THEN** 配置加载 MUST fail closed
- **AND** 系统 MUST NOT 静默切换到未知模型

### Requirement: Validator parse diagnostics are secret-safe and auditable

Workflow SHALL 将 structured parse 失败分类为允许列表中的 `invalid_json`、
`structured_envelope_mismatch`、`missing_required_field`、`invalid_enum`、
`wrong_container_type`、`extra_field`、`unknown_evidence_id` 或
`invalid_json_or_schema`，并 SHALL 在 Step、Checkpoint 与 Run Artifact 中保存安全模型名、
错误码、category、phase 和尝试次数。

#### Scenario: First response needs a format correction
- **WHEN** Validator 第一次响应无法通过 JSON 或 Schema 校验且第二次响应合法
- **THEN** 最终 validation error category MUST 为空
- **AND** 审计 MUST 保留第一次的安全 parse code

#### Scenario: Format correction has insufficient deadline
- **WHEN** Validator 第一次响应无法通过 JSON 或 Schema 校验且剩余 hard deadline 少于一个完整 Validator role timeout 加 5 秒调度余量
- **THEN** Workflow MUST NOT 发起第二次模型调用或消耗第二次模型预算
- **AND** Workflow MUST 保存首次安全 parse code 和 `retry_skipped_insufficient_deadline`
- **AND** Recovery Policy MUST 保持 `executionPermitted=false` 并进入人工审核

#### Scenario: Validator receives an exact public output contract
- **WHEN** Workflow 构造 LLM Validator 请求
- **THEN** Prompt MUST 明确五个字段、字段类型、`valid/invalid` 枚举、空数组语义和禁止额外字段
- **AND** Pydantic Schema 与 Evidence ID allowlist MUST 继续作为最终接受条件
- **AND** Prompt MUST NOT 包含 Ground Truth、Oracle、凭据或原始 CLS 日志

#### Scenario: Parse failure is exhausted
- **WHEN** 两次 Validator 响应均无法通过 JSON 或 Schema 校验
- **THEN** Workflow MUST 保存 `retry_exhausted` 和 `structured_parse`
- **AND** Workflow MUST NOT 保存 Prompt、原始响应、异常正文、字段值、凭据、Ground Truth、Oracle 或原始 CLS 日志

#### Scenario: Validation audit contains malformed metadata
- **WHEN** 持久化 validation payload 包含未知枚举值或不符合 `^[A-Za-z0-9._-]{1,120}$` 的模型名
- **THEN** Run Artifact MUST 丢弃该值
- **AND** Benchmark scoring MUST 保持不变

### Requirement: Persisted hypothesis state is authoritative for sufficiency

Sufficiency Gate SHALL 从公开 Hypothesis 全集和持久化 Hypothesis State 确定
supported、refuted 与 unresolved 分类，且 SHALL NOT 允许模型输出覆盖该分类。

#### Scenario: Model prematurely closes an open competitor
- **WHEN** 模型报告 `sufficient`，但一个公开竞争假设在持久化状态中仍为 `open`
- **THEN** Workflow MUST 保存 `status=insufficient` 和该 unresolved hypothesis
- **AND** Workflow MUST 优先执行尚未运行且 `testsHypotheses` 覆盖该 competitor 的 Plan Step

#### Scenario: Persisted state is incomplete or malformed
- **WHEN** 公开 hypothesis 缺少状态，或状态包含重复/非公开 ID
- **THEN** Workflow MUST fail closed 为 insufficient
- **AND** Workflow MUST NOT 把非公开 ID 写入 Sufficiency 审计

### Requirement: Grounded expression normalization preserves strict validation

Workflow MAY 在 Validator 前把 LLM Candidate 的 trigger 和 causal chain 规范化为当前任务
supporting Observation 的精确 summary，但 MUST 保留 component、mechanism、Evidence IDs 和
confidence，并 MUST 在规范化前后运行同一确定性 Validator。

#### Scenario: Only expression checks fail
- **WHEN** Candidate 仅未通过 `trigger_present` 或 `grounded_causal_chain`
- **AND** 唯一 supported hypothesis 没有 open competitor
- **AND** Candidate 引用了规范化所使用的全部 supporting Observation Evidence
- **THEN** Workflow MAY 生成 `llm_grounded_normalization`
- **AND** 规范化结果 MUST 通过全部确定性 checks 才能进入 LLM Validator

#### Scenario: Candidate does not cite a copied observation
- **WHEN** 规范化链需要使用一个 Candidate 未引用 Evidence ID 的 supporting Observation
- **THEN** Workflow MUST NOT 规范化 Candidate
- **AND** 后续验证与恢复 MUST 保持 fail closed
