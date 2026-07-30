## ADDED Requirements

### Requirement: Transactional event outbox

系统 SHALL 在与业务状态相同的PostgreSQL事务中写入Outbox，并由独立Dispatcher发布到Redis Streams。

#### Scenario: Redis publish fails

- **WHEN** Dispatcher无法发布事件
- **THEN** Outbox MUST 保留事件、记录安全错误并使用退避重试，业务事务 MUST 保持已提交。

#### Scenario: Event is published more than once

- **WHEN** Dispatcher在确认前重试同一事件
- **THEN** 消费者 MUST 通过稳定event id和业务sequence去重。

### Requirement: Redis real-time event distribution

系统 SHALL 使用统一Redis Stream和Consumer Group分发AIOps实时事件，但 SHALL NOT 将Redis Stream ID作为业务顺序。

#### Scenario: Multiple application instances consume events

- **WHEN** 多个SSE实例运行
- **THEN** 它们 MUST 能消费实时事件并按owner、job和sequence隔离用户订阅。

### Requirement: Versioned Redis caches

系统 SHALL 仅缓存可安全回源的MCP工具定义和知识检索结果，并 SHALL 使用owner与资源版本隔离key。

#### Scenario: Cache misses or Redis fails

- **WHEN** 缓存未命中、过期或Redis不可用
- **THEN** 服务 MUST 回源MCP或Milvus且 MUST NOT 改变授权边界。

### Requirement: Distributed rate limits

系统 SHALL 使用Redis原子操作限制用户诊断、模型和MCP调用，并定义Redis不可用时的保守降级。

#### Scenario: Redis is unavailable for a read-only request

- **WHEN** 只读请求无法访问分布式限流
- **THEN** 服务 MUST 使用有界的进程内限制或拒绝高成本请求。

#### Scenario: Redis is unavailable for a recovery write

- **WHEN** 恢复类写操作无法完成分布式限流
- **THEN** 服务 MUST 默认拒绝执行。

