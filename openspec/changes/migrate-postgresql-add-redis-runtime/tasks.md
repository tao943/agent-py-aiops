## 1. PostgreSQL基础设施

- [x] 1.1 为Compose增加PostgreSQL 16、持久卷和健康检查
- [x] 1.2 将项目配置切换为`backend.databaseUrl`
- [x] 1.3 删除`aiosqlite`并增加`asyncpg`
- [x] 1.4 让Alembic在空PostgreSQL上从base升级到head

## 2. Repository迁移

- [x] 2.1 将SQLite命名实现重构为通用SQLAlchemy Repository
- [x] 2.2 修复JSONB、TIMESTAMPTZ、默认值、约束和索引
- [x] 2.3 将Repository集成测试迁移到真实PostgreSQL
- [x] 2.4 移除SQLite运行时与集成测试路径

## 3. 持久任务并发

- [x] 3.1 使用`FOR UPDATE SKIP LOCKED`原子领取后台任务
- [x] 3.2 验证租约续期、取消、重试和过期恢复
- [x] 3.3 增加多Worker无重复领取并发测试

## 4. Outbox与Redis Streams

- [ ] 4.1 增加Outbox ORM、Alembic迁移和Repository
- [ ] 4.2 实现批量领取、发布、退避和归档Dispatcher
- [ ] 4.3 为Compose增加Redis 7、持久卷和健康检查
- [ ] 4.4 实现统一AIOps Redis Stream和Consumer Group
- [ ] 4.5 将SSE接入Redis并保留PostgreSQL轮询降级
- [ ] 4.6 覆盖重复发布、Redis断连、恢复补发和SSE续传

## 5. 缓存与限流

- [ ] 5.1 缓存版本化MCP工具发现结果
- [ ] 5.2 缓存版本化知识检索结果并支持回源
- [ ] 5.3 使用Lua实现原子Token Bucket
- [ ] 5.4 实现Redis不可用时的只读降级与写操作拒绝策略

## 6. 运行与验证

- [x] 6.1 更新readiness、结构化日志和运行指标
- [ ] 6.2 更新README、基础设施指南、配置模板和架构图
- [ ] 6.3 运行后端lint、类型检查、PostgreSQL/Redis集成测试
- [ ] 6.4 运行Compose配置与OpenSpec验证

