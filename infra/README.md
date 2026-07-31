# Docker Compose 基础设施

`infra/compose.yaml` 管理七个本地基础设施服务：PostgreSQL、Redis、etcd、
MinIO、Milvus、Attu 和 Alertmanager。后端、前端与腾讯云 CLS MCP Server
仍作为本机应用进程运行，不进入 Compose。

## 启动全部基础设施

在仓库根目录执行：

```bash
docker compose -f infra/compose.yaml up -d postgres redis etcd minio milvus attu alertmanager
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

## PostgreSQL 开发数据库

```bash
docker compose -f infra/compose.yaml up -d postgres
docker compose -f infra/compose.yaml ps postgres
```

Compose 报告 `healthy` 后再执行 Alembic。开发凭据为数据库 `agent_py`、用户
`agent_py`、密码 `agent_py_dev`；独立集成测试数据库为 `agent_py_test`。

`infra/postgres/init/` 中的 SQL 只会在新的 `postgres-data` 卷初始化时执行。
项目采用 fresh-database 策略，不提供 SQLite 数据导入或双写路径。

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

应用配置来自 `config/project.json` 与相关本地 JSON 文件，不从 `.env` 读取。
文档上传、知识索引、CLS 日志上传与 Alertmanager 示例均为显式运行时流程，不会在
启动时自动执行。
