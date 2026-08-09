## Why

前端当前直接访问本机 FastAPI，缺少能够在请求进入应用前吸收恶意流量和突发流量的统一入口。后端已有 Redis Token Bucket，但它面向认证用户和高成本业务动作，不能替代按来源 IP 工作的边缘保护。

## What Changes

- 在 `infra/compose.yaml` 中增加官方 Nginx Open Source 服务，将 `127.0.0.1:8080` 作为本地 API 网关。
- 按客户端 IP 分别限制普通 API、登录/注册和 Chat/AIOps SSE 建连，超限返回 HTTP 429。
- 保持 FastAPI 现有 Redis Token Bucket，形成网关入口保护与应用资源控制两层限流。
- 为 SSE 禁用代理缓冲与缓存，并保留 10 MB 文档上传能力。
- 记录安全的网关、上游和限流状态，支持区分 Nginx 429、应用 429、502 和 504。
- 将前端、演示脚本、本地启动器、CI 和运行文档切换到统一网关入口。

故障实验室、生产问题模拟和 Agent Eval 不属于本变更。

## Capabilities

### New Capabilities

- `edge-rate-limit-gateway`: 定义 Nginx 入口、分级 IP 限流、SSE/上传代理行为和安全日志。

### Modified Capabilities

- `docker-compose-startup`: Compose 增加 Nginx 基础设施入口，应用进程仍在宿主机运行。
- `local-development-operations-guide`: 本地配置、启动器和文档使用 8080 网关入口并说明 8000 直连边界。

## Impact

- `infra/nginx/`、`infra/compose.yaml` 和 Compose CI 验证。
- `config/project.template.json` 与被忽略的本机 `config/project.json`。
- macOS/Linux 与 Windows 本地启动器。
- GitHub Actions 路径检测、Nginx 配置检查和 `CI Gate`。
- 根 README、基础设施指南、OpenSpec 与 WIKI。
