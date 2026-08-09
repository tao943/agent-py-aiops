## MODIFIED Requirements

### Requirement: Cross-platform local launchers

仓库 SHALL 提供 macOS/Linux shell 启动器和 Windows 命令启动器，通过 Compose 准备本地基础设施和 Nginx 网关，运行数据库迁移，并在宿主机直接启动 CLS MCP Server、FastAPI 后端和 Vue 前端。启动器 SHALL 将 `http://127.0.0.1:8080` 报告为 API 网关，将 `http://127.0.0.1:8000` 标识为仅限本机调试的后端直连地址，并 MUST NOT 打印凭据。

#### Scenario: Developer invokes either launcher

- **WHEN** 开发者运行 `scripts/start-local.sh` 或 `scripts\start-local.bat`
- **THEN** 启动器 MUST 通过 Compose 启动 Nginx，通过宿主机进程启动 MCP、后端和前端，并 MUST 打印前端、API 网关、后端直连、MCP 和日志地址

### Requirement: Gateway-aware local configuration

可跟踪项目配置模板 SHALL 将前端 API 和 AIOps 演示脚本后端地址设置为 `http://127.0.0.1:8080`，同时 SHALL 保持 FastAPI 自身监听 `127.0.0.1:8000`。

#### Scenario: Recipient creates local configuration from templates

- **WHEN** 开发者从模板创建本地 `config/project.json`
- **THEN** 浏览器和显式演示脚本 MUST 默认通过 Nginx，且后端进程 MUST 继续使用 8000
