## MODIFIED Requirements

### Requirement: Durable SSE sequence contract

AIOps SSE SHALL 使用PostgreSQL持久事件sequence作为断点位置，并 MAY 使用Redis Streams降低实时延迟。

#### Scenario: Client reconnects after interruption

- **WHEN** 客户端使用最后已确认sequence重新订阅
- **THEN** 服务 MUST 只返回更大sequence的事件，并 MUST 避免重复业务事件。

#### Scenario: Redis fails during an active stream

- **WHEN** Redis连接在诊断期间失败
- **THEN** SSE MUST 降级读取PostgreSQL持久事件，且诊断 MUST NOT 被取消。

