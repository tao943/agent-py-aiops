# PostgreSQL 与 Redis 运行时改造设计

日期：2026-07-30

## 1. 背景

Agent Py 当前使用 SQLite 保存用户、聊天、知识文档元数据、AIOps 诊断、证据、审计、LangGraph checkpoint 和后台任务。该实现适合本地单进程原型，但后台任务领取采用查询后修改的租约方式，实时事件依赖数据库轮询，不适合作为多 Worker Agent 平台的长期运行边界。

本次改造不迁移已有 SQLite 数据。当前源码包中不存在实际 SQLite 数据库文件，因此允许创建全新的 PostgreSQL 开发数据库并重新构建 Milvus 索引。

## 2. 已确认决策

- PostgreSQL 16 是唯一关系型数据库；不继续支持 SQLite 运行时或 SQLite 集成测试。
- 使用 SQLAlchemy 2.x Async、`asyncpg` 和 Alembic。
- Redis 7 承担 Streams、TTL 缓存和分布式限流。
- PostgreSQL 是业务事实源；Redis 不保存不可恢复的唯一数据。
- 现有 PostgreSQL 持久任务模型继续保留，不引入 Celery、RQ 或第二套 Redis 任务队列。
- 通过 Transactional Outbox 将 PostgreSQL 事件可靠发布到 Redis Streams。
- Milvus 继续负责知识 chunk 和向量，不引入 `pgvector`。

## 3. 目标与非目标

### 目标

- 将全部关系数据和后台任务迁移到 PostgreSQL。
- 使用 `FOR UPDATE SKIP LOCKED`支持多个 Worker 安全领取任务和 Outbox 事件。
- 使用 Redis Streams 为诊断事件提供低延迟、多实例实时分发。
- 使用 Redis 缓存 MCP 工具发现结果和版本化知识检索结果。
- 使用 Redis 原子 Token Bucket 限制诊断、LLM 和 MCP 调用。
- Redis 故障时保持诊断、证据和任务正确性，并允许 SSE 回退到 PostgreSQL。
- 使用真实 PostgreSQL 和 Redis 完成集成测试。

### 非目标

- 不迁移或保留 SQLite 开发数据。
- 不使用 Redis 替代 PostgreSQL 后台任务表。
- 不缓存最终诊断结论、恢复动作、权限判断或凭证。
- 不在本次改造中实现自动恢复动作或 OpenSRE 评测运行时。
- 不将后端、前端或 CLS MCP Server 移入 Docker Compose。

## 4. 总体架构

```text
FastAPI
├── PostgreSQL 16
│   ├── 用户、会话、知识元数据
│   ├── 诊断任务、步骤、证据、报告、案例
│   ├── LangGraph checkpoints
│   ├── 后台任务、租约、重试、持久事件
│   ├── 工具审计
│   └── outbox_events
├── Redis 7
│   ├── stream:aiops:events
│   ├── MCP / retrieval TTL cache
│   └── Token Bucket rate limits
└── Milvus
    └── 文档 chunk 和向量
```

Compose 继续只管理基础设施，新增 PostgreSQL 与 Redis，保留 etcd、MinIO、Milvus、Attu 和 Alertmanager。

## 5. PostgreSQL 设计

### 5.1 配置与依赖

- 将 `backend.memoryDatabaseUrl` 重命名为 `backend.databaseUrl`。
- 默认使用 `postgresql+asyncpg://agent_py:agent_py@127.0.0.1:5432/agent_py`。
- 删除 `aiosqlite`，增加 `asyncpg`。
- Alembic 和应用必须解析同一个项目配置。

### 5.2 Repository 边界

业务协议和 Record 类型保持稳定。实现从 `SQLite*Repository` 重命名为数据库无关的 `SqlAlchemy*Repository`，工厂从 `create_sqlite_memory_repositories` 改为 `create_sqlalchemy_repositories`。

实现模块不应以数据库产品命名；PostgreSQL 特有的并发语句封装在 Repository 内，业务服务不能直接引用 SQLAlchemy ORM 或 PostgreSQL SQL。

### 5.3 类型与迁移

- 灵活负载使用 PostgreSQL `JSONB`。
- 所有业务时间使用带时区的 `TIMESTAMPTZ`，应用写入 UTC。
- 对 owner、父资源、状态、创建时间的组合查询建立复合索引。
- 在空 PostgreSQL 上执行既有迁移链并修复方言不兼容项。
- 不提供 SQLite 到 PostgreSQL 的数据复制脚本。

### 5.4 后台任务并发

Worker 使用单事务领取：

```sql
SELECT ...
FROM background_jobs
WHERE ...
ORDER BY available_at, created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

领取事务更新状态、attempt、lease owner 和 lease expiration。租约续期、完成、失败和取消必须校验当前 lease owner。系统承诺至少一次执行，不声称 exactly-once；业务 handler 仍需保持幂等。

## 6. Redis 设计

### 6.1 Streams

统一 Stream 名称为 `stream:aiops:events`。事件包含：

- `event_id`
- `owner_user_id`
- `incident_id`
- `job_id`
- `sequence`
- `event_type`
- `payload`
- `created_at`

Redis Stream ID 只用于传输，PostgreSQL `sequence`才是断点续传和业务顺序依据。消费者通过`event_id`和`job_id + sequence`去重。

### 6.2 Transactional Outbox

业务状态、持久事件和`outbox_events`在同一PostgreSQL事务提交。Outbox Dispatcher批量使用`SKIP LOCKED`领取未发布事件，发布到Redis后记录`published_at`。

Outbox保存：

- event id、aggregate id/type
- owner、job和sequence
- event type与payload
- attempt、available at、last error
- created/published timestamps

Dispatcher支持重试、指数退避、幂等发布和已发布数据归档。Redis短暂故障不得回滚已提交的业务事务。

### 6.3 缓存

第一版只缓存：

```text
cache:mcp-tools:{owner}:{connection_version}
cache:retrieval:{owner}:{kb_version}:{query_hash}:{top_k}
```

- MCP工具清单TTL为60秒。
- 检索结果TTL为5分钟。
- 文档、连接或权限变化通过版本号切换命名空间。
- Cache miss和Redis异常直接回源。
- 不缓存最终答案、诊断、恢复方案、权限结果和凭证。

### 6.4 限流

通过Redis Lua脚本实现原子Token Bucket，维度包括用户、模型、MCP连接和工具。

- 只读请求在Redis异常时使用进程内保守限流。
- 新诊断和高成本模型请求可降级为更严格的本地限制。
- 恢复类写操作在无法完成分布式限流和策略判断时默认拒绝。

## 7. 实时事件与降级

正常链路：

```text
诊断节点
→ PostgreSQL业务表 + 持久事件 + Outbox
→ Outbox Dispatcher
→ Redis Stream
→ SSE Gateway
→ 浏览器
```

Redis不可用时：

```text
诊断继续写PostgreSQL
→ SSE Gateway按sequence轮询持久事件
→ Redis恢复后Dispatcher补发Outbox
```

SSE客户端提交最后收到的`sequence`，服务端只发送更大的业务序号。重复Redis消息不得产生重复UI事件。

## 8. 容错

- PostgreSQL不可用：readiness失败，不接受新任务。
- Redis不可用：缓存回源、事件回退PostgreSQL、诊断继续。
- Milvus不可用：明确返回检索降级，不构造虚假SOP证据。
- Dispatcher失败：保留Outbox并退避重试。
- Worker崩溃：租约过期后由其他Worker重新领取。
- Redis限流不可用：只读请求保守降级，写操作默认拒绝。
- 应用关闭：停止领取新任务，等待当前事务结束，安全停止Dispatcher和Redis客户端。

## 9. 测试

### 单元测试

- 领域服务、key构造、缓存版本、限流决策和事件去重使用纯单元测试。
- 单元测试不使用SQLite模拟PostgreSQL。

### 集成测试

- 使用真实PostgreSQL执行完整Alembic升级。
- Repository测试通过事务回滚或独立schema隔离。
- 两个Worker并发领取同一批任务，验证无重复领取。
- 使用真实Redis验证Streams consumer group、缓存TTL和Lua限流。
- 验证Redis中断期间任务完成、PostgreSQL事件可读，恢复后Outbox补发。
- 验证SSE按sequence断点续传且不会重复呈现。

### 验收条件

- 运行时代码和配置中不存在`aiosqlite`及`sqlite+aiosqlite`。
- 空PostgreSQL可从Alembic base升级至head。
- 多Worker不会同时领取同一任务。
- Redis停止时诊断仍能完成并持久化完整证据。
- Redis恢复后未发布事件可补发。
- SSE断线重连无丢失、无重复业务事件。
- 后端lint、type check和完整测试通过。

## 10. 实施顺序

1. 增加Compose PostgreSQL/Redis与健康检查。
2. 切换数据库配置、依赖和测试夹具。
3. 修复Alembic迁移和ORM PostgreSQL兼容性。
4. 重命名Repository并迁移全部集成测试。
5. 使用`SKIP LOCKED`重写任务领取并验证并发。
6. 添加Outbox模型、Repository和Dispatcher。
7. 添加Redis Streams发布、消费和SSE回退。
8. 添加缓存与限流。
9. 更新readiness、文档、架构图和运行手册。
10. 完成故障注入、回归和验收。

## 11. 风险

- 既有迁移可能包含SQLite假设：必须在真实PostgreSQL逐版本验证。
- Redis和PostgreSQL双通道可能产生重复事件：必须使用稳定event id和业务sequence。
- JSONB可能掩盖领域模型不清晰：只用于仍在演进的负载，稳定字段继续建列和索引。
- 新增基础设施提高本地启动成本：Compose提供健康检查、持久卷和明确的启动/清理命令。
- 源码包没有`.git`元数据：本设计可写入但无法在当前目录提交，需在恢复Git仓库后纳入版本控制。

