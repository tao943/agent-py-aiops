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
