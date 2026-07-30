# Redis Cache and Rate Limiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add user-isolated Redis caching for MCP discovery and RAG retrieval plus distributed token-bucket limits for costly Agent entry points, with safe in-process degradation when Redis is unavailable.

**Architecture:** Cache keys include tenant/user ownership and deterministic versions derived from PostgreSQL. Cached values are accelerators only; misses and Redis failures call canonical services. A Redis Lua token bucket coordinates limits across workers, while a bounded in-memory fallback protects a single process during Redis outages.

**Tech Stack:** Python, FastAPI, Redis 7/redis-py asyncio, Lua, PostgreSQL 16, pytest.

## Global Constraints

- Complete the PostgreSQL and Outbox/Streams plans first.
- Never use cache data as authorization evidence.
- Include owner/user scope in every cache and rate-limit key.
- Cache only sanitized serializable DTOs; never cache secrets, access tokens, cookies, or raw MCP headers.
- Redis failure must not disable diagnosis or chat. It may reduce performance and cross-worker limit precision.
- Preserve existing API response shapes except documented HTTP 429 headers.

---

## Task 1: Build typed, observable cache primitives

**Files:**

- Create: `apps/backend/src/super_ai/redis_runtime/cache.py`
- Create: `apps/backend/tests/test_redis_cache.py`

- [ ] **Step 1: Write failing cache tests**

Test:

- stable SHA-256 key construction independent of dictionary ordering;
- different owners never share a key;
- `get_json` distinguishes miss, hit, invalid payload, and Redis error;
- TTL is applied;
- values larger than the configured byte limit are not cached;
- corrupt entries are deleted and treated as misses.

- [ ] **Step 2: Define cache boundary**

```python
@dataclass(frozen=True)
class CacheLookup[T]:
    state: Literal["hit", "miss", "degraded"]
    value: T | None


class RuntimeCache(Protocol):
    async def get_json(self, key: str) -> CacheLookup[dict[str, object]]:
        raise NotImplementedError

    async def set_json(
        self, key: str, value: Mapping[str, object], ttl_seconds: int
    ) -> bool:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError
```

If the project’s Python version does not support PEP 695 syntax, use `TypeVar`/`Generic` while retaining the same semantics.

- [ ] **Step 3: Implement Redis cache**

Key format:

```text
agent-py:cache:<purpose>:<owner-hash>:<version-hash>:<input-hash>
```

Hash raw identifiers so email/user strings do not appear in Redis keys. Serialize with deterministic JSON. Catch only Redis/serialization failures, log purpose and state without values, and return degraded/miss.

- [ ] **Step 4: Run tests**

Run:

```powershell
uv run pytest tests/test_redis_cache.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add -- apps/backend/src/super_ai/redis_runtime/cache.py apps/backend/tests/test_redis_cache.py
git commit -m "feat: add typed Redis cache primitives"
```

---

## Task 2: Cache MCP tool discovery without caching tool execution

**Files:**

- Create: `apps/backend/src/super_ai/mcp/cached_client.py`
- Modify: MCP client/provider composition files under `apps/backend/src/super_ai/mcp/`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Create: `apps/backend/tests/test_mcp_discovery_cache.py`

- [ ] **Step 1: Write behavior tests**

Using a counting fake MCP client, prove:

- two discovery calls for the same owner/connection/version call upstream once;
- different owners call upstream separately;
- a connection configuration update changes the version and misses cache;
- Redis failure calls upstream;
- `call_tool` always calls upstream and is never cached;
- cached tool definitions are validated before return.

- [ ] **Step 2: Implement a decorator**

`CachedMcpClient` implements the same client protocol:

```python
class CachedMcpClient:
    async def discover_tools(self) -> Sequence[McpToolDefinition]:
        raise NotImplementedError

    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> object:
        raise NotImplementedError
```

Constructor inputs include inner client, cache, owner ID, connection ID, and a connection version derived from PostgreSQL `updated_at` plus a hash of non-secret behavioral configuration. TTL defaults to 300 seconds.

- [ ] **Step 3: Wire per-owner composition**

Wrap clients only after authorization has selected an owner-visible MCP connection. Do not put credentials into version strings or cache values.

- [ ] **Step 4: Verify**

Run:

```powershell
uv run pytest tests/test_mcp_discovery_cache.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add -- apps/backend/src/super_ai/mcp apps/backend/src/super_ai/api/app.py apps/backend/tests/test_mcp_discovery_cache.py
git commit -m "feat: cache owner-scoped MCP tool discovery"
```

---

## Task 3: Cache RAG retrieval using PostgreSQL-derived versions

**Files:**

- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Create: `apps/backend/src/super_ai/retrieval/cached_tool.py`
- Modify: retrieval composition in `apps/backend/src/super_ai/api/app.py`
- Create: `apps/backend/tests/test_retrieval_cache.py`

- [ ] **Step 1: Write repository version tests**

Add:

```python
async def get_knowledge_base_cache_version(
    self,
    *,
    owner_user_id: str,
    knowledge_base_ids: Sequence[str],
) -> str:
    raise NotImplementedError
```

Tests prove the version:

- is stable when documents do not change;
- changes after insert, successful re-index/update, or deletion;
- differs by owner;
- is independent of input KB ordering.

- [ ] **Step 2: Implement canonical version calculation**

Query PostgreSQL for each requested KB’s document count and maximum `updated_at`, scoped by owner. Canonically sort rows and SHA-256 hash their JSON representation. Authorization filtering happens before version calculation.

- [ ] **Step 3: Write cached retrieval tests**

Using a counting real/fake inner `KnowledgeRetrievalTool`, prove:

- repeated identical query/options/owner/KB-version hits cache;
- query, retrieval settings, accessible KB set, or version change misses;
- different owners never share results;
- Redis failure invokes the inner tool;
- empty and error results use short TTLs or are not cached according to explicit constants.

- [ ] **Step 4: Implement `CachedKnowledgeRetrievalTool`**

Cache only the validated `KnowledgeRetrievalToolResult` DTO. Suggested TTLs:

- successful non-empty result: 120 seconds;
- empty result: 15 seconds;
- errors: not cached.

The key input must include normalized query, sorted accessible KB IDs, top-k/rerank settings, owner hash, and PostgreSQL version.

- [ ] **Step 5: Wire retrieval**

Wrap the existing retrieval tool at application composition. If cache lookup/version calculation fails, record degraded cache metrics and run normal embedding → Milvus → rerank flow.

- [ ] **Step 6: Verify**

Run:

```powershell
uv run pytest tests/test_retrieval_cache.py tests/test_knowledge_retrieval_api.py tests/test_stream_rag_chat_api.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```powershell
git add -- apps/backend/src/super_ai/memory apps/backend/src/super_ai/retrieval apps/backend/src/super_ai/api/app.py apps/backend/tests/test_retrieval_cache.py apps/backend/tests/test_knowledge_retrieval_api.py apps/backend/tests/test_stream_rag_chat_api.py
git commit -m "feat: cache versioned RAG retrieval results"
```

---

## Task 4: Implement distributed token-bucket limiting with local fallback

**Files:**

- Create: `apps/backend/src/super_ai/redis_runtime/rate_limit.py`
- Create: `apps/backend/tests/test_rate_limit.py`

- [ ] **Step 1: Write contract and algorithm tests**

Test:

- initial burst capacity;
- refill over a controlled clock;
- rejection includes remaining tokens and retry-after;
- separate owner and action buckets;
- Redis keys receive expiry;
- concurrent calls never overspend capacity;
- Redis failure uses bounded in-memory buckets;
- recovery-write policy rejects when Redis is unavailable instead of using local fallback;
- in-memory state is capped and stale buckets are evicted.

- [ ] **Step 2: Define the interface**

```python
@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    mode: Literal["redis", "local-fallback"]


class RateLimiter(Protocol):
    async def acquire(
        self,
        *,
        owner_id: str,
        action: str,
        cost: int = 1,
        failure_policy: Literal["local_fallback", "fail_closed"],
    ) -> RateLimitDecision:
        raise NotImplementedError
```

- [ ] **Step 3: Implement Redis Lua token bucket**

Use Redis server time inside one Lua script. Store current tokens and last refill timestamp in a hash, apply refill, deduct atomically, and set expiry to at least twice the full-refill period. Return integer allowed/remaining/retry-after values.

Key format:

```text
agent-py:limit:<action>:<owner-hash>
```

- [ ] **Step 4: Implement bounded local fallback**

Use the same token-bucket semantics behind an `asyncio.Lock`, monotonic time, maximum bucket count, and periodic stale-entry eviction. The fallback is per process and must emit a degraded-mode counter/log. If `failure_policy == "fail_closed"`, return a rejected decision when Redis is unavailable without consulting the local bucket.

- [ ] **Step 5: Verify**

Run:

```powershell
uv run pytest tests/test_rate_limit.py -q
```

Expected: all pass, including concurrent acquisition.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/src/super_ai/redis_runtime/rate_limit.py apps/backend/tests/test_rate_limit.py
git commit -m "feat: add distributed Agent rate limiting"
```

---

## Task 5: Apply limits to costly Agent entry points

**Files:**

- Create: `apps/backend/src/super_ai/api/rate_limits.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `packages/api-contracts/src/index.ts` if error contracts are typed
- Create: `apps/backend/tests/test_agent_rate_limits.py`

- [ ] **Step 1: Write API tests**

Cover:

- diagnostic creation limit;
- streaming chat limit;
- MCP tool-call limit at the execution boundary;
- same owner exhausting one action does not block another;
- different owners do not share quota;
- 429 contains `Retry-After`, `X-RateLimit-Remaining`, and stable JSON error code;
- local fallback still enforces a limit when Redis is unavailable;
- authorization runs before a user can consume another owner’s bucket.

- [ ] **Step 2: Define explicit policies**

Start with configuration-backed defaults:

```text
diagnostic.create: capacity 5, refill 1 per 30 seconds
chat.stream: capacity 10, refill 1 per 6 seconds
mcp.tool_call: capacity 30, refill 1 per 2 seconds
```

Keep policies in project JSON so they are visible and reviewable.

Classify current diagnosis, chat, and MCP tool-call entry points as `local_fallback`. Reserve `fail_closed` for recovery/side-effecting remediation actions; add a direct service test for `recovery.execute` even if the project does not yet expose a recovery endpoint.

- [ ] **Step 3: Add reusable FastAPI enforcement**

Implement a dependency/helper that receives authenticated owner ID and action, calls `RateLimiter.acquire`, and raises HTTP 429 with headers and:

```json
{
  "code": "rate_limit_exceeded",
  "action": "diagnostic.create",
  "retryAfterSeconds": 12
}
```

For streaming endpoints, acquire before constructing the streaming response.

- [ ] **Step 4: Enforce MCP execution limits**

Apply the limiter immediately before the actual MCP `call_tool`; do not limit cached discovery. Preserve tool audit records for allowed calls, and record a sanitized rejected-call audit if the existing audit model supports rejection status.

- [ ] **Step 5: Verify**

Run:

```powershell
uv run pytest tests/test_agent_rate_limits.py tests/test_aiops_diagnostics.py tests/test_stream_rag_chat_api.py tests/test_tool_call_audits.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/src/super_ai/api apps/backend/tests packages/api-contracts/src/index.ts config
git commit -m "feat: protect costly Agent operations with rate limits"
```

---

## Task 6: Add observability, fault tests, and final documentation

**Files:**

- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: existing metrics/logging modules
- Create: `apps/backend/tests/test_redis_degraded_mode.py`
- Modify: `README.md`
- Modify: `apps/backend/README.md`
- Modify: `openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md`

- [ ] **Step 1: Add metrics and structured logs**

Expose counters/histograms for:

- cache hit/miss/degraded by purpose;
- cache lookup latency;
- rate-limit allow/reject/fallback by action;
- Redis readiness;
- stream publication/retry lag from the previous plan.

Labels must be bounded enums; never label by user, query, job ID, or MCP tool arguments.

- [ ] **Step 2: Write a degraded-mode acceptance test**

With Redis unavailable, prove in one test flow that:

- readiness is 200/degraded;
- diagnostics can be created;
- job events are stored in PostgreSQL;
- SSE falls back to PostgreSQL;
- RAG and MCP discovery bypass cache;
- local rate limiting remains active.

- [ ] **Step 3: Document design and interview evidence**

Document:

- why PostgreSQL remains canonical;
- cache key/version/tenant isolation;
- token-bucket algorithm and fallback trade-off;
- Redis failure matrix;
- commands to inspect stream, cache TTL, rate-limit hash, and Outbox backlog;
- measurable demo scenarios suitable for the project README.

- [ ] **Step 4: Run final verification**

Run:

```powershell
uv run pytest -q
uv run ruff check src tests
uv run mypy src
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate migrate-postgresql-add-redis-runtime --strict
```

Expected: all checks pass and OpenSpec reports the change is valid.

- [ ] **Step 5: Update OpenSpec task state**

Check the cache, rate-limit, fault-tolerance, observability, and documentation boxes only after their tests pass.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend README.md openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md
git commit -m "docs: complete Redis cache and rate-limit runtime"
```
