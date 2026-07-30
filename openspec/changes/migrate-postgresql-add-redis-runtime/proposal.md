## Why

SQLite 和数据库轮询满足本地单进程原型，但无法充分支撑多 Worker 的可靠任务领取、低延迟实时事件分发和分布式限流。项目已经通过 Repository 与 Alembic 保留数据库替换边界，现在需要将 PostgreSQL 设为唯一事实源，并用 Redis 提供不影响正确性的实时与缓存能力。

## What Changes

- 将唯一关系数据库从 SQLite 切换为 PostgreSQL 16，使用 SQLAlchemy Async、`asyncpg`和Alembic。
- 删除SQLite运行时和SQLite集成测试支持，不迁移现有SQLite数据。
- 使用PostgreSQL `FOR UPDATE SKIP LOCKED`实现多Worker后台任务领取。
- 增加Transactional Outbox，将持久诊断事件可靠发布到Redis Streams。
- 使用Redis缓存版本化MCP工具清单与知识检索结果。
- 使用Redis原子Token Bucket限制诊断、模型和MCP调用。
- Redis故障时缓存回源，SSE从PostgreSQL持久事件降级读取。
- 在Compose基础设施中增加PostgreSQL与Redis。

## Capabilities

### New Capabilities

- `redis-runtime-services`: 定义Redis Streams、缓存、限流、Outbox发布和降级行为。

### Modified Capabilities

- `memory-repositories`: PostgreSQL替代SQLite成为唯一关系存储。
- `background-job-runtime`: 使用PostgreSQL租约和`SKIP LOCKED`支持多Worker。
- `docker-compose-startup`: 增加PostgreSQL和Redis基础设施。
- `api-and-sse-contracts`: SSE使用持久sequence断点续传并支持Redis降级。
- `runtime-readiness-checks`: 区分PostgreSQL强依赖和Redis可降级依赖。

## Impact

- 后端依赖、项目配置、Alembic迁移和全部Repository实现。
- 后台任务并发控制、事件持久化和SSE订阅。
- Compose拓扑、健康检查、本地运行文档和CI服务。
- MCP工具发现、知识检索和API限流路径。
- PostgreSQL、Redis故障与恢复集成测试。

