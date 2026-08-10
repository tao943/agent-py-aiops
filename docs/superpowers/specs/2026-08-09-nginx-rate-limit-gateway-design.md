# Nginx 入口限流网关设计

## 背景

Agent Py 当前采用本地优先运行方式：Vue/Vite 前端监听 `127.0.0.1:5173`，
FastAPI 后端监听 `127.0.0.1:8000`，PostgreSQL、Redis、Milvus 和
Alertmanager 等基础设施由 `infra/compose.yaml` 管理。前端目前直接请求 FastAPI，
因此没有独立的入口层来吸收恶意请求或突发流量。

后端已经使用 Redis Token Bucket 对 `chat.stream`、`diagnostic.create`、
`mcp.tool_call` 和 `recovery.execute` 等高成本动作实施用户级资源限流。本变更不替换
这套机制，而是在它之前增加按客户端 IP 限制的 Nginx 网关层。

故障测评、故障注入和生产问题模拟不属于本变更范围，后续作为独立能力设计。

## 目标

- 在当前“前后端仍为本机进程”的开发模式中增加统一 API 入口。
- 对普通 API、认证入口和 Agent 流式建连实施不同强度的 IP 级限流。
- 超过网关限制时返回 HTTP 429。
- 保持 Chat 和 AIOps SSE 实时传输，不因代理缓冲而延迟。
- 保持最大 10 MB 文档上传能力。
- 通过日志区分网关限流、应用限流和上游失败。
- 为以后将前后端容器化或部署到单台服务器保留清晰迁移边界。

## 非目标

- 不实现 HTTPS、证书签发或域名配置。
- 不将前端或后端容器化。
- 不实现多实例负载均衡。
- 不实现基于 JWT 用户身份的 Nginx 限流。
- 不引入 Nginx Plus、Lua、OpenResty、Traefik 或 Docker Socket 动态发现。
- 不修改后端 Redis Token Bucket 的策略。
- 不实现故障实验室、自动评分或 Agent Eval。

## 复用评估

### 约束

- Windows Docker Desktop 是当前主要开发环境，同时应兼容 Linux Docker Engine。
- 后端仍是宿主机进程，不能要求它加入 Compose 网络。
- SSE 和 multipart 文件上传必须保持现有协议。
- 方案不应要求 Docker Socket、额外数据库或新的应用依赖。
- 仓库当前没有声明项目级许可证；本变更不复制第三方实现代码。

### 候选方案

1. **官方 Nginx Docker 镜像和静态配置**：Nginx 核心模块原生提供
   `limit_req_zone`、`limit_req` 和反向代理能力。官方 Docker 镜像采用
   BSD-2-Clause 许可，适合一个明确上游的当前结构。
2. **nginx-proxy**：可以通过 Docker 元数据动态生成代理配置，但通常需要读取
   Docker Socket；当前后端不是容器，动态发现没有收益，额外权限与模板层增加风险。
3. **Traefik**：适合大量动态容器、服务发现和 Kubernetes，但当前只有一个固定的
   FastAPI 上游，配置面和运行能力超过本阶段需求。

### 决策

直接采用官方 `nginx:1.30-alpine` stable 系列镜像，挂载项目自有静态配置。配置只使用
Nginx Open Source 核心模块，不增加 Python 或 Node 依赖，也不使用无约束的
`latest` 标签。

## 架构

```text
Vue/Vite 127.0.0.1:5173
             |
             | HTTP API / SSE
             v
Nginx 127.0.0.1:8080 (Docker Compose)
             |
             | host.docker.internal:8000
             v
FastAPI 127.0.0.1:8000 (宿主机进程)
```

Nginx 作为 `infra/compose.yaml` 中的无状态服务运行，通过只读卷加载配置。端口发布为
`127.0.0.1:8080:80`，避免默认暴露到局域网。上游使用
`host.docker.internal:8000`；Compose 同时配置 `host-gateway`，使 Linux Docker
Engine 能解析相同主机名。

FastAPI 继续只监听 loopback。当前开发机上的本地进程仍可直接访问 `:8000`，但远程
请求只能通过未来服务器防火墙明确开放的 Nginx 入口。生产部署时应将后端放入私有容器
网络或通过主机防火墙禁止外部访问 `:8000`，不依赖本设计中的开发机端口边界作为生产
安全策略。

## 限流策略

Nginx 以 `$binary_remote_addr` 为键建立三个共享内存区域：

| 区域 | 适用路由 | 速率 | Burst | 行为 |
| --- | --- | ---: | ---: | --- |
| `api_per_ip` | 除特殊路由外的 API | 20 r/s | 40 | 超限立即返回 429 |
| `auth_per_ip` | `/auth/login`、`/auth/register` | 10 r/m | 5 | 超限立即返回 429 |
| `stream_per_ip` | Chat/AIOps SSE 建连 | 5 r/s | 10 | 超限立即返回 429 |

`limit_req_status 429` 统一网关超限响应状态。Burst 使用 `nodelay`，允许正常浏览器的
短暂并发请求立即通过，但超过令牌和 Burst 的请求不在 Nginx 内排队。

`/health`、`/ready` 和 Nginx 自身健康检查不应用请求限流，防止健康探针在服务繁忙时
被误判。`/metrics` 仍走普通 API 限流，避免无界抓取。

网关限流按来源 IP 保护入口，后端 Redis Token Bucket 按认证用户和业务动作控制昂贵
资源。请求通过 Nginx 后仍必须经过应用限流，两层任意一层都可以返回 429。

## 路由和代理行为

### 普通 API

默认 location 将请求代理到 FastAPI，设置：

- `Host`
- `X-Real-IP`
- `X-Forwarded-For`
- `X-Forwarded-Proto`

第一版不信任客户端主动提供的转发头来计算限流键。未来若 Nginx 前面增加云负载均衡，
必须独立配置可信代理地址后再使用真实客户端 IP。

### 认证接口

`/auth/login` 和 `/auth/register` 使用精确匹配 location，以更严格的认证限流区保护。
其他认证接口走普通 API 策略，避免正常的登录状态恢复和注销操作受到分钟级认证限流。

### SSE 接口

Chat 和 AIOps 流式端点使用独立 location，并配置：

- `proxy_http_version 1.1`
- `proxy_buffering off`
- `proxy_cache off`
- 足够长的 `proxy_read_timeout`
- 禁止对上游 SSE 响应做聚合缓冲

Nginx 的请求限流只在建立流连接时计数，不按每个 SSE 事件计数。应用现有流式终止、错误
和重连语义保持不变。

### 上传

设置 `client_max_body_size 12m`。后端继续执行 10 MB 文件内容限制；额外空间只用于
multipart boundary 和表单字段开销，Nginx 不取代应用的文件类型与大小校验。

## 错误行为

- 网关超限：Nginx 返回 HTTP 429，并记录限流状态。
- 应用超限：请求到达 FastAPI，由现有统一错误 envelope、`Retry-After` 和
  `X-RateLimit-Remaining` 处理。
- 后端未启动或连接失败：Nginx 返回 502。
- 后端响应超时：Nginx 返回 504。
- Nginx 配置错误：容器健康检查失败，配置验证阻止交付。

第一版不强制把 Nginx 自身的 429 包装为 FastAPI JSON envelope，因为网关必须能够在
后端不可用时独立拒绝流量。前端 API 客户端应继续按 HTTP 状态处理非 JSON 或网关错误。

## 可观测性

Nginx 访问日志使用有界字段记录：

- 时间和请求 ID（存在时）
- 客户端地址
- 请求方法和 URI
- HTTP 状态
- 请求总耗时
- 上游地址、状态和响应耗时
- `$limit_req_status`

日志不得记录 Authorization、Cookie、请求体或查询内容的展开值。通过状态和
`limit_req_status` 可以区分 Nginx 429 与上游 FastAPI 429；通过 `upstream_status`
区分上游 502/504。

Nginx 自身提供一个固定的内部健康端点，只验证网关进程和配置。FastAPI 的 `/health` 和
`/ready` 继续经 Nginx 代理，分别验证应用存活和依赖就绪状态。

## 配置和启动流程

- 在 `infra/nginx/` 保存可提交的 Nginx 配置。
- 在 `infra/compose.yaml` 增加 Nginx 服务、只读配置挂载、loopback 端口和健康检查。
- `scripts/start-local.sh` 与 `scripts/start-local.bat` 启动 Nginx，并把用户可访问的后端
  地址显示为 `http://127.0.0.1:8080`。
- `config/project.template.json` 的前端 API 地址和演示脚本后端地址指向网关。
- 为当前开发机同步被 Git 忽略的 `config/project.json`，但不提交其中的本地配置。
- README 和 `infra/README.md` 解释两层限流、端口、验证方法和直连边界。

## 测试和验收

### 静态验证

- `docker compose -f infra/compose.yaml config` 成功。
- 使用 Compose 中相同镜像和主机映射执行 `nginx -t` 成功。
- OpenSpec change 通过 `openspec validate --all`。
- 文档通过 VitePress build。

### 运行验证

在 FastAPI 和 Nginx 启动后验证：

1. `GET http://127.0.0.1:8080/health` 返回后端健康响应。
2. 普通 API 的受控突发流量能够观察到 HTTP 429。
3. 登录接口达到分钟级限制后返回 HTTP 429。
4. Chat/AIOps SSE 首包和后续事件不被 Nginx 缓冲。
5. 允许的 multipart 文档上传不会被 Nginx 默认 1 MB 限制拒绝。
6. Nginx 访问日志能够区分 `PASSED` 和 `REJECTED` 限流状态。
7. 原有后端 Redis 限流聚焦测试继续通过。

### CI

在现有路径感知 CI 中加入 Nginx 配置和 Compose 结构验证。CI 不启动真实 LLM、CLS、
Milvus 或完整应用栈，只验证可确定的网关配置；需要真实后端的突发限流和 SSE 验收保留为
本地集成验证。

## 风险与后续迁移

- 同一 NAT 出口的多个用户共享 IP 限额；第一版使用较宽松普通 API 限额降低误伤。
- 当前后端仍可由本机直接访问，Nginx 不是本机进程之间的强制安全边界。
- Nginx 重启会清空内存限流状态；入口限流只用于削峰和基础保护，不是计费或持久配额。
- 多台 Nginx 实例之间不会共享限流计数。以后进入多实例或云部署时，应重新评估云网关、
  Ingress 或分布式限流，而不是把本设计直接视为全局配额方案。
- HTTPS、真实客户端 IP 信任链、多实例上游和故障实验室分别作为后续独立变更处理。
