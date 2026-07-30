# Transactional Outbox and Redis Streams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish durable PostgreSQL job events to Redis Streams and deliver low-latency AIOps SSE updates without making Redis a source of truth.

**Architecture:** Each job event and its Outbox row are committed in the same PostgreSQL transaction. A background dispatcher claims unpublished Outbox rows with `FOR UPDATE SKIP LOCKED`, publishes an idempotent event to Redis Streams, and records publication. SSE consumes Redis for the fast path and resumes/polls PostgreSQL by sequence whenever Redis is unavailable or a stream gap is detected.

**Tech Stack:** Python, FastAPI SSE, SQLAlchemy async, PostgreSQL 16, Redis 7, redis-py asyncio, Alembic, pytest, Docker Compose.

## Global Constraints

- Complete `2026-07-30-postgresql-runtime.md` first.
- PostgreSQL job events are canonical. Redis stream loss, flush, restart, or timeout must not lose a user-visible event.
- Do not enqueue background jobs in Redis and do not replace `BackgroundJobRuntime`.
- Never mark an Outbox row published before Redis acknowledges `XADD`.
- Stream payloads must not contain credentials, model secrets, or raw connection headers.
- Use bounded stream retention and explicit consumer cancellation.

---

## Task 1: Add Redis runtime configuration and health-checked infrastructure

**Files:**

- Modify: `infra/compose.yaml`
- Modify: `infra/README.md`
- Modify: `config/project.json`
- Modify: `config/project.template.json`
- Modify: `config/project.test.json`
- Modify: `apps/backend/pyproject.toml`
- Create: `apps/backend/src/super_ai/redis_runtime/__init__.py`
- Create: `apps/backend/src/super_ai/redis_runtime/config.py`
- Create: `apps/backend/src/super_ai/redis_runtime/client.py`
- Create: `apps/backend/tests/test_redis_runtime_config.py`

- [ ] **Step 1: Write failing configuration tests**

Test that project configuration loads:

```python
RedisRuntimeSettings(
    url="redis://localhost:6379/0",
    stream_prefix="agent-py",
    stream_maxlen=10_000,
    block_timeout_ms=1_000,
)
```

Also assert the URL parser rejects non-Redis schemes and that `repr(settings)` does not expose passwords.

- [ ] **Step 2: Confirm tests fail**

Run:

```powershell
uv run pytest tests/test_redis_runtime_config.py -q
```

Expected: import failure because `redis_runtime` does not exist.

- [ ] **Step 3: Add dependency and settings**

Add:

```toml
"redis[hiredis]>=5.2.0",
```

Add a `redis` section to all three project configuration files with URL, stream prefix, maximum length, block timeout, cache TTL defaults, and rate-limit defaults. Use database `/0` for development and `/15` for `config/project.test.json`. Keep application configuration in JSON rather than introducing application environment variables.

- [ ] **Step 4: Implement client construction**

`create_redis_client(settings)` must return `redis.asyncio.Redis` with:

```python
Redis.from_url(
    settings.url,
    decode_responses=True,
    health_check_interval=30,
    socket_connect_timeout=1.0,
    socket_timeout=2.0,
    retry_on_timeout=True,
)
```

Add an async `ping_redis` helper returning a structured result; it must not raise through readiness composition.

- [ ] **Step 5: Add Redis 7 to Compose**

Use `redis:7-alpine`, enable append-only persistence, add `redis-cli ping` health check, expose port `6379`, and add a named `redis-data` volume.

- [ ] **Step 6: Verify**

Run:

```powershell
Set-Location infra
docker compose up -d redis
docker compose ps redis
Set-Location ../apps/backend
uv sync
uv run pytest tests/test_redis_runtime_config.py -q
```

Expected: Redis is healthy and all configuration tests pass.

- [ ] **Step 7: Commit**

```powershell
git add -- infra config apps/backend/pyproject.toml apps/backend/uv.lock apps/backend/src/super_ai/redis_runtime apps/backend/tests/test_redis_runtime_config.py
git commit -m "build: add Redis runtime infrastructure"
```

---

## Task 2: Persist job events and Outbox messages atomically

**Files:**

- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/extended_sqlalchemy.py`
- Create: `apps/backend/alembic/versions/202607300002_add_outbox_events.py`
- Create: `apps/backend/tests/test_outbox_repository.py`

- [ ] **Step 1: Write failing repository tests**

Tests must prove:

- `append_event` creates one `background_job_events` row and one `outbox_events` row in one commit;
- an injected exception before commit leaves neither row;
- two dispatchers claim distinct rows;
- a failed publication releases/reschedules the row;
- `mark_published` is idempotent.

- [ ] **Step 2: Define Outbox records and protocol**

Add immutable `OutboxEventRecord` plus:

```python
class OutboxEventRepository(Protocol):
    async def claim_batch(
        self, *, worker_id: str, limit: int, lease_seconds: int
    ) -> Sequence[OutboxEventRecord]:
        raise NotImplementedError

    async def mark_published(
        self, event_id: str, *, published_at: datetime
    ) -> None:
        raise NotImplementedError

    async def release(
        self, event_id: str, *, error: str, available_at: datetime
    ) -> None:
        raise NotImplementedError
```

Expose it through `MemoryRepositories`.

- [ ] **Step 3: Add the schema**

Create `outbox_events` with:

- `id` UUID/string primary key;
- `aggregate_type`, `aggregate_id`, `event_type`;
- `sequence` integer;
- `payload` JSONB;
- `created_at`, `available_at`, `published_at`;
- `claimed_by`, `claim_expires_at`;
- `attempt_count`, `last_error`.

Add a unique constraint on `(aggregate_type, aggregate_id, sequence, event_type)` and an index on `(published_at, available_at, claim_expires_at)`.

- [ ] **Step 4: Write both rows in one transaction**

Refactor `append_event` so event sequence allocation, `BackgroundJobEventModel` insertion, and matching `OutboxEventModel` insertion use one `AsyncSession.begin()` boundary. Use the job-event ID as the Outbox ID or include it as a stable payload field for downstream deduplication.

- [ ] **Step 5: Implement multi-dispatcher claiming**

Claim available unpublished rows with:

```python
select(OutboxEventModel)
.where(
    OutboxEventModel.published_at.is_(None),
    OutboxEventModel.available_at <= now,
)
.order_by(OutboxEventModel.created_at, OutboxEventModel.id)
.with_for_update(skip_locked=True)
.limit(limit)
```

Lease claimed rows so a crashed dispatcher can be recovered.

- [ ] **Step 6: Run tests**

Run:

```powershell
uv run alembic upgrade head
uv run pytest tests/test_outbox_repository.py tests/test_postgresql_background_jobs.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```powershell
git add -- apps/backend/src/super_ai/memory apps/backend/alembic/versions/202607300002_add_outbox_events.py apps/backend/tests/test_outbox_repository.py
git commit -m "feat: persist job events through transactional outbox"
```

---

## Task 3: Dispatch Outbox events to Redis Streams

**Files:**

- Create: `apps/backend/src/super_ai/events/__init__.py`
- Create: `apps/backend/src/super_ai/events/outbox.py`
- Create: `apps/backend/src/super_ai/redis_runtime/streams.py`
- Create: `apps/backend/tests/test_redis_streams.py`
- Create: `apps/backend/tests/test_outbox_dispatcher.py`

- [ ] **Step 1: Write Redis stream adapter tests**

Against the Compose Redis test database, assert:

- publishing uses the single stream key `agent-py:aiops:events`;
- fields include `event_id`, `owner_id_hash`, `job_id`, `sequence`, `event_type`, and JSON `payload`;
- publishing the same event twice does not create duplicate logical delivery;
- stream length remains bounded near `stream_maxlen`.

Use a unique test prefix and delete only that prefix’s keys after each test.

- [ ] **Step 2: Define publisher boundary**

```python
class JobEventPublisher(Protocol):
    async def publish(self, event: OutboxEventRecord) -> None:
        raise NotImplementedError
```

Implement `RedisStreamJobEventPublisher`. Use a small Lua script that checks a per-event dedupe key and performs `XADD` to the unified stream atomically; set the dedupe key TTL longer than maximum SSE resume age.

- [ ] **Step 3: Write dispatcher lifecycle tests**

Use a fake publisher to prove:

- successful publication calls `mark_published`;
- failure calls `release` with capped exponential backoff;
- cancellation releases no already-published row;
- one poison event does not block later events indefinitely.

- [ ] **Step 4: Implement dispatcher**

`OutboxDispatcher` must have `start()`, `stop()`, and `run_once()` methods. Configure batch size, lease duration, minimum/maximum backoff, and poll interval. Log event ID, aggregate ID, attempt, and latency without logging payload contents.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
uv run pytest tests/test_redis_streams.py tests/test_outbox_dispatcher.py -q
```

Expected: all pass with Redis running.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/src/super_ai/events apps/backend/src/super_ai/redis_runtime/streams.py apps/backend/tests/test_redis_streams.py apps/backend/tests/test_outbox_dispatcher.py
git commit -m "feat: dispatch outbox events to Redis Streams"
```

---

## Task 4: Add Redis-fast, PostgreSQL-safe SSE delivery

**Files:**

- Create: `apps/backend/src/super_ai/events/relay.py`
- Create: `apps/backend/src/super_ai/events/subscriber.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Create: `apps/backend/tests/test_aiops_sse_delivery.py`

- [ ] **Step 1: Write end-to-end SSE tests**

Cover:

1. A unified Redis stream event wakes the owning job subscriber without waiting for the PostgreSQL polling interval.
2. When Redis is stopped/unreachable, events still arrive from PostgreSQL.
3. `afterSequence=7` returns only sequence `> 7`.
4. `Last-Event-ID: 7` has the same resume behavior.
5. Redis replay plus PostgreSQL fallback never emits a sequence twice.
6. Events for another owner/job do not wake or leak into the subscription.
7. Client cancellation releases the local subscription.

- [ ] **Step 2: Implement a per-instance Consumer Group relay**

`RedisJobEventRelay` reads the unified stream through `XREADGROUP`. Each backend process creates a group named `agent-py:sse:<instance-id>` and one consumer named from its process identity. Separate groups make every backend instance receive wake-ups; consumers inside an instance share its work.

The relay must:

- route only `(owner_id_hash, job_id, sequence)` wake-up metadata to local subscribers;
- acknowledge after routing the wake-up;
- never treat Redis payload as the canonical API event;
- destroy its own group during graceful shutdown;
- document how to inspect and remove groups left by a crashed instance.

- [ ] **Step 3: Define a subscriber**

Create:

```python
class JobEventSubscriber:
    async def iter_events(
        self,
        *,
        owner_user_id: str,
        job_id: str,
        after_sequence: int,
    ) -> AsyncIterator[BackgroundJobEventRecord]:
        raise NotImplementedError
```

Algorithm:

1. Read PostgreSQL for all rows after the last delivered sequence.
2. Register an owner/job-scoped local wake-up with `RedisJobEventRelay`.
3. Wait for a relay notification with a bounded timeout.
4. Re-read canonical PostgreSQL rows and emit contiguous unseen sequences.
5. On Redis error, log degraded mode and continue PostgreSQL polling.
6. Periodically retry Redis with bounded backoff.

- [ ] **Step 4: Add resumable SSE IDs**

Encode each event with:

```text
id: <sequence>
data: <existing-json-payload>

```

Accept `afterSequence` query input and the standard `Last-Event-ID` header. Use the greater valid value, reject negative/non-integer values with HTTP 422, and preserve existing `data` JSON.

- [ ] **Step 5: Replace endpoint polling**

Replace the manual `sequence = 0`/`sleep(0.1)` loop in the AIOps event endpoint with `JobEventSubscriber.iter_events`. Keep authorization and job ownership checks unchanged.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
uv run pytest tests/test_aiops_sse_delivery.py tests/test_aiops_diagnostics.py -q
```

Expected: all pass both with Redis available and in the explicit Redis-failure case.

- [ ] **Step 7: Commit**

```powershell
git add -- apps/backend/src/super_ai/events/relay.py apps/backend/src/super_ai/events/subscriber.py apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/chat/streaming.py apps/backend/tests/test_aiops_sse_delivery.py apps/backend/tests/test_aiops_diagnostics.py
git commit -m "feat: stream durable job events through Redis-backed SSE"
```

---

## Task 5: Wire dispatcher lifecycle and degraded readiness

**Files:**

- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: backend readiness/lifespan tests

- [ ] **Step 1: Add lifespan tests**

Assert app startup starts exactly one dispatcher per process, shutdown awaits dispatcher cancellation, and a Redis connection failure does not stop `BackgroundJobRuntime`.

- [ ] **Step 2: Add readiness contract tests**

When PostgreSQL and required dependencies are healthy but Redis fails, assert:

- HTTP status remains 200;
- top-level status is `degraded`;
- `dependencies.redis.ok` is `false`;
- diagnostic creation and PostgreSQL event polling still work.

When PostgreSQL fails, preserve HTTP 503.

- [ ] **Step 3: Compose lifecycle**

Create the Redis client, publisher, dispatcher, relay, and subscriber during app composition. Start/stop the dispatcher and relay in FastAPI lifespan. Close the Redis connection pool on shutdown.

- [ ] **Step 4: Implement optional-dependency readiness**

Calculate readiness from blocking dependencies (`postgresql`, existing Milvus/LLM/MCP policy). Redis failure changes status to degraded but does not make the process unready. Include a concise sanitized Redis error.

- [ ] **Step 5: Verify**

Run:

```powershell
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/src/super_ai/api/app.py apps/backend/tests
git commit -m "feat: run Redis event delivery as degradable infrastructure"
```

---

## Task 6: Verify recovery behavior and document operations

**Files:**

- Modify: `README.md`
- Modify: `apps/backend/README.md`
- Modify: `infra/README.md`
- Modify: `openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md`

- [ ] **Step 1: Add an executable recovery test**

The test must:

1. Stop Redis.
2. Append several job events and confirm Outbox rows remain unpublished.
3. Confirm SSE retrieves events from PostgreSQL.
4. Start Redis.
5. Wait for Outbox publication.
6. Confirm Redis receives each sequence once logically.

- [ ] **Step 2: Document runbook**

Document stream key format, retention, Outbox inspection query, retry fields, Redis-loss behavior, dispatcher metrics/logs, and safe recovery steps.

- [ ] **Step 3: Run full verification**

Run:

```powershell
uv run pytest -q
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate migrate-postgresql-add-redis-runtime --strict
```

Expected: tests pass and OpenSpec reports the change is valid.

- [ ] **Step 4: Update OpenSpec task state**

Check only the Outbox, Redis Streams, SSE, lifecycle, and degraded-readiness tasks whose tests passed.

- [ ] **Step 5: Commit**

```powershell
git add -- README.md apps/backend/README.md infra/README.md apps/backend/tests openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md
git commit -m "docs: add Redis stream recovery runbook"
```
