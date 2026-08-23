## MODIFIED Requirements

### Requirement: Registered background job handlers

Runtime SHALL 通过 kind 到 handler 的注册表执行任务，并 SHALL 限制并发、超时和最大尝试次数。`production_recovery` handler SHALL 使用持久 execution claim 区分可安全恢复的调度、已完成副作用后的验证续跑和未知副作用结果。

#### Scenario: Recovery job is redelivered before side-effect claim
- **WHEN** queued/revalidating recovery job 的 Worker 租约过期且尚无 side-effect execution claim
- **THEN** 新 Worker MUST 重新授权和 preflight 后继续处理

#### Scenario: Recovery job is redelivered after unknown side effect
- **WHEN** side-effecting execution claim 过期且结果未知
- **THEN** Runtime MUST NOT 自动重试 handler 的副作用
- **AND** Intent MUST 转入 `manual_intervention`

#### Scenario: Recovery job resumes after completed side effect
- **WHEN** execution record 已 completed 但 Intent 尚未完成验证
- **THEN** Runtime MUST 复用 execution result 并仅恢复 verifier

