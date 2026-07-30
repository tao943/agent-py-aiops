## MODIFIED Requirements

### Requirement: Durable PostgreSQL background jobs

系统 SHALL 在PostgreSQL中持久化后台任务、尝试、租约、取消、失败和事件，并 SHALL 支持多个Worker安全领取任务。

#### Scenario: Multiple workers claim queued jobs

- **WHEN** 多个Worker并发寻找可执行任务
- **THEN** Repository MUST 使用行锁和`SKIP LOCKED`保证同一任务不会被两个Worker同时领取。

#### Scenario: Worker lease expires

- **WHEN** running任务的租约过期
- **THEN** 其他Worker MUST 能重新领取，且原attempt和审计信息 MUST 保留。

### Requirement: Durable events remain canonical

PostgreSQL后台任务事件 SHALL 保持SSE断点续传的规范事实源，即使Redis Streams可用。

#### Scenario: Redis is unavailable

- **WHEN** Redis无法发布或消费实时事件
- **THEN** 任务 MUST 继续执行，SSE MUST 能按PostgreSQL sequence读取持久事件。

