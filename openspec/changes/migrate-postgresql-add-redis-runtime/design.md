## Context

项目当前使用SQLite保存全部关系数据，并使用持久任务事件表支持SSE轮询。源码包中不存在需要迁移的SQLite数据库。目标是形成更符合多Worker Agent平台的后端栈，同时避免为了引入Redis而创建第二套任务事实源。

## Goals / Non-Goals

**Goals:**

- PostgreSQL-only运行时和集成测试。
- 多Worker安全领取任务和Outbox事件。
- Redis Streams低延迟事件分发。
- 可版本化、可回源的缓存。
- 原子分布式限流。
- Redis故障不影响业务正确性。

**Non-Goals:**

- 不迁移SQLite数据。
- 不引入Celery/RQ或Redis任务队列。
- 不缓存最终诊断、权限或凭证。
- 不替换Milvus。

## Decisions

### PostgreSQL是唯一事实源

用户、知识元数据、诊断、证据、检查点、审计、任务、事件和Outbox全部保存到PostgreSQL。Redis数据可以从PostgreSQL或外部服务重建。

### Repository实现与数据库名称解耦

业务继续依赖协议和Record。SQLite命名实现改为通用SQLAlchemy实现，PostgreSQL特有锁语句封装在Repository内部。

### 后台任务保留PostgreSQL租约

不使用Redis替换任务系统。任务领取使用`FOR UPDATE SKIP LOCKED`，租约、重试、取消和事件历史继续由PostgreSQL负责。

### Transactional Outbox连接Redis Streams

业务状态、持久事件和Outbox在同一事务写入。Dispatcher异步发布统一Stream，发布失败保留Outbox并退避重试。消费者以稳定event id和业务sequence去重。

### Redis只提供可降级能力

Redis提供Streams、MCP/检索缓存和Token Bucket。缓存异常时回源；Streams异常时SSE轮询PostgreSQL；恢复类写操作在限流不可用时默认拒绝。

## Risks / Trade-offs

- 本地基础设施增加 -> Compose提供健康检查和统一启动命令。
- 双通道事件可能重复 -> 使用event id与job sequence去重。
- 既有迁移可能带SQLite假设 -> 在真实PostgreSQL逐版本执行。
- Redis故障会增加数据库轮询负载 -> 降级模式使用退避和有界批量。

## Migration Plan

1. 添加PostgreSQL/Redis Compose服务和依赖。
2. 切换配置与Alembic。
3. 迁移Repository及测试。
4. 重写任务领取并验证并发。
5. 添加Outbox与Dispatcher。
6. 接入Streams、SSE降级、缓存和限流。
7. 更新readiness、文档和CI。

