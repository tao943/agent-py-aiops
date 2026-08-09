# docker-compose-startup Specification

## Purpose
定义标准的 Docker Compose 启动边界：由仓库管理的基础设施服务在本地运行，应用服务直接在主机上运行。
## Requirements
### Requirement: Unified compose startup

项目 SHALL 在 `infra` 下提供一个 Docker Compose 文件，管理 PostgreSQL、Redis、etcd、MinIO、Milvus、Attu、Alertmanager 和 Nginx 本地基础设施。Nginx SHALL 作为访问宿主机 FastAPI 的统一开发入口；后端、前端和官方 CLS MCP Server SHALL 继续由本地启动器直接在宿主机运行，Compose SHALL NOT 构建或运行应用程序镜像。

#### Scenario: Compose 文件定义了基础设施服务

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

### Requirement: Milvus stack managed by compose
Compose 堆栈 SHALL 通过 Docker Compose 使用可本地拉取的独立 Milvus 镜像标签来管理 Milvus 独立服务及其所需依赖项。

#### Scenario: Milvus dependencies are compose services
- **WHEN** 检查 Compose 文件  
- **THEN** etcd 和 MinIO MUST 应被声明为 Milvus 所依赖的服务。

#### Scenario: Milvus and Attu ports are exposed
- **WHEN** 检查 Compose 文件
- **THEN** Milvus MUST 暴露端口 19530，而 Attu MUST 暴露本地 UI 端口。

#### Scenario: Milvus 镜像标签支持本地启动
- **WHEN** 启动本地 Compose Milvus 服务
- **THEN** 配置的 Milvus 独立镜像标签 MUST 应该可拉取并通过 Compose health 检查报告 healthy。

