## ADDED Requirements

### Requirement: Local Nginx API gateway

系统 SHALL 在 FastAPI 前提供 Compose 管理的 Nginx Open Source 网关，并 SHALL 将 `127.0.0.1:8080` 作为本地前端和演示脚本的 API/SSE 入口。网关 SHALL 代理宿主机 `127.0.0.1:8000` 的 FastAPI，且 SHALL NOT 将应用进程移入 Compose。

#### Scenario: Developer uses the local gateway

- **WHEN** Nginx 和宿主机 FastAPI 已启动
- **THEN** 对 `http://127.0.0.1:8080/health` 的请求 MUST 到达 FastAPI 并返回应用健康响应

#### Scenario: Gateway binds the development interface

- **WHEN** 检查 Compose 发布端口
- **THEN** Nginx MUST 只发布 `127.0.0.1:8080:80`，并 MUST 使用 `host.docker.internal:8000` 访问宿主机上游

### Requirement: Tiered edge request limits

系统 SHALL 使用客户端 IP 分别限制普通 API、认证入口和 Agent SSE 建连：普通 API MUST 为 20 r/s 与 burst 40，`/auth/login` 和 `/auth/register` MUST 为 10 r/m 与 burst 5，Chat/AIOps SSE 建连 MUST 为 5 r/s 与 burst 10。超限请求 MUST 立即返回 HTTP 429。

#### Scenario: General API burst exceeds the allowance

- **WHEN** 同一客户端 IP 的普通 API 突发请求超过 20 r/s 与 burst 40
- **THEN** Nginx MUST 返回 HTTP 429，并 MUST 在限流日志状态中记录拒绝

#### Scenario: Authentication requests exceed the allowance

- **WHEN** 同一客户端 IP 对 `/auth/login` 或 `/auth/register` 超过 10 r/m 与 burst 5
- **THEN** Nginx MUST 返回 HTTP 429，且被网关拒绝的请求 MUST NOT 到达 FastAPI

#### Scenario: Agent stream connections exceed the allowance

- **WHEN** 同一客户端 IP 对 Chat 或 AIOps SSE 的建连超过 5 r/s 与 burst 10
- **THEN** Nginx MUST 返回 HTTP 429，且 MUST NOT 按流内 SSE 事件重复计数

### Requirement: Health check exemptions

Nginx 自身 `/nginx-health`、FastAPI `/health` 和 `/ready` SHALL 不应用请求限流；`/metrics` SHALL 继续使用普通 API 限流。

#### Scenario: Health probes run during a traffic burst

- **WHEN** 普通 API 限流区正在拒绝突发请求
- **THEN** `/nginx-health`、`/health` 和 `/ready` MUST 仍按各自健康语义响应

### Requirement: Streaming and upload compatibility

网关 SHALL 对 Chat/AIOps SSE 使用 HTTP/1.1，关闭代理缓冲和缓存，并使用 600 秒读取超时。网关 SHALL 设置 `client_max_body_size 12m`，后端 SHALL 继续执行 10 MB 文件内容限制。

#### Scenario: Agent stream remains live through the gateway

- **WHEN** 客户端通过网关建立 Chat 或 AIOps SSE
- **THEN** Nginx MUST 按事件转发响应而不聚合缓冲

#### Scenario: Allowed document uses multipart overhead

- **WHEN** 客户端通过网关上传后端允许的最大 10 MB 文档
- **THEN** Nginx MUST NOT 因默认 1 MB 请求体限制拒绝该请求

### Requirement: Safe gateway observability

Nginx SHALL 记录客户端地址、方法、无查询参数 URI、响应状态、请求耗时、上游状态、上游耗时、响应 request ID 和限流状态，并 SHALL NOT 记录 Authorization、Cookie 或请求体。

#### Scenario: Operator distinguishes rejection sources

- **WHEN** 请求收到 429、502 或 504
- **THEN** 网关日志 MUST 能区分 Nginx 限流拒绝、FastAPI 上游响应和上游连接或超时失败
