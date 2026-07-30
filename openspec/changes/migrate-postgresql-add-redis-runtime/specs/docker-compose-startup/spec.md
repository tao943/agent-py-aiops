## MODIFIED Requirements

### Requirement: Unified infrastructure compose startup

项目 SHALL 在`infra/compose.yaml`中管理etcd、MinIO、Milvus、Attu、Alertmanager、PostgreSQL 16和Redis 7；后端、前端和CLS MCP Server SHALL 继续在主机运行。

#### Scenario: Compose defines PostgreSQL and Redis

- **WHEN** 检查Compose服务
- **THEN** PostgreSQL和Redis MUST 具有固定主版本、健康检查、开发端口和命名持久卷。

#### Scenario: Application services remain outside Compose

- **WHEN** 检查Compose拓扑
- **THEN** 它 MUST NOT 构建或运行backend、frontend或CLS MCP Server。

