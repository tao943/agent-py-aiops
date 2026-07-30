## MODIFIED Requirements

### Requirement: PostgreSQL and Redis readiness semantics

运行时 SHALL 将PostgreSQL视为强依赖，将Redis视为可降级依赖，并分别报告健康状态。

#### Scenario: PostgreSQL is unavailable

- **WHEN** PostgreSQL健康检查失败
- **THEN** readiness MUST 失败且API MUST NOT 接受新的持久任务。

#### Scenario: Redis is unavailable

- **WHEN** Redis健康检查失败但PostgreSQL正常
- **THEN** readiness MUST 报告降级状态，诊断和持久任务 MUST 继续，缓存和事件 MUST 使用规定的回源路径。

