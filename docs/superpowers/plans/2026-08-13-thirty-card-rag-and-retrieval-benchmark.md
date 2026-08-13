# AgentPy 30 张知识卡与 60 查询 Retrieval Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将测试知识库从 7 张扩展为 30 张经来源审核的差分排障知识卡，将检索试题从 6 条扩展为 60 条，并用文档级指标和严格 citation 审计完成离线与真实 Retrieval Eval。

**Architecture:** RAG 知识卡、Retrieval labels 与 Diagnosis oracle 继续物理隔离。复用现有 heading-aware chunker、filename-idempotent importer、tenant-scoped hybrid retrieval 和安全 runner；只扩展项目自有 immutable DTO/纯评分器，不引入新框架。知识卡按四批审查和提交，全部离线合同通过并完成 chunk 预览后，才执行一次 30 卡真实导入和一次 60 查询真实检索。

**Tech Stack:** Python 3.10+、pytest、PyYAML、FastAPI、PostgreSQL/SQLAlchemy、Milvus、LangChain text splitters、DashScope Embedding/Rerank、OpenSpec CLI、Markdown。

## Global Constraints

- 本计划不启动 Docker Compose 故障注入、不运行 Live Agent Eval、不调用 Chat 模型。
- 知识卡总数必须恰好 30：保留 7 张现有卡，新增设计中固定的 23 张。
- Retrieval labels 必须恰好 60：54 条有答案查询、6 条无答案校准探针。
- `queries.yaml`、相关性标签、场景、Snapshot、ground truth、provenance 和评分规则不得导入 PostgreSQL/Milvus。
- 每张卡必须是 `agentpy-original-summary`，以官方文档和明确许可证的开源资料为主；公开博客只作事实参考，不复制原文。
- 每张卡必须标记 `docker_validation: pending`；本阶段不得宣称已在本项目复现。
- 不增加依赖，不更换向量库、chunker 或评测框架。
- 普通 CI 必须离线；真实 Embedding/Rerank 和批量导入只能显式手动运行。
- 真实报告不得包含正文、excerpt、凭据、原始配置、owner/KB 的秘密映射或 Diagnosis 答案。
- 任一 filename 在目标 owner/KB 中出现多个 active 文档、chunk 预览超界、来源审核不完整或模型服务失败时，停止真实导入/评测，不自动清理或伪造结果。

## Reuse Assessment Gate

项目已经提供 `chunk_document_text(..., strategy="markdown-heading")`、安全批量导入、同名覆盖、PostgreSQL/Milvus scope、BM25 + vector + RRF + rerank、Retrieval DTO/loader/scorer/runner 和离线测试。

2026-08-13 GitHub discovery：

- 查询：`retrieval evaluation recall mrr rag language:Python`。`however-yir/ragproof`、`mozdowski/retrieval-eval-kit`、`muhammadrashid4587/ragbench` 为 MIT，可参考 frozen golden set 与 Recall/MRR 定义；其他候选缺少清晰许可证。
- 查询：`sre runbooks postgres redis kubernetes nginx` 没有返回可直接采用且同时覆盖本项目范围的候选，不将搜索失败/空结果描述成许可证已审核的内容库。
- 一手来源仓库：`postgres/postgres`（PostgreSQL License/Other）、`redis/docs`（Redis 文档许可，按页面核验）、`kubernetes/website`（CC-BY-4.0）、`nginx/nginx`（BSD-2-Clause）。实施时还需逐卡记录具体官方页面和许可。

决定：**仓内直接复用 + 外部参考定义**。不新增 Ragas、BEIR、EvalScope 或其他依赖；知识内容由项目原创重写，不复制第三方 runbook。

---

### Task 1: 规范 30 卡与 60 查询能力

**Files:**
- Create: `openspec/changes/expand-rag-retrieval-benchmark/.openspec.yaml`
- Create: `openspec/changes/expand-rag-retrieval-benchmark/proposal.md`
- Create: `openspec/changes/expand-rag-retrieval-benchmark/design.md`
- Create: `openspec/changes/expand-rag-retrieval-benchmark/tasks.md`
- Create: `openspec/changes/expand-rag-retrieval-benchmark/specs/knowledge-card-catalog/spec.md`
- Create: `openspec/changes/expand-rag-retrieval-benchmark/specs/knowledge-retrieval-eval/spec.md`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-08-13-thirty-card-rag-and-retrieval-benchmark-design.md`.
- Produces: normative catalog, query distribution, no-answer probe, document-ranking and citation requirements.

- [ ] **Step 1: 创建 OpenSpec metadata、proposal 与 design delta**

Use schema `spec-driven`. State exact requirements:

```text
1. Knowledge candidate catalog contains exactly 30 reviewed Markdown cards.
2. Every card contains all eight required headings and pending Docker validation metadata.
3. Retrieval labels contain exactly 60 queries with category counts 12/14/12/8/8/6.
4. Exactly 54 answerable queries cover all 30 cards; six no-answer probes are excluded from Recall/MRR gates.
5. Document ranking de-duplicates sources by first appearance before Recall/MRR.
6. Every returned hit contributes one citation completeness denominator entry, including missing citations.
7. Benchmark labels and Diagnosis answers never enter RAG.
8. Real import stops on legacy duplicates or invalid chunk previews.
```

- [ ] **Step 2: 创建 capability specs 与任务清单**

In `knowledge-card-catalog/spec.md`, add scenarios for exact catalog, source/provenance fields, required headings, forbidden tokens and `docker_validation: pending`. In `knowledge-retrieval-eval/spec.md`, add category distribution, coverage, no-answer exclusion, deduplication and missing-citation scenarios.

- [ ] **Step 3: 验证 OpenSpec 红/绿状态**

Run:

```powershell
npx --yes @fission-ai/openspec@1.6.0 validate expand-rag-retrieval-benchmark
```

Expected: change validates with zero failures.

- [ ] **Step 4: 提交规范**

```powershell
git add openspec/changes/expand-rag-retrieval-benchmark
git commit -m "spec: define expanded retrieval benchmark"
```

---

### Task 2: 先固定知识卡目录、安全和 Chunk 合同

**Files:**
- Modify: `apps/backend/tests/test_knowledge_candidate_safety.py`
- Create: `apps/backend/tests/test_knowledge_catalog.py`
- Create: `apps/backend/scripts/audit_knowledge_catalog.py`

**Interfaces:**
- Produces: `EXPECTED_CARD_FILENAMES: frozenset[str]` test constant.
- Produces: `audit_catalog(root: Path) -> dict[str, object]` safe report with filename, chunk count and heading paths only.
- Consumes: `chunk_document_text(text, strategy="markdown-heading")`.

- [ ] **Step 1: 写 30 卡目录与结构失败测试**

Add exact filename set from the approved design and assert:

```python
files = {path.name for path in KNOWLEDGE.glob("*.md")}
assert files == EXPECTED_CARD_FILENAMES
assert len(files) == 30
```

For every card require:

```python
REQUIRED_HEADINGS = (
    "## 适用现象", "## 候选原因", "## 建议证据", "## 如何区分",
    "## 安全恢复边界", "## 恢复后验证", "## 来源", "## 验证状态",
)
assert all(heading in text for heading in REQUIRED_HEADINGS)
assert "content_type: agentpy-original-summary" in text
assert "docker_validation: pending" in text
assert text.count("https://") >= 2
```

Expand forbidden tokens with `RET-`, `relevant_documents`, `forbidden_top_one`, `expected_no_answer`, `ownerUserId`, `knowledgeBaseId`, `apiKey`, `password`, and `token` using boundary-aware/case-insensitive checks that do not reject ordinary prose such as “token bucket” unless it resembles a secret field.

- [ ] **Step 2: 运行并确认缺 23 卡/验证章节的正确红灯**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py tests/test_knowledge_catalog.py -q
```

Expected: FAIL because only seven files exist and existing cards lack the validation section.

- [ ] **Step 3: 写 catalog audit 脚本失败测试**

Test a temporary two-card directory and expect a content-free payload:

```python
report = audit_catalog(root)
assert report["totalDocuments"] == 2
assert report["documents"][0].keys() == {"filename", "chunkCount", "headingPaths"}
assert "secret body" not in json.dumps(report)
```

Also assert `ValueError` for zero chunks or more than six chunks.

- [ ] **Step 4: 实现安全 Chunk audit**

The script loads Markdown, calls:

```python
chunks = chunk_document_text(text, strategy="markdown-heading")
```

It emits filename, `len(chunks)` and non-empty unique `heading_path`; it never serializes `chunk.content`. CLI args:

```text
--source-dir required
--output optional
```

Exit 0 only when every file has 1..6 chunks.

- [ ] **Step 5: 验证 audit 单测和静态检查**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_catalog.py -q
.venv\Scripts\python.exe -m ruff check scripts/audit_knowledge_catalog.py tests/test_knowledge_catalog.py tests/test_knowledge_candidate_safety.py
.venv\Scripts\python.exe -m pyright scripts/audit_knowledge_catalog.py tests/test_knowledge_catalog.py
```

Expected: audit tests/static checks pass; exact catalog test remains red until Tasks 3–6.

- [ ] **Step 6: 不提交仍红的 catalog 合同**

Keep these changes uncommitted until Task 6 supplies all 30 cards so no commit leaves required tests failing.

---

### Task 3: 编写 PostgreSQL 与 Redis 新卡（9 张）

**Files:**
- Create: `docs/knowledge-candidates/postgres-slow-query-lock-wait.md`
- Create: `docs/knowledge-candidates/postgres-deadlock.md`
- Create: `docs/knowledge-candidates/postgres-connectivity-auth.md`
- Create: `docs/knowledge-candidates/postgres-replication-lag.md`
- Create: `docs/knowledge-candidates/postgres-disk-wal-pressure.md`
- Create: `docs/knowledge-candidates/redis-memory-eviction.md`
- Create: `docs/knowledge-candidates/redis-slow-command-hot-key.md`
- Create: `docs/knowledge-candidates/redis-failover-reconnect.md`
- Create: `docs/knowledge-candidates/redis-maxclients-pressure.md`

**Interfaces:**
- Consumes: Task 2 card contract.
- Produces: nine reviewed differential cards used by Task 7 labels.

- [ ] **Step 1: 收集每张卡至少两个具体来源**

Use official primary pages first. Required source families:

```text
PostgreSQL: monitoring-stats, explicit-locking, deadlock handling,
client authentication/pg_hba, warm standby/replication slots, WAL/checkpoints.
Redis: eviction, latency/slowlog, hot/big key guidance, replication/failover,
client reconnect guidance, maxclients/connection errors.
```

Record exact URL, source type, license and access date in each card. If a source license is unclear, label `unknown-reference-only`; do not copy text.

- [ ] **Step 2: 按统一八章节写卡**

Each card must contain at least three candidates and differentiation based on two independent dimensions, e.g. database/server evidence plus application/client evidence. Put these exact metadata lines under `## 验证状态`:

```text
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
```

- [ ] **Step 3: 运行本批内容安全检查**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py -q
```

Expected: failures only for cards not yet created in later batches; no failure points to the nine files in this task.

- [ ] **Step 4: 手工核对差分重叠**

For each pair below, confirm the cards do not collapse into identical advice:

```text
pool exhaustion vs slow query/lock wait
slow query/lock wait vs deadlock
connectivity/auth vs pool exhaustion
memory eviction vs unavailable
slow command/hot key vs memory eviction
failover/reconnect vs unavailable
maxclients vs client connection leak
```

Do not commit this batch until Task 6 turns the global catalog contract green; stage review is by `git diff -- docs/knowledge-candidates`.

---

### Task 4: 编写 Nginx/HTTP 与微服务新卡（6 张）

**Files:**
- Create: `docs/knowledge-candidates/nginx-upstream-timeout.md`
- Create: `docs/knowledge-candidates/nginx-routing-service-discovery.md`
- Create: `docs/knowledge-candidates/http-rate-limit-retry-storm.md`
- Create: `docs/knowledge-candidates/service-thread-pool-saturation.md`
- Create: `docs/knowledge-candidates/service-circuit-breaker-degradation.md`
- Create: `docs/knowledge-candidates/service-startup-config-failure.md`

**Interfaces:** Task 2 contract; query targets for Task 7.

- [ ] **Step 1: 审核 Nginx 与协议来源**

Use Nginx upstream/proxy timeout docs, HTTP semantics for 429/Retry-After, and maintained resilience-library documentation for circuit breaker/retry behavior. Prefer BSD-2-Clause Nginx sources and official protocol documentation; record exact URLs/licenses.

- [ ] **Step 2: 编写六张差分卡**

Explicitly distinguish:

```text
502 connection failure vs 504/upstream timeout
route/upstream endpoint mismatch vs service process failure
legitimate rate limit vs retry amplification
local executor saturation vs downstream wait
circuit open vs dependency still failing vs half-open recovery failure
startup config error vs dependency-not-ready vs probe misconfiguration
```

Include the exact validation metadata from Task 3.

- [ ] **Step 3: 运行内容安全检查并审查 diff**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py -q
git diff --check
```

Expected: this batch has no local safety failure; later missing cards may keep global catalog red.

---

### Task 5: 编写 Kubernetes、队列与主机/TLS 新卡（8 张）

**Files:**
- Create: `docs/knowledge-candidates/kubernetes-pod-crashloop.md`
- Create: `docs/knowledge-candidates/kubernetes-service-endpoint-mismatch.md`
- Create: `docs/knowledge-candidates/queue-consumer-stalled.md`
- Create: `docs/knowledge-candidates/queue-poison-message-dlq.md`
- Create: `docs/knowledge-candidates/host-disk-capacity-pressure.md`
- Create: `docs/knowledge-candidates/host-cpu-load-pressure.md`
- Create: `docs/knowledge-candidates/host-file-descriptor-exhaustion.md`
- Create: `docs/knowledge-candidates/tls-certificate-handshake-failure.md`

**Interfaces:** Task 2 contract; completes all 23 new cards.

- [ ] **Step 1: 审核官方来源**

Use Kubernetes Pod lifecycle/debug Service docs (CC-BY-4.0), queue vendor-neutral or current project job semantics plus RabbitMQ/Kafka official docs as reference, Linux man-pages/kernel documentation for disk/CPU/FD, and RFC/OpenSSL documentation for TLS. Record exact URL/license/access date.

- [ ] **Step 2: 编写八张卡并保持差分边界**

Required distinctions:

```text
CrashLoop: app exit vs probe vs config/dependency
Service endpoint: selector vs readiness vs targetPort
consumer stalled: process/lease vs downstream wait vs capacity
poison message: deterministic payload vs transient dependency vs idempotency failure
disk: bytes vs inode vs read-only/IO
CPU: saturation vs quota throttling vs load from blocked tasks
FD: process limit vs host limit vs connection leak
TLS: expiry vs trust chain vs SNI/protocol/clock
```

Include validation metadata.

- [ ] **Step 3: 运行内容安全检查**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py -q
```

Expected: remaining failures, if any, are only existing-card validation metadata fixed in Task 6.

---

### Task 6: 统一现有七卡、转绿 Catalog 合同并提交

**Files:**
- Modify: `docs/knowledge-candidates/kubernetes-dns-debugging.md`
- Modify: `docs/knowledge-candidates/kubernetes-memory-saturation.md`
- Modify: `docs/knowledge-candidates/microservice-timeout.md`
- Modify: `docs/knowledge-candidates/nginx-upstream-502.md`
- Modify: `docs/knowledge-candidates/postgres-pool-exhaustion.md`
- Modify: `docs/knowledge-candidates/queue-backlog.md`
- Modify: `docs/knowledge-candidates/redis-unavailable.md`
- Modify/Create from Task 2: `apps/backend/tests/test_knowledge_candidate_safety.py`
- Create from Task 2: `apps/backend/tests/test_knowledge_catalog.py`
- Create from Task 2: `apps/backend/scripts/audit_knowledge_catalog.py`

**Interfaces:** completes exact 30-card catalog and safe audit CLI.

- [ ] **Step 1: 补齐现有卡所缺章节和验证状态**

Do not rewrite valid diagnostic content. Add `## 验证状态` and exact metadata. If any existing card lacks one of the other seven headings or two sources, minimally add the missing section/source.

- [ ] **Step 2: 运行完整 Catalog 测试**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py tests/test_knowledge_catalog.py tests/test_document_indexing.py -q
```

Expected: PASS and exactly 30 Markdown cards.

- [ ] **Step 3: 运行安全 Chunk audit**

```powershell
.venv\Scripts\python.exe scripts/audit_knowledge_catalog.py --source-dir ../../docs/knowledge-candidates --output var/knowledge-import/catalog-audit.json
```

Expected: exit 0; `totalDocuments=30`; every chunk count in 1..6; output contains no card body.

- [ ] **Step 4: 静态检查**

```powershell
.venv\Scripts\python.exe -m ruff check scripts/audit_knowledge_catalog.py tests/test_knowledge_catalog.py tests/test_knowledge_candidate_safety.py
.venv\Scripts\python.exe -m pyright scripts/audit_knowledge_catalog.py tests/test_knowledge_catalog.py tests/test_knowledge_candidate_safety.py
```

Expected: zero errors.

- [ ] **Step 5: 提交 30 卡与审计合同**

```powershell
git add docs/knowledge-candidates apps/backend/scripts/audit_knowledge_catalog.py apps/backend/tests/test_knowledge_catalog.py apps/backend/tests/test_knowledge_candidate_safety.py
git commit -m "docs: expand reviewed troubleshooting catalog"
```

---

### Task 7: 扩展查询 DTO、无答案合同和文档级评分

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/retrieval.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Modify: `apps/backend/tests/test_retrieval_evaluation.py`

**Interfaces:**
- Changes `RetrievalQuery` fields to:

```python
id: str
query_type: Literal["explicit_component", "ambiguous_symptom", "log_signal", "operator_perturbation", "cross_component_distractor", "no_answer_probe"]
query: str
relevant_documents: tuple[str, ...]
acceptable_top_k: int
forbidden_top_one: tuple[str, ...]
source_type: Literal["project-synthesized", "public-symptom-rewrite"]
review_status: Literal["reviewed"]
expected_no_answer: bool
```

- Changes `RetrievalQueryResult` to include `expected_no_answer: bool`.
- Changes `RetrievalEvaluationResult` to include `answerable_query_count: int` and `no_answer_probe_count: int`.
- Produces `deduplicate_ranked_documents(documents: Sequence[str]) -> tuple[str, ...]`.

- [ ] **Step 1: 写 loader 元数据和分布失败测试**

Test required `type`, `source_type`, `review_status`, `expected_no_answer`; reject unknown values and inconsistent labels:

```python
if expected_no_answer:
    assert relevant_documents == ()
else:
    assert relevant_documents
```

Keep duplicate ID, Top-K and answer token checks.

- [ ] **Step 2: 写文档去重和无答案分母失败测试**

Representative result:

```python
ranked = ("target.md", "target.md", "wrong.md")
assert deduplicate_ranked_documents(ranked) == ("target.md", "wrong.md")
```

Use two answerable queries plus one no-answer probe; assert Recall/MRR denominators are 2, `query_count == 3`, `answerable_query_count == 2`, `no_answer_probe_count == 1`.

- [ ] **Step 3: 运行并观察模型/字段缺失红灯**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py -q
```

Expected: FAIL because DTOs/helper/loader do not yet support the fields.

- [ ] **Step 4: 实现 strict loader 和去重评分**

Use `Literal` aliases, immutable dataclasses and strict `_text` validation. `evaluate_retrieval` must reject an input set with zero answerable queries, but must allow probes alongside answerable results. For answerable results, deduplicate before Recall@K/MRR/forbidden Top-1. No-answer probes do not affect those ranking metrics.

- [ ] **Step 5: 转绿并静态检查**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py -q
.venv\Scripts\python.exe -m ruff check src/super_ai/evaluation/retrieval.py tests/test_retrieval_evaluation.py
.venv\Scripts\python.exe -m pyright src/super_ai/evaluation/retrieval.py tests/test_retrieval_evaluation.py
```

Expected: all exit 0.

- [ ] **Step 6: 提交评分合同**

```powershell
git add apps/backend/src/super_ai/evaluation apps/backend/tests/test_retrieval_evaluation.py
git commit -m "feat: score document-level retrieval queries"
```

---

### Task 8: 让 runner 对每个 hit 审计 citation 并报告 probes

**Files:**
- Modify: `apps/backend/scripts/run_retrieval_benchmark.py`
- Modify: `apps/backend/tests/test_retrieval_benchmark_cli.py`

**Interfaces:**
- Consumes Task 7 DTOs.
- Report metrics add `answerableQueryCount` and `noAnswerProbeCount`.
- Each run adds `queryType`, `expectedNoAnswer`, and for probes `topOneRerankScore`/`topTwoMargin` (nullable).

- [ ] **Step 1: 写 missing citation 红灯测试**

Fake tool returns two hits and a citation only for the first. Assert:

```python
assert payload["metrics"]["citationCompletenessRate"] == 0.5
```

The report still includes both safe hit records; content/excerpt stays absent.

- [ ] **Step 2: 写 no-answer probe 报告测试**

Use a temporary labels YAML with one answerable query and one probe. Assert probe fields, query counts and that `_passes` depends only on answerable ranking gates, forbidden rate and citation completeness—not on an uncalibrated probe threshold.

- [ ] **Step 3: 运行正确红灯**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_benchmark_cli.py -q
```

Expected: missing citation incorrectly scores complete or new report fields are absent.

- [ ] **Step 4: 实现每 hit 一条 citation audit**

For each hit, look up citation by chunk ID. If absent, create:

```python
RetrievalCitationAudit(
    chunk_id=hit.chunk_id,
    document_id="",
    knowledge_base_id="",
    vector_score=None,
    rerank_score=None,
)
```

If present, preserve citation fields. Keep scope validation before scoring.

- [ ] **Step 5: 实现文档级排名和 probe diagnostics**

Pass raw basename sequence to Task 7 scorer (which deduplicates). Compute probe diagnostic scores from hits only; do not serialize query text, content or excerpts.

- [ ] **Step 6: 验证 runner**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_benchmark_cli.py tests/test_retrieval_evaluation.py -q
.venv\Scripts\python.exe -m ruff check scripts/run_retrieval_benchmark.py tests/test_retrieval_benchmark_cli.py
.venv\Scripts\python.exe -m pyright scripts/run_retrieval_benchmark.py tests/test_retrieval_benchmark_cli.py
```

Expected: all exit 0.

- [ ] **Step 7: 提交 runner 修复**

```powershell
git add apps/backend/scripts/run_retrieval_benchmark.py apps/backend/tests/test_retrieval_benchmark_cli.py
git commit -m "fix: audit every retrieval hit citation"
```

---

### Task 9: 编写并验证 60 条 Retrieval labels

**Files:**
- Modify: `benchmarks/agentpy/retrieval/queries.yaml`
- Modify: `apps/backend/tests/test_retrieval_evaluation.py`

**Interfaces:** exactly 60 labels for Task 8 runner.

- [ ] **Step 1: 写分布、覆盖和污染失败测试**

Assert exact counts:

```python
Counter(query.query_type for query in queries) == {
    "explicit_component": 12,
    "ambiguous_symptom": 14,
    "log_signal": 12,
    "operator_perturbation": 8,
    "cross_component_distractor": 8,
    "no_answer_probe": 6,
}
```

Assert 54 answerable, six probes, all 30 catalog filenames appear in answerable relevance, and exactly 24 filenames appear at least twice. Reject title-stem copying by normalizing filename tokens and requiring non-explicit queries not to contain the full target stem.

- [ ] **Step 2: 运行并确认 6/60 红灯**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py -q
```

Expected: FAIL on total/distribution/coverage.

- [ ] **Step 3: 编写 54 条有答案查询**

Keep the six existing query intents but add required metadata and, where needed, rename IDs consistently. Create 48 additional answerable labels so all 30 cards are covered and 24 have two expressions. Assign strong `forbidden_top_one` from the closest alternative card, not a random different domain.

- [ ] **Step 4: 编写 6 条无答案探针**

Use out-of-scope operational questions such as object-storage quorum, GPU ECC, BGP route leak, time-series cardinality, certificate issuance policy and kernel filesystem corruption. Set:

```yaml
relevant_documents: []
expected_no_answer: true
type: no_answer_probe
```

Do not label a merely difficult in-scope query as no-answer.

- [ ] **Step 5: 运行 loader、分布和安全测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py tests/test_knowledge_candidate_safety.py -q
.venv\Scripts\python.exe -m ruff check tests/test_retrieval_evaluation.py
.venv\Scripts\python.exe -m pyright tests/test_retrieval_evaluation.py
```

Expected: all exit 0; knowledge cards contain none of the label-only fields.

- [ ] **Step 6: 提交 60 查询集**

```powershell
git add benchmarks/agentpy/retrieval/queries.yaml apps/backend/tests/test_retrieval_evaluation.py
git commit -m "test: expand retrieval benchmark to sixty queries"
```

---

### Task 10: 30 卡真实导入与存储核验

**Files:**
- Runtime only: `apps/backend/var/knowledge-import/thirty-card/`
- Runtime only: `apps/backend/var/knowledge-import/evidence/`
- No tracked source changes unless a defect is reproduced by a new failing test.

**Interfaces:** consumes current ignored `aiopsDemo` credentials, backend, PostgreSQL, Milvus and Embedding provider.

- [ ] **Step 1: 创建 ignored staging 并复制恰好 30 卡**

Copy tracked cards into `apps/backend/var/knowledge-import/thirty-card/`. Compare SHA-256 per filename against source and assert count 30; do not edit staging copies.

- [ ] **Step 2: dry-run 和 Chunk audit**

```powershell
cd apps/backend
.venv\Scripts\python.exe scripts/import_knowledge_batch.py --source-dir var/knowledge-import/thirty-card --dry-run
.venv\Scripts\python.exe scripts/audit_knowledge_catalog.py --source-dir var/knowledge-import/thirty-card --output var/knowledge-import/evidence/catalog-audit.json
```

Expected: exactly 30 filenames; every chunk count 1..6.

- [ ] **Step 3: 认证并记录 owner/KB 的导入前状态**

Reuse `_register_or_login` without printing password/token. Save only owner ID, KB ID, filename, active count and active document ID in ignored evidence. Stop if any of the 30 filenames has active count greater than one.

- [ ] **Step 4: 启动当前 worktree backend 并执行一次真实 import**

```powershell
.venv\Scripts\python.exe scripts/import_knowledge_batch.py --source-dir var/knowledge-import/thirty-card --continue-on-error
```

Expected: `succeeded=30`, `failed=0`. This consumes Embedding quota only. If any file fails, stop before Retrieval Eval and preserve the actual summary.

- [ ] **Step 5: 核验 PostgreSQL**

For every filename assert exactly one active document, `index_status=indexed`, and its latest task is `succeeded`. For replaced IDs assert `status=deleted` and `deleted_at IS NOT NULL`.

- [ ] **Step 6: 核验 Milvus**

List chunks for the explicit owner/KB. Assert every active document ID has 1..6 chunks with matching owner/tenant/KB scope and every replaced document ID has zero chunks. Save only counts/IDs, never content.

- [ ] **Step 7: 不提交 runtime evidence**

Run `git status --short --ignored` and confirm staging, reports, credentials and evidence are ignored.

---

### Task 11: 真实 60 查询 Eval、文档和最终回归

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `apps/backend/README.md`
- Modify: `apps/backend/tests/test_local_development_docs.py`
- Modify: `openspec/changes/expand-rag-retrieval-benchmark/tasks.md`
- Runtime only: `apps/backend/var/benchmarks/retrieval-30-card-v1.json`

**Interfaces:** consumes verified 30-card KB and 60 labels.

- [ ] **Step 1: 写文档失败测试**

Require docs to mention `30 张`, `60 条`, `54 条有答案`, `6 条无答案探针`, `Document Recall@3`, `docker_validation: pending`, and that probes do not gate exit status.

- [ ] **Step 2: 运行并确认文档红灯**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_local_development_docs.py -q
```

Expected: FAIL until docs are updated.

- [ ] **Step 3: 运行一次真实 60 查询 Eval**

```powershell
.venv\Scripts\python.exe scripts/run_retrieval_benchmark.py --owner-user-id <verified-owner> --knowledge-base-id <verified-kb> --output var/benchmarks/retrieval-30-card-v1.json
```

Expected answerable gates:

```text
Document Recall@1 >= 0.80
Document Recall@3 >= 0.95
Document MRR >= 0.85
Forbidden Top-1 Rate <= 0.05
Citation Completeness = 1.00
```

No-answer probes are diagnostic only. If answerable gates fail, keep report/bad cases and stop tuning; do not change labels or delete queries.

- [ ] **Step 4: 更新文档与真实基线**

Document model names, query/catalog versions, five answerable metrics, no-answer diagnostic summary, that Docker was not run, and the ignored report path. Do not commit runtime IDs or raw scores if they expose tenant mapping.

- [ ] **Step 5: 聚焦回归**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py tests/test_knowledge_catalog.py tests/test_document_indexing.py tests/test_retrieval_evaluation.py tests/test_retrieval_benchmark_cli.py tests/test_import_knowledge_batch.py tests/test_local_development_docs.py -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pyright
```

Expected: zero failures/errors, excluding documented environment skips.

- [ ] **Step 6: OpenSpec 与有界普通回归**

```powershell
cd ../..
npx --yes @fission-ai/openspec@1.6.0 validate --all
cd apps/backend
.venv\Scripts\python.exe -m pytest
```

Use a bounded local window. If the ordinary suite again exceeds the window without a failure summary, leave its task unchecked and require GitHub Actions `backend-tests` before merge.

- [ ] **Step 7: 按证据勾选 OpenSpec tasks**

Mark only completed items. Docker/Live validation remains explicitly pending and is not part of this change.

- [ ] **Step 8: 提交文档和完成状态**

```powershell
git add docs/aiops/agentpy-domainbench.md apps/backend/README.md apps/backend/tests/test_local_development_docs.py openspec/changes/expand-rag-retrieval-benchmark/tasks.md
git commit -m "docs: record thirty-card retrieval baseline"
```

## Plan Self-Review

- Spec coverage: exact 30-card catalog, 23 new cards, source/license/validation metadata, 60-query distribution, 54/6 split, all-card coverage, no-answer calibration boundary, document deduplication, citation completeness, chunk budget, import verification and real evaluation each map to an explicit task.
- Isolation coverage: only `docs/knowledge-candidates` is staged for import; labels and Diagnosis files have no importer path.
- Type consistency: Task 7 defines query/result/evaluation fields consumed by Tasks 8–9; runner report camelCase fields are explicitly named.
- Reuse coverage: local chunker/importer/retrieval/scorer are reused; GitHub candidates and licenses are recorded; no dependency adoption is planned.
- TDD coverage: contracts fail before production/data implementation for catalog, audit CLI, DTO/loader, scoring, citation handling, query distribution and docs.
- Runtime safety: real import follows duplicate audit and chunk audit; evidence/report paths are ignored; failure stops before downstream model calls.
- Placeholder scan: runtime owner/KB remain explicit operator values by design and are obtained in Task 10; every implementation behavior has a concrete step and acceptance command.
