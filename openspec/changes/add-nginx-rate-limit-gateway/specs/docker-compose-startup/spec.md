## MODIFIED Requirements

### Requirement: Unified compose startup

项目 SHALL 在 `infra` 下提供一个 Docker Compose 文件，管理 PostgreSQL、Redis、etcd、MinIO、Milvus、Attu、Alertmanager 和 Nginx 本地基础设施。Nginx SHALL 作为访问宿主机 FastAPI 的统一开发入口；后端、前端和官方 CLS MCP Server SHALL 继续由本地启动器直接在宿主机运行，Compose SHALL NOT 构建或运行应用程序镜像。

#### Scenario: Compose defines infrastructure and gateway services

- **WHEN** `infra/compose.yaml` 被检查
- **THEN** 它 MUST 定义 PostgreSQL、Redis、etcd、MinIO、Milvus、Attu、Alertmanager 和 Nginx，并且 MUST NOT 定义 backend、frontend 或 `cls-mcp-server`

#### Scenario: Nginx configuration is mounted read-only

- **WHEN** 检查 Compose Nginx 服务
- **THEN** 它 MUST 使用官方固定 stable 系列镜像、只读项目配置挂载、自身健康检查和 loopback 发布端口

#### Scenario: Compose startup does not require local env files

- **WHEN** `infra/compose.yaml` 被检查
- **THEN** 它 MUST NOT 需要 `env_file`、`.env.example` 或 `--env-file` 作为项目应用程序配置输入

#### Scenario: Compose excludes application image runtime

- **WHEN** 检查 `infra` 中的 Compose 启动资产
- **THEN** 它们 MUST NOT 构建、引用或要求应用程序 Dockerfile 作为本地启动前提
