## MODIFIED Requirements

### Requirement: PostgreSQL application schema

后端 SHALL 使用SQLAlchemy ORM和PostgreSQL 16保存知识文档、索引任务、聊天、AIOps诊断、证据、报告、工具审计、LangGraph checkpoint、后台任务和Outbox；SQLite SHALL NOT 作为运行时或集成测试数据库。

#### Scenario: Fresh PostgreSQL database is migrated

- **WHEN** Alembic将空PostgreSQL数据库升级至head
- **THEN** 所有应用Repository和运行时所需的表、约束和索引 MUST 存在。

#### Scenario: Structured payloads are persisted

- **WHEN** 业务记录包含结构化工作流负载
- **THEN** PostgreSQL MUST 使用结构化JSON类型完成无损往返。

### Requirement: Application database project configuration

后端 SHALL 从跟踪的项目配置读取通用数据库URL，并 SHALL 使用PostgreSQL异步驱动。

#### Scenario: Runtime and Alembic share configuration

- **WHEN** 应用和Alembic解析数据库配置
- **THEN** 两者 MUST 使用相同的`backend.databaseUrl`且URL MUST 使用`postgresql+asyncpg`。

### Requirement: Database-independent repository boundary

后端 SHALL 暴露Repository协议和Record，使业务代码不依赖PostgreSQL SQL、SQLAlchemy ORM或数据库实现类。

#### Scenario: Business service uses migrated repositories

- **WHEN** 存储实现从SQLite迁移至PostgreSQL
- **THEN** 业务服务方法签名 MUST 保持稳定，且实现名称 MUST NOT 继续表达SQLite专属语义。

