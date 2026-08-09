# Docker Compose 基础设施

`infra/compose.yaml` 管理八个本地基础设施服务：PostgreSQL、Redis、etcd、
MinIO、Milvus、Attu、Alertmanager 和 Nginx。后端、前端与腾讯云 CLS MCP Server
仍作为本机应用进程运行，不进入 Compose。

## 启动全部基础设施

在仓库根目录执行：

```bash
docker compose -f infra/compose.yaml up -d postgres redis etcd minio milvus attu alertmanager nginx
docker compose -f infra/compose.yaml ps
```

主要服务地址：

- PostgreSQL：`localhost:5432`，开发数据库 `agent_py`
- Redis：`localhost:6379`，开发数据库 `/0`
- Milvus：`localhost:19530`
- MinIO 控制台：`http://localhost:9001`
- Attu：`http://localhost:8001`
- Alertmanager：`http://localhost:9093`
- Milvus Web UI/指标端口：`http://localhost:9091`
- Nginx API 网关：`http://127.0.0.1:8080`

## Nginx API 网关

Nginx 使用官方 `nginx:1.30-alpine` 镜像，只发布 `127.0.0.1:8080:80`，通过
`host.docker.internal:8000` 访问宿主机 FastAPI。Linux Docker Engine 使用 Compose
中的 `host-gateway` 映射；backend、frontend 和 CLS MCP Server 仍不进入 Compose。

```bash
docker compose -f infra/compose.yaml up -d nginx
docker compose -f infra/compose.yaml ps nginx
docker compose -f infra/compose.yaml logs nginx
docker compose -f infra/compose.yaml exec nginx nginx -T
```

限流按客户端 IP 分级：普通 API 为 20 r/s、burst 40；登录和注册为 10 r/m、
burst 5；Chat/AIOps SSE 建连为 5 r/s、burst 10。`/nginx-health`、`/health` 和
`/ready` 不限流。超限请求由 Nginx 直接返回 429；FastAPI 内部 Redis Token Bucket
继续按认证用户控制高成本 Agent 资源。

SSE 路由关闭 buffering/cache，读取超时为 600 秒。`client_max_body_size 12m`
允许 10 MB 文档及 multipart 开销，后端继续执行内容限制。访问日志记录 URI（不含
查询参数）、响应/上游状态、耗时、request ID 和 `limitStatus`，不记录认证头、Cookie
或请求体。

- `limitStatus=REJECTED`：Nginx 入口限流。
- 上游 429：FastAPI/Redis 应用限流。
- 502：宿主机 FastAPI 未启动或 Nginx 无法连接 8000。
- 504：FastAPI 或下游处理超过代理超时。

## PostgreSQL 开发数据库

```bash
docker compose -f infra/compose.yaml up -d postgres
docker compose -f infra/compose.yaml ps postgres
```

Compose 报告 `healthy` 后再执行 Alembic。开发凭据为数据库 `agent_py`、用户
`agent_py`、密码 `agent_py_dev`；独立集成测试数据库为 `agent_py_test`。

`infra/postgres/init/` 中的 SQL 只会在新的 `postgres-data` 卷初始化时执行。
项目采用 fresh-database 策略，不提供旧数据库导入或双写路径。

## Redis 可恢复运行时

```bash
docker compose -f infra/compose.yaml up -d redis
docker compose -f infra/compose.yaml ps redis
docker compose -f infra/compose.yaml exec redis redis-cli ping
```

Redis 使用 Redis 7、AOF 持久化和命名卷 `redis-data`。Compose 应报告
`healthy`，`redis-cli ping` 应返回 `PONG`。

Redis 仅承担缓存、限流与低延迟事件传输。PostgreSQL 始终是任务和用户可见事件
的事实源；Redis 数据丢失或重建不得造成业务事实丢失，也不得把 Redis 当作持久任务队列。
测试配置使用 Redis 数据库 `/15`，测试清理只能清理该数据库或专用键前缀。

## 停止与清理

停止服务但保留数据：

```bash
docker compose -f infra/compose.yaml down
```

只有明确需要清除全部本地基础设施状态时才删除命名卷：

```bash
docker compose -f infra/compose.yaml down -v
```

删除卷会清除 PostgreSQL 与 Redis 等本地数据，操作不可恢复。

## 启动本机应用

基础设施健康后，可运行：

```bash
./scripts/start-local.sh
```

Windows：

```text
scripts\start-local.bat
```

## Redis Streams recovery runbook

The runtime stream key is `<streamPrefix>:aiops:events` (default
`agent-py:aiops:events`), with approximate `MAXLEN ~ streamMaxlen` retention
(default `10000`). Dedupe keys are
`<streamPrefix>:aiops:events:dedupe:<event-id>` and expire after
`eventDedupeTtlSeconds` (default `86400`). SSE relay groups are
`<streamPrefix>:sse:<instance-id>`; inspect them safely with:

```bash
docker compose -f infra/compose.yaml exec redis redis-cli XINFO GROUPS agent-py:aiops:events
```

Destroy only a group belonging to a confirmed-crashed instance; do not delete the
shared stream. Redis has AOF and the named `redis-data` volume, but PostgreSQL—not
Redis—is the durable source of job and API-event facts.

When Redis is unavailable, keep PostgreSQL and the backend running: Outbox rows stay
unpublished, PostgreSQL-backed SSE remains available, and readiness is `degraded`
rather than unready. Restore Redis and verify it before waiting for the dispatcher:

```bash
docker compose -f infra/compose.yaml up -d redis
docker compose -f infra/compose.yaml ps redis
docker compose -f infra/compose.yaml exec redis redis-cli ping
docker compose -f infra/compose.yaml exec postgres psql -U agent_py -d agent_py -c "SELECT id, aggregate_id, sequence, attempt_count, available_at, claimed_by, claim_expires_at, last_error FROM outbox_events WHERE published_at IS NULL ORDER BY created_at, id;"
```

Watch backend logs for `Outbox publication failed`, `Outbox publication acknowledged`,
and `Redis SSE relay degraded`; they contain IDs, attempts, and latency but not event
payloads. Runtime allocation is Redis `/0`; tests use `/15` plus a UUID-qualified
prefix. Test cleanup must scan and delete only that prefix's keys—never run
`FLUSHDB` on a shared service.

应用配置来自 `config/project.json` 与相关本地 JSON 文件，不从 `.env` 读取。
文档上传、知识索引、CLS 日志上传与 Alertmanager 示例均为显式运行时流程，不会在
启动时自动执行。
