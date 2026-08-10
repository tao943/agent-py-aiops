## Context

Agent Py 的 Vue/Vite 前端、FastAPI 后端和 CLS MCP Server 作为宿主机进程运行，Compose 管理 PostgreSQL、Redis、Milvus 等基础设施。前端配置当前直接指向 `127.0.0.1:8000`。FastAPI 内部 Redis 限流按认证 owner 和业务 action 控制资源消耗，但应用之前没有 IP 级入口保护。

## Goals / Non-Goals

**Goals:**

- 使用官方 Nginx Open Source 镜像增加本地统一 API 入口。
- 对普通 API、认证入口和 SSE 建连实施开发友好的分级限流。
- 保持 SSE 实时性、上传边界和现有应用限流语义。
- 提供可复现的 Compose、CI、日志和运行验证。

**Non-Goals:**

- 不实现 HTTPS、域名、云负载均衡或多实例上游。
- 不容器化 backend、frontend 或 CLS MCP Server。
- 不读取 Docker Socket，不引入 Nginx Plus、Lua、OpenResty 或 Traefik。
- 不实现故障注入、自动评分或 Agent Eval。

## Decisions

### 官方静态 Nginx 配置

采用 `nginx:1.30-alpine`，挂载项目自有只读配置。单一固定上游不需要动态服务发现；静态配置降低权限和供应链表面积。

### 宿主机上游边界

Nginx 发布 `127.0.0.1:8080:80`，使用 `host.docker.internal:8000` 访问 FastAPI，并通过 `host-gateway` 兼容 Linux Docker Engine。FastAPI 继续监听 loopback；生产部署必须用私有容器网络或防火墙禁止外部直连 8000。

### 两层限流职责

Nginx 使用 `$binary_remote_addr`：普通 API 为 20 r/s、burst 40；登录/注册为 10 r/m、burst 5；Chat/AIOps SSE 建连为 5 r/s、burst 10。所有网关超限立即返回 429。FastAPI Redis Token Bucket 不变，继续按用户限制高成本动作。

### 健康、SSE 和上传

`/nginx-health` 只检查网关进程；`/health` 和 `/ready` 不限流并代理到 FastAPI；`/metrics` 走普通限流。SSE 使用 HTTP/1.1、关闭 buffering/cache、读取超时 600 秒。Nginx 请求体上限为 12m，后端继续执行 10 MB 文件内容限制。

### 安全日志

访问日志只记录时间、客户端地址、方法、无查询参数 URI、状态、耗时、上游状态/耗时、响应 request ID 和 `$limit_req_status`。不记录认证头、Cookie 或请求体。

## Risks / Trade-offs

- NAT 后多个用户共享 IP 配额；普通 API 使用宽松速率降低误伤。
- 开发机本地进程可绕过 8080 直连 8000；这是本机调试边界，不作为生产安全边界。
- Nginx 重启会清空限流状态；网关限流用于削峰，不作为持久配额或计费依据。
- 多 Nginx 实例不共享计数；未来云部署需重新评估云网关、Ingress 或分布式入口限流。

## Migration Plan

1. 新增并验证 Nginx 配置与 Compose 服务。
2. 将跟踪配置模板、本机配置和启动器切换到 8080。
3. 扩展路径感知 CI，使用 Compose 和 `nginx -t` 验证配置。
4. 更新运行文档，执行健康、限流、SSE 和上传验收。
5. 验证并归档 OpenSpec change。
