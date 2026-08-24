# background-job-runtime Specification

## Purpose
TBD - created by archiving change durable-background-job-runtime. Update Purpose after archive.
## Requirements
### Requirement: Durable owner-scoped background jobs

系统 SHALL 在 PostgreSQL 中持久化后台任务、执行尝试、租约、取消请求、失败原因、终态和事件，通过 Repository 边界访问，并 SHALL 支持多个 Worker 安全领取任务。

#### Scenario: 服务在任务执行期间重启

- **WHEN** running 任务的租约过期且服务重新启动
- **THEN** Runtime MUST 将任务重新置为可领取状态，并 MUST 保留原 attempt 信息。

#### Scenario: 其他用户查询任务

- **WHEN** 用户查询不属于自己的后台任务
- **THEN** API MUST 返回统一权限错误且不得泄露任务信息。

#### Scenario: Multiple workers claim queued jobs

- **WHEN** 多个Worker并发寻找可执行任务
- **THEN** Repository MUST 使用行锁和`SKIP LOCKED`保证同一任务不会被两个Worker同时领取。

#### Scenario: Worker lease expires

- **WHEN** running任务的租约过期
- **THEN** 其他Worker MUST 能重新领取，且原attempt和审计信息 MUST 保留。

### Requirement: Registered background job handlers

Runtime SHALL 通过 kind 到 handler 的注册表执行任务，并 SHALL 限制并发、超时和最大尝试次数。`production_recovery` handler SHALL 使用持久 execution claim 区分可安全恢复的调度、已完成副作用后的验证续跑和未知副作用结果。

#### Scenario: handler 执行成功

- **WHEN** 注册 handler 正常完成
- **THEN** job MUST 进入 succeeded 并记录开始和完成时间。

#### Scenario: handler 暂时失败

- **WHEN** attempt 小于最大次数
- **THEN** job MUST 使用退避时间重新排队；达到上限后 MUST 进入 failed。

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

### Requirement: Durable background job events

Runtime SHALL 在 PostgreSQL 中按 job 和单调递增 sequence 持久化后台任务事件，使订阅者可以断点读取；即使 Redis Streams 可用，PostgreSQL 仍 SHALL 保持规范事实源。

#### Scenario: SSE 客户端断开

- **WHEN** AIOps SSE 客户端断开但后台任务仍在运行
- **THEN** 任务 MUST 继续，重新订阅 MUST 能读取此前已保存和后续事件。

#### Scenario: Redis is unavailable

- **WHEN** Redis无法发布或消费实时事件
- **THEN** 任务 MUST 继续执行，SSE MUST 能按PostgreSQL sequence读取持久事件。

### Requirement: Background job cancellation and retry
用户 SHALL 能取消自己的 queued/running 任务，并为 failed/cancelled 任务创建重试。

#### Scenario: 取消 running 任务
- **WHEN** 用户请求取消 running 任务
- **THEN** Runtime MUST 记录取消请求并在安全事件边界停止，最终状态 MUST 为 cancelled。

### Requirement: AIOps jobs resume from durable execution state

Background job runtime SHALL resume an interrupted AIOps job only when task ID and graph version match. It SHALL
load the last durable LangGraph checkpoint and reuse completed execution records instead of restarting the graph.

#### Scenario: Worker lease expires after a checkpoint
- **WHEN** an AIOps Worker stops and another Worker claims the expired job lease
- **THEN** the new Worker MUST resume from the last completed checkpoint with the original budgets and deadlines

#### Scenario: SSE client disconnects
- **WHEN** the browser disconnects from an active AIOps event stream
- **THEN** the PostgreSQL-backed job MUST continue independently
- **AND** reconnect MUST replay durable events without creating a second diagnostic run

### Requirement: Unknown side effects are not blindly replayed

Background job runtime SHALL distinguish known failed work from side-effecting work with an unknown outcome.

#### Scenario: Recovery request loses its response
- **WHEN** a recovery tool request may have reached the target but its response is lost
- **THEN** execution MUST become `uncertain`
- **AND** retry MUST run an allowlisted state probe or require manual review instead of replaying the action

### Requirement: PostgreSQL remains the idempotency authority

Runtime SHALL use PostgreSQL unique constraints and conflict-safe reads as the final idempotency guarantee.

#### Scenario: Two Workers claim the same logical execution
- **WHEN** two Workers concurrently claim one stable execution key
- **THEN** at most one Worker MUST execute the operation
- **AND** the other Worker MUST wait for or reuse the durable result
