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
