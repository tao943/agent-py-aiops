## MODIFIED Requirements

### Requirement: Local-first developer startup guide

仓库 SHALL 提供中文的根级本地优先开发者指南，从 Compose 管理的 PostgreSQL、Redis、etcd、MinIO、Milvus、Attu、Alertmanager 和 Nginx 开始，并在宿主机直接运行后端、Vue 前端和官方本地 CLS MCP Server。该指南 MUST 用中文标识本地 URL 和状态，表明应用服务未通过 Compose 启动。

#### Scenario: Developer follows ordinary local startup

- **WHEN** 开发人员从已安装好文档中所述先决条件的全新代码库中遵循中文根 README
- **THEN** 指南 MUST 为前端依赖项、后端依赖项和迁移、基础设施与网关依赖项、本地 MCP、后端和前端提供命令，而无需在 Compose 中使用应用服务

#### Scenario: Developer needs infrastructure services

- **WHEN** 开发者调用记录在案的 Compose 命令
- **THEN** 它 MUST 启动 PostgreSQL、Redis、etcd、MinIO、Milvus、Attu、Alertmanager 和 Nginx，且 MUST NOT 启动 backend、frontend 或 CLS MCP Server 容器

### Requirement: Cross-platform local launchers

仓库 SHALL 提供 macOS/Linux shell 启动器和 Windows 命令启动器，通过 Compose 准备本地基础设施和 Nginx 网关，运行数据库迁移，并在宿主机直接启动 CLS MCP Server、FastAPI 后端和 Vue 前端。启动器 SHALL 将 `http://127.0.0.1:8080` 报告为 API 网关，将 `http://127.0.0.1:8000` 标识为仅限本机调试的后端直连地址，并 MUST NOT 打印凭据。

#### Scenario: macOS or Linux developer invokes the launcher

- **WHEN** 一名开发者在安装了所需工具的仓库根目录下运行 `scripts/start-local.sh`
- **THEN** 它 MUST 通过 Compose 启动基础设施和 Nginx，通过宿主机进程启动 MCP、后端和前端

#### Scenario: Windows developer invokes the launcher

- **WHEN** 开发人员在安装了所需工具的仓库根目录中运行 `scripts\start-local.bat`
- **THEN** 它 MUST 启动相同的依赖项、网关和宿主机进程集，而无需使用 Unix shell

#### Scenario: Developer reads launcher output

- **WHEN** 启动程序完成进程启动
- **THEN** 它 MUST 报告前端、API 网关、后端直连、MCP 和本地日志地址，并 MUST NOT 打印凭据

## ADDED Requirements

### Requirement: Gateway-aware local configuration

可跟踪项目配置模板 SHALL 将前端 API 和 AIOps 演示脚本后端地址设置为 `http://127.0.0.1:8080`，同时 SHALL 保持 FastAPI 自身监听 `127.0.0.1:8000`。

#### Scenario: Recipient creates local configuration from templates

- **WHEN** 开发者从模板创建本地 `config/project.json`
- **THEN** 浏览器和显式演示脚本 MUST 默认通过 Nginx，且后端进程 MUST 继续使用 8000
