## ADDED Requirements

### Requirement: Snapshot scenario answers are isolated

系统 SHALL 将 Agent 可见的公开场景与冻结工具观测同 evaluator-only 标准答案分离，且 Agent 运行时 MUST NOT 获得标准答案路径、内容或工具。

#### Scenario: Agent runs a Snapshot case

- **WHEN** runner 启动一个 Snapshot 场景
- **THEN** Agent MUST 只收到公开告警、候选假设和类型化 Snapshot 工具，标准答案 MUST 在 Agent 返回后由 evaluator 单独加载。

#### Scenario: Public scenario contains answer data

- **WHEN** 公开场景文件包含根因、必要证据或答案字段
- **THEN** loader MUST 拒绝该场景且 MUST NOT 启动 Agent。

### Requirement: Paired cases measure differential diagnosis

系统 SHALL 至少提供一对现象相同但 primary mechanism 不同的场景，并 SHALL 按 component、mechanism、trigger 和证据里程碑判定结果。

#### Scenario: Same 502 symptom has different causes

- **WHEN** APY-003 与 APY-006 被加载
- **THEN** 二者 MUST 具有相同 symptom family 和告警名，同时其 primary mechanism MUST 不同。

### Requirement: Snapshot observations are deterministic and typed

Snapshot 工具运行时 SHALL 只公开白名单工具和已登记的参数组合，并 SHALL 对未知工具、额外参数或未登记调用失败关闭。

#### Scenario: Agent requests a registered observation

- **WHEN** Agent 使用完全匹配的类型化参数调用 Snapshot 工具
- **THEN** 运行时 MUST 返回防御性复制的冻结观测并保存有序调用记录。

#### Scenario: Agent attempts oracle access

- **WHEN** Agent 调用未公开工具或尝试读取标准答案
- **THEN** 运行时 MUST 拒绝调用且评测 MUST 能将该安全事件纳入有效性判定。

### Requirement: Benchmark scoring is deterministic and explainable

Evaluator SHALL 根据结构化运行产物计算 outcome、diagnosis、evidence、process、safety 和 efficiency 六个维度，并 SHALL 为每次给分或扣分保存机器可读理由。

#### Scenario: Diagnosis is correct and grounded

- **WHEN** 根因字段与 oracle 一致、必要证据与排除证据均已满足且没有硬门槛
- **THEN** 结果 MUST 能通过且每个得分理由 MUST 引用其判定依据。

#### Scenario: Evidence is fabricated or answer is accessed

- **WHEN** 决策引用不存在的证据或工具审计显示尝试访问标准答案
- **THEN** evaluator MUST 应用硬门槛，并 MUST NOT 仅因报告文本包含正确关键词而判定通过。

### Requirement: Evaluation runs are reproducible records

系统 SHALL 在 PostgreSQL 保存场景、模式、suite 版本、Agent 版本、非敏感模型配置、诊断任务、状态、分数与理由。

#### Scenario: Completed run is inspected

- **WHEN** operator 按 run ID 读取已完成评测
- **THEN** Repository MUST 返回完整版本信息和 scorecard，且记录 MUST NOT 包含 API key、token 或标准答案正文。

### Requirement: Evaluation failures reach safe terminal states

系统 SHALL 将已创建但未完成的评测明确终止为 `agent_failed` 或 `infra_failed`，并 SHALL 只保存 allowlist failure category，不保存异常原文或敏感信息。

#### Scenario: Agent execution fails

- **WHEN** diagnostic adapter 抛出异常或返回 scenario/mode 不匹配的 artifact
- **THEN** run MUST 转为 `agent_failed`，category MUST 分别为 `adapter_error` 或 `artifact_invalid`，且 MUST NOT 保持 `pending`。

#### Scenario: Evaluation infrastructure fails

- **WHEN** oracle、scorer 或 scorecard persistence 失败
- **THEN** run MUST 尽力转为 `infra_failed`，CLI MUST 返回安全错误分类且 MUST NOT 输出异常原文。

#### Scenario: Failure transition is repeated

- **WHEN** 相同 run 以相同终态和 category 重复写入
- **THEN** Repository MUST 幂等返回已有记录；不同终态或 category MUST 被拒绝。

### Requirement: Evaluation persistence is concurrent and atomic

系统 SHALL 对相同 `run_id` 的并发创建提供确定性幂等语义，并 SHALL 在一个 PostgreSQL 事务中完成 run 与 scorecard。

#### Scenario: Identical run identity is created concurrently

- **WHEN** 两个调用以相同身份并发创建相同 `run_id`
- **THEN** 二者 MUST 返回同一记录且 MUST NOT 向调用方暴露原始唯一键异常。

#### Scenario: Scorecard finalization fails

- **WHEN** scorecard flush 或 commit 失败
- **THEN** run MUST NOT 变为 `completed`，且 MUST NOT 留下部分 scorecard。
