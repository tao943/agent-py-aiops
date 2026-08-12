# 后端

后端使用 Python、`uv` 和 src 布局。应用代码位于 `src/super_ai` 下，并通过包名称进行导入：

```python
from super_ai.foundation import get_foundation_info
```

## 命令

```bash
uv sync
uv run ruff check .
uv run pyright
uv run pytest
```

普通测试使用临时离线配置，不读取本地 API Key，也不会调用外部模型。需要验证本地
DashScope 配置以及真实的 Chat、Embedding 和 Rerank 模型时，显式运行：

```bash
uv run pytest -m live_llm tests/test_live_llm.py -q
```

真实模型测试会读取被 Git 忽略的 `config/project.json` 和
`config/user.project.json`，并消耗对应模型额度。

## 审核后知识卡批量导入

`scripts/import_knowledge_batch.py` 复用现有认证、文档上传和持久索引任务 API，
按文件名顺序导入指定目录第一层的 Markdown。它不会直接查询 PostgreSQL：任务创建
响应从 `data.task` 解析，后续任务查询从 `data.status` 解析。`pending` 和 `running`
继续轮询，`succeeded` 才计为成功；`failed`、`cancelled`、协议错误和截止时间超时
都会产生明确失败结果。HTTP timeout/network error 只在总截止时间内有限重试。

先进行不认证、不发 HTTP 请求的预览：

```powershell
uv run python scripts/import_knowledge_batch.py --source-dir ../../docs/knowledge-candidates --dry-run
```

确认文件列表后再执行真实导入：

```powershell
uv run python scripts/import_knowledge_batch.py --source-dir ../../docs/knowledge-candidates
```

默认首项失败后停止；需要收集整个批次结果时增加 `--continue-on-error`，只要任一文件
失败，最终退出码仍为非零。JSON 汇总只包含文件名、文档 ID、任务 ID、状态和安全错误，
不会输出 token、密码、API Key 或正文。

真实导入需要 PostgreSQL、Milvus、后端和 embedding 模型，会消耗模型额度，因此不属于
普通 CI。CLS MCP 不可用或 `/ready` 因 CLS 返回 503 不会阻塞文档索引。成功验收必须
同时确认 PostgreSQL 中 document 为 `indexed`、task 为 `succeeded`，并确认 Milvus 中
每个 document ID 至少有一个带 owner、tenant 和 knowledge-base scope 的 chunk。

## AgentPy DomainBench

首个评测切片包含 `APY-003` 与 `APY-006` 两个同为 Nginx 502、但根因不同的
Snapshot 场景。默认测试验证冻结工具、答案隔离、结构化证据链和确定性评分，不调用
真实模型；`application` CLI adapter 会复用现有 AIOps workflow 并消耗本地配置的
模型额度。运行方式、评分规则、PostgreSQL 审计查询和当前阶段边界见
[`docs/aiops/agentpy-domainbench.md`](../../docs/aiops/agentpy-domainbench.md)。

在应用迁移后运行本地 API：

```bash
mkdir -p var
uv run alembic upgrade head
uv run uvicorn super_ai.api:create_app --factory --reload
```

## LLM 提供者

后端在 `src/super_ai/llm` 下使用可替换的 LLM 提供商抽象。
默认提供程序是 `QwenOpenAIProvider`，由 LangChain 的 OpenAI-compatible 支持
`ChatOpenAI`。

跟踪的 Qwen 默认值位于 `config/project.json` 的 `llm` 部分下：

- `baseUrl`: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- `chatModel`: `qwen3.7-max`
- `embeddingModel`: `text-embedding-v4`
- `embeddingDimensions`: `1024`
- `rerankModel`: `qwen3-vl-rerank`
- `temperature`: `0.2`
- `timeoutSeconds`: `30`
- `maxRetries`: `2`
- `apiKey`: 私有仓库开发密钥

后端不会读取本地环境变量中的提供者设置。

就绪检查返回提供者、模型、端点、延迟和安全错误字符串
不包含凭据：

```python
from super_ai.llm import build_default_llm_provider

provider = build_default_llm_provider()
result = await provider.check_readiness()
```

## 内存存储

后端内存层存储聊天会话、消息、AIOps 诊断任务，
报告、工具调用审计以及 LangGraph checkpoints 的仓库背后
`src/super_ai/memory` 中的接口

默认的本地 PostgreSQL 数据库 URL 是：

```json
"databaseUrl": "postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py"
```

使用 Alembic 初始化或升级本地 PostgreSQL 模式：

```bash
mkdir -p var
uv run alembic upgrade head
```

业务服务应依赖 `MemoryRepositories`，
`ChatMemoryRepository`，或 `DiagnosticMemoryRepository` 从
`super_ai.memory.repositories`。SQLAlchemy/PostgreSQL 实现位于
`super_ai.memory.sqlalchemy`。

## Milvus 向量存储

Milvus 向量存储位于 `src/super_ai/vector_store` 下。导入 Milvus
包仅定义了设置、模式帮助程序和仓库风格的边界；
它不会创建 Milvus 客户端或连接到 Milvus。请
从显式的启动流程或
维护流程中调用 Milvus，当由 Compose 管理的 Milvus 服务就绪时。

默认本地设置：

```json
{
  "uri": "http://localhost:19530",
  "collectionName": "knowledge_chunks",
  "vectorDimension": 1024,
  "indexType": "HNSW",
  "metricType": "COSINE",
  "indexParams": {"M": 16, "efConstruction": 200},
  "searchParams": {"ef": 64}
}
```

chunk 集合将 tenant ownership 存储为标量字段
(`ownerUserId`, `tenantId`, `knowledgeBaseId`, `documentId`, `chunkId`) 以及在
chunk 元数据中，以便检索时可以根据经过身份验证的 tenant 范围进行过滤。

## 身份验证

API 暴露了：

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`

密码通过 `pwdlib` 使用 Argon2 进行哈希处理；明文密码不会被存储。
身份验证会话使用不透明的 bearer 令牌，该令牌一旦返回给前端就会被使用。
仅在 PostgreSQL 中存储 SHA-256 令牌哈希，因此注销可以撤销当前会话。
。

知识库、聊天和 AIOps API 路由需要：

```text
Authorization: Bearer <token>
```

## PostgreSQL runtime operations

PostgreSQL 16 is the only relational runtime. `agent_py` is the development
database and `agent_py_test` is reserved for isolated integration tests. From the
repository root, start and inspect PostgreSQL with:

```bash
docker compose -f infra/compose.yaml up -d postgres
docker compose -f infra/compose.yaml ps postgres
```

Before starting this backend, run `uv run alembic upgrade head` from this directory.
The schema is intended for fresh PostgreSQL databases: no SQLite importer, dual-write
path, or compatibility path is provided because no source database was supplied for
preservation.

The durable job repository claims queued work with PostgreSQL row locks and `FOR
UPDATE SKIP LOCKED`; it commits before handlers do external work. Event appends lock
the parent job row so each job's event sequence is serialized. Inspect the data with:

```bash
docker compose -f infra/compose.yaml exec postgres psql -U agent_py -d agent_py -c "SELECT id, status, lease_owner, lease_expires_at, created_at FROM background_jobs ORDER BY created_at, id;"
docker compose -f infra/compose.yaml exec postgres psql -U agent_py -d agent_py -c "SELECT job_id, sequence, payload->>'type' AS event_type, payload, created_at FROM background_job_events ORDER BY job_id, sequence, created_at, id;"
```

`/ready` reports `postgresql` and the actual `asyncpg` driver. `/config/check` uses
the same names when reporting PostgreSQL configuration and dependency status.

## Redis-backed event delivery and recovery

PostgreSQL remains the canonical event log. `append_event` commits the canonical
`background_job_events` row and its `outbox_events` row atomically. Redis Streams
only wakes local SSE subscribers; subscribers always re-read PostgreSQL by sequence
before emitting an event.

The default stream is `agent-py:aiops:events`; its approximate retention is bounded
by `redis.streamMaxlen` (default `10000`). The publisher creates
`agent-py:aiops:events:dedupe:<event-id>` keys for
`redis.eventDedupeTtlSeconds` (default `86400`) so a publish retry is one logical
delivery. Per-instance relay groups are `agent-py:sse:<instance-id>`.

Inspect delayed or failed publication without exposing payloads:

```bash
docker compose -f ../../infra/compose.yaml exec postgres psql -U agent_py -d agent_py -c "SELECT id, aggregate_id, sequence, attempt_count, available_at, claimed_by, claim_expires_at, published_at, last_error FROM outbox_events WHERE published_at IS NULL ORDER BY available_at, created_at, id;"
```

`attempt_count`, `available_at`, `claimed_by`, `claim_expires_at`, and `last_error`
describe retry state. Redis failure is degraded delivery, not data loss: writes
continue, `/ready` reports HTTP 200/degraded, and AIOps SSE falls back to canonical
PostgreSQL polling. After Redis returns, keep the application running so its
dispatcher retries unpublished rows; confirm `Outbox publication acknowledged` logs
and inspect the query above. Do not fabricate, replay, or treat Redis payloads as
API facts.

Development uses Redis `/0`; integration tests use `/15` and a UUID-qualified prefix.
Use only prefix-scoped `SCAN`/`DEL` cleanup, never `FLUSHDB`.

## Cache, rate limit, and degraded-mode evidence

`RedisJsonCache` stores validated DTOs only. MCP discovery keys are owner/connection
version scoped; `call_tool` always reaches the MCP server. RAG keys include owner,
normalized query/settings, sorted authorized KB IDs, and a PostgreSQL document
version. Cached RAG evidence is revalidated against the current owner and KB scope.

`DistributedRateLimiter` uses Redis server time in one Lua token-bucket operation.
Diagnostic creation, Chat streaming, and MCP tool execution use local fallback on
Redis loss; `recovery.execute` is fail-closed. Policy values are in project JSON.
The in-memory fallback is locked, bounded, and stale-entry aware.

Run focused evidence:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_redis_cache.py tests/test_retrieval_cache.py tests/test_rate_limit.py tests/test_agent_rate_limits.py tests/test_redis_degraded_mode.py -q
```

`/metrics` exposes only bounded purpose/action labels. Tests use Redis `/15` and
prefix-only cleanup.

## GitHub Actions backend checks

CI 的 `backend-quality` Job 在 Python 3.13 上按 `uv.lock` 执行：

```powershell
uv sync --frozen
uv run ruff check .
uv run pyright
```

`backend-tests` 使用 PostgreSQL 16 和 Redis 7 service containers。PostgreSQL 同时
提供开发契约数据库 `agent_py` 和隔离集成数据库 `agent_py_test`；运行时 Redis 使用
`/0`，集成测试使用 `/15`。工作流从可提交模板生成临时且被 Git 忽略的
`config/project.json` 和 `config/user.project.json`，只写入固定假密钥
`offline-test-key`，不读取 GitHub secrets。

完整离线测试命令为：

```powershell
uv run pytest
```

pytest 默认排除 `live_llm`。只有需要显式验证本地 DashScope Chat、Embedding 和
Rerank 配置时，才手动运行 `uv run pytest -m live_llm tests/test_live_llm.py -q`；
该命令不属于普通 PR CI。
