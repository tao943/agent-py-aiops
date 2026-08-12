# Knowledge Index Polling and Bulk Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This project explicitly requires inline execution and no subagents.

**Goal:** Replace fragile document-index polling with one tested API client helper, then import a reviewed Markdown troubleshooting batch through the existing document and index-task APIs.

**Architecture:** Keep FastAPI, PostgreSQL, the durable indexing worker, and Milvus unchanged. A focused module in `apps/backend/scripts` parses the two existing response envelopes and owns bounded polling; the existing SOP seed command and a new batch-import command both call it. Ordinary tests use HTTPX `MockTransport`, while the final opt-in run uses the local backend and verifies persisted/indexed outcomes.

**Tech Stack:** Python 3.10, HTTPX 0.28, argparse, pytest, Ruff, strict Pyright, OpenSpec CLI, FastAPI, PostgreSQL, Milvus.

## Global Constraints

- Execute all tasks inline in the current session; do not start multiple agents.
- Do not change the backend API, PostgreSQL schema, indexing worker, Milvus schema, or frontend contracts.
- `POST .../index-tasks` is parsed from `data.task`; `GET .../index-tasks/{task_id}` is parsed from `data`.
- Do not poll PostgreSQL from an importer or make `/ready` a prerequisite for document indexing.
- Add no dependency, service, native binary, or environment-variable configuration path.
- Import only reviewed Markdown files under an explicitly selected source directory.
- Do not import Benchmark ground truth, hidden evidence, scoring answers, full copied blog posts, secrets, or credentials.
- Do not print API keys, access tokens, passwords, or document bodies.
- Do not delete the two earlier duplicate documents without separate approval.
- Follow red-green-refactor for every production-code behavior.
- A live import is not ordinary CI and may consume configured embedding-model quota.

---

### Task 1: Define the OpenSpec change

**Files:**
- Create: `openspec/changes/add-knowledge-batch-import/.openspec.yaml`
- Create: `openspec/changes/add-knowledge-batch-import/proposal.md`
- Create: `openspec/changes/add-knowledge-batch-import/design.md`
- Create: `openspec/changes/add-knowledge-batch-import/tasks.md`
- Create: `openspec/changes/add-knowledge-batch-import/specs/document-indexing-jobs/spec.md`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-08-12-knowledge-index-polling-and-bulk-import-design.md`.
- Produces: validated behavioral requirements for query-envelope parsing, bounded polling, and auditable sequential batch import.

- [ ] **Step 1: Create the OpenSpec skeleton**

Run from the repository root:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' new change add-knowledge-batch-import
```

Expected: `openspec/changes/add-knowledge-batch-import/` exists with the repository's `spec-driven` schema.

- [ ] **Step 2: Write proposal, design, task list, and delta specification**

The delta spec must include these exact observable requirements and scenarios:

```markdown
## ADDED Requirements

### Requirement: Reliable index task polling client
系统 SHALL 提供一个复用现有索引任务查询 API 的客户端轮询器。轮询器 MUST 从查询响应的 `data.status` 读取状态，MUST 对传输瞬时失败执行受总截止时间约束的有限重试，并 MUST 明确区分成功、终止失败、协议错误和超时。

#### Scenario: Index task advances to success
- **WHEN** 查询 API 依次返回 `pending`、`running` 和 `succeeded`
- **THEN** 轮询器 MUST 返回最终任务，且 MUST NOT 从 `data.task.status` 读取查询状态

#### Scenario: Query response violates the contract
- **WHEN** HTTP 成功响应缺少非空字符串 `data.status`
- **THEN** 轮询器 MUST 立即报告协议错误，且 MUST NOT 把该响应视为仍在运行

### Requirement: Reviewed Markdown batch import
系统 SHALL 提供顺序批量导入命令，通过现有认证、上传和索引任务 API 导入显式目录中的 Markdown 文件，并 SHALL 输出不含凭据和正文的逐项及汇总结果。

#### Scenario: A reviewed batch is imported
- **WHEN** 操作者指定一个受限目录，其中包含经过审核的 Markdown 知识卡
- **THEN** 命令 MUST 按确定性文件顺序上传、创建任务、等待 `succeeded`，并 MUST 报告文件名、文档 ID、任务 ID 和最终状态
```

The OpenSpec design must also state that PostgreSQL is not a client fallback, CLS readiness is unrelated, and ground-truth data is excluded.

- [ ] **Step 3: Validate the OpenSpec change**

Run:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-knowledge-batch-import
```

Expected: validation exits 0 with no missing artifacts or malformed scenarios.

- [ ] **Step 4: Commit the specification**

```powershell
git add -- openspec/changes/add-knowledge-batch-import
git commit -m "spec: define reliable knowledge batch import"
```

Expected: the commit contains only the new OpenSpec change.

---

### Task 2: Add and prove the reusable polling client

**Files:**
- Create: `apps/backend/scripts/knowledge_index_client.py`
- Create: `apps/backend/tests/test_knowledge_index_client.py`
- Modify: `apps/backend/tests/test_document_indexing_api.py`

**Interfaces:**
- Consumes: `httpx.Client.get()`, the existing success envelope, and a fully formed index-task query endpoint.
- Produces: `IndexPollingError`, `IndexTaskFailed`, `IndexPollingTimeout`, `IndexProtocolError`, `parse_created_task(payload) -> dict[str, object]`, and `wait_for_index_task(...) -> dict[str, object]`.

- [ ] **Step 1: Write the API response contract assertion first**

In `test_document_indexing_api.py`, extend the existing task create/read test with:

```python
read_payload = read_response.json()["data"]
assert read_payload["id"] == task["id"]
assert read_payload["status"] == "pending"
assert "task" not in read_payload
```

Run from `apps/backend`:

```powershell
uv run pytest tests/test_document_indexing_api.py::test_document_index_task_api_creates_reads_and_retries_scoped_tasks -q
```

Expected: PASS, documenting the already implemented API contract before client extraction.

- [ ] **Step 2: Write failing polling tests**

Create tests using `httpx.MockTransport` and injected `monotonic`/`sleep` functions. The desired public calls must match this shape:

```python
task = wait_for_index_task(
    client,
    endpoint="/knowledge-bases/kb-a/documents/doc-a/index-tasks/task-a",
    headers={"Authorization": "Bearer redacted"},
    poll_interval_seconds=0,
    deadline=10.0,
    monotonic=clock.monotonic,
    sleep=clock.sleep,
)
assert task["status"] == "succeeded"
```

Add separate tests for:

```text
pending -> running -> succeeded
httpx.ReadTimeout -> succeeded within retry budget
failed with failureReason -> IndexTaskFailed
cancelled -> IndexTaskFailed
deadline exceeded with last status -> IndexPollingTimeout
missing data.status -> IndexProtocolError
unknown status -> IndexProtocolError
POST creation payload uses data.task
```

Run:

```powershell
uv run pytest tests/test_knowledge_index_client.py -q
```

Expected: collection/import FAIL because `knowledge_index_client.py` does not exist.

- [ ] **Step 3: Implement the minimum typed polling module**

The implementation must expose this bounded interface:

```python
class IndexPollingError(RuntimeError): ...
class IndexTaskFailed(IndexPollingError): ...
class IndexPollingTimeout(IndexPollingError): ...
class IndexProtocolError(IndexPollingError): ...

def parse_created_task(payload: object) -> dict[str, object]: ...

def wait_for_index_task(
    client: httpx.Client,
    *,
    endpoint: str,
    headers: Mapping[str, str],
    poll_interval_seconds: float,
    deadline: float,
    transient_retry_limit: int = 2,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]: ...
```

Use only these known states:

```python
ACTIVE_STATUSES = frozenset({"pending", "running"})
SUCCESS_STATUS = "succeeded"
FAILURE_STATUSES = frozenset({"failed", "cancelled"})
```

Each loop must check the overall deadline; only `httpx.TimeoutException` and `httpx.NetworkError` consume transient retries. Reset the consecutive transient count after a valid response. Call `raise_for_status()` for non-success HTTP responses. Parse query payload only from `response.json()["data"]`. Include the last valid status in timeout text and include `failureReason` in terminal failure text when it is a non-empty string.

- [ ] **Step 4: Run the focused tests and refactor only while green**

```powershell
uv run pytest tests/test_knowledge_index_client.py tests/test_document_indexing_api.py -q
uv run ruff check scripts/knowledge_index_client.py tests/test_knowledge_index_client.py tests/test_document_indexing_api.py
uv run pyright scripts/knowledge_index_client.py tests/test_knowledge_index_client.py tests/test_document_indexing_api.py
```

Expected: all commands exit 0. If Pyright does not include `scripts`, pass the file explicitly as shown rather than changing global include scope.

- [ ] **Step 5: Commit the polling client and tests**

```powershell
git add -- apps/backend/scripts/knowledge_index_client.py apps/backend/tests/test_knowledge_index_client.py apps/backend/tests/test_document_indexing_api.py
git commit -m "fix: harden document index polling"
```

---

### Task 3: Migrate the existing SOP seeder to the shared poller

**Files:**
- Modify: `apps/backend/scripts/seed_ecommerce_aiops_sop.py`
- Create: `apps/backend/tests/test_seed_ecommerce_aiops_sop.py`

**Interfaces:**
- Consumes: `parse_created_task()` and `wait_for_index_task()` from Task 2.
- Produces: unchanged `--profile` CLI behavior with no private duplicate `_wait_for_index` loop.

- [ ] **Step 1: Write the failing migration test**

The test must inspect/import the script and prove that the shared helper is called with the task-query endpoint. Use a fake HTTPX client or `MockTransport`; assert the create response is parsed from `data.task`, while query parsing is delegated. Also add a structural regression assertion:

```python
source = SCRIPT_PATH.read_text(encoding="utf-8")
assert "def _wait_for_index(" not in source
assert "from knowledge_index_client import" in source
```

Run:

```powershell
uv run pytest tests/test_seed_ecommerce_aiops_sop.py -q
```

Expected: FAIL because the script still contains its local polling function.

- [ ] **Step 2: Replace only the duplicated polling behavior**

Use:

```python
from knowledge_index_client import parse_created_task, wait_for_index_task
```

Parse the task creation response with `parse_created_task(task_response.json())`, build the existing query endpoint, and call `wait_for_index_task(...)` with the configured interval and deadline. Convert `IndexPollingError` to a concise `SystemExit` at the CLI boundary without printing tokens or response bodies. Remove the local `_wait_for_index` function but retain `_register_or_login`, `_object`, and `_string` where still used.

- [ ] **Step 3: Verify unchanged seeder behavior**

```powershell
uv run pytest tests/test_seed_ecommerce_aiops_sop.py tests/test_ecommerce_aiops_fixtures.py -q
uv run ruff check scripts/seed_ecommerce_aiops_sop.py tests/test_seed_ecommerce_aiops_sop.py
uv run pyright scripts/seed_ecommerce_aiops_sop.py tests/test_seed_ecommerce_aiops_sop.py
```

Expected: all commands exit 0; no live API or embedding call occurs.

- [ ] **Step 4: Commit the migration**

```powershell
git add -- apps/backend/scripts/seed_ecommerce_aiops_sop.py apps/backend/tests/test_seed_ecommerce_aiops_sop.py
git commit -m "refactor: reuse knowledge index poller"
```

---

### Task 4: Build the reviewed Markdown batch importer

**Files:**
- Create: `apps/backend/scripts/import_knowledge_batch.py`
- Create: `apps/backend/tests/test_import_knowledge_batch.py`
- Modify: `config/project.template.json`

**Interfaces:**
- Consumes: `parse_created_task()`, `wait_for_index_task()`, existing `/auth/*`, document upload, and index-task endpoints.
- Produces: `discover_markdown_files(source_dir: Path) -> list[Path]`, `import_batch(...) -> BatchSummary`, and CLI options `--source-dir`, `--continue-on-error`, and `--dry-run`.

- [ ] **Step 1: Write failing discovery and safety tests**

The tests must establish:

```python
files = discover_markdown_files(source_dir)
assert [path.name for path in files] == ["a.md", "b.md"]
```

They must also prove that discovery ignores non-Markdown files and symlinks, rejects a missing/non-directory source, resolves every candidate under the selected root, and never recursively imports an escaped path. Use temporary directories only.

Run:

```powershell
uv run pytest tests/test_import_knowledge_batch.py -q
```

Expected: collection/import FAIL because the importer does not exist.

- [ ] **Step 2: Implement deterministic discovery and dry-run**

Implement sorted, non-recursive `*.md` discovery. `--dry-run` must print only the selected relative filenames and total count, perform no authentication or HTTP call, and return a non-zero exit when no eligible files exist.

Run the focused tests again. Expected: discovery and dry-run tests PASS.

- [ ] **Step 3: Write failing sequential-import and summary tests**

Using `httpx.MockTransport`, assert the exact request sequence for each file:

```text
POST /auth/register (or login after 409)
POST /knowledge-bases/{kb_id}/documents
POST /knowledge-bases/{kb_id}/documents/{document_id}/index-tasks
GET  /knowledge-bases/{kb_id}/documents/{document_id}/index-tasks/{task_id}
```

Assert that a successful result records only `filename`, `document_id`, `task_id`, and `status`. Add separate tests for default fail-fast and `--continue-on-error`, where the latter imports later files and exits non-zero with accurate succeeded/failed counts.

Run:

```powershell
uv run pytest tests/test_import_knowledge_batch.py -q
```

Expected: FAIL because sequential import and summaries are not implemented.

- [ ] **Step 4: Implement the minimum batch workflow**

Use frozen dataclasses with these shapes:

```python
@dataclass(frozen=True)
class ImportResult:
    filename: str
    document_id: str | None
    task_id: str | None
    status: Literal["succeeded", "failed"]
    error: str | None = None

@dataclass(frozen=True)
class BatchSummary:
    results: tuple[ImportResult, ...]

    @property
    def succeeded(self) -> int: ...

    @property
    def failed(self) -> int: ...
```

Authenticate once, derive `kb_{user_id}`, then process files sequentially. Reuse the existing `aiopsDemo` credentials and polling configuration; do not add another password or API-key field. Add a `knowledgeBatch.sourceDir` default of `docs/knowledge-candidates` only to the tracked template, while allowing the CLI argument to override it. Serialize the final summary with `json.dumps()` and never include headers, tokens, passwords, or content.

- [ ] **Step 5: Run focused quality checks**

```powershell
uv run pytest tests/test_import_knowledge_batch.py tests/test_knowledge_index_client.py tests/test_seed_ecommerce_aiops_sop.py -q
uv run ruff check scripts/import_knowledge_batch.py tests/test_import_knowledge_batch.py config/project.template.json
uv run pyright scripts/import_knowledge_batch.py tests/test_import_knowledge_batch.py
```

Expected: Pytest, Ruff, and Pyright exit 0. If Ruff cannot lint JSON, remove only the JSON path from the Ruff invocation; validate JSON separately with the repository's configuration tests.

- [ ] **Step 6: Verify the selected local batch without network calls**

Run from `apps/backend`:

```powershell
uv run python scripts/import_knowledge_batch.py --source-dir ../../../docs/knowledge-candidates --dry-run
```

Expected: exactly the seven currently reviewed Markdown filenames are listed in deterministic order, total `7`, and no credential or document body appears.

- [ ] **Step 7: Commit code and reviewed knowledge cards intentionally**

First inspect the file list and scan for forbidden oracle/secrets:

```powershell
rg -n "ground_truth|primary_cause|oracle|api[_-]?key|accessToken|secretKey" docs/knowledge-candidates
git diff --check
```

Expected: no Benchmark oracle or credential matches. Then commit only the importer, tests, template field, and seven reviewed cards:

```powershell
git add -- apps/backend/scripts/import_knowledge_batch.py apps/backend/tests/test_import_knowledge_batch.py config/project.template.json docs/knowledge-candidates
git commit -m "feat: add reviewed knowledge batch import"
```

---

### Task 5: Run regression checks and perform the live batch import

**Files:**
- Modify: `openspec/changes/add-knowledge-batch-import/tasks.md`
- Modify if command documentation is needed: `apps/backend/README.md`

**Interfaces:**
- Consumes: the local backend at the configured `aiopsDemo.backendBaseUrl`, valid local credentials, PostgreSQL, embedding model, and Milvus.
- Produces: seven confirmed indexing outcomes plus persisted task/document and vector evidence.

- [ ] **Step 1: Run deterministic backend regression checks**

From `apps/backend`:

```powershell
uv run pytest tests/test_knowledge_index_client.py tests/test_seed_ecommerce_aiops_sop.py tests/test_import_knowledge_batch.py tests/test_document_indexing_api.py -q
uv run ruff check .
uv run pyright
```

Expected: all commands exit 0. These commands must not call DashScope because ordinary Pytest excludes `live_llm` and the new tests use mock transports.

- [ ] **Step 2: Confirm live prerequisites without relying on CLS**

Query `/health` and the document/indexing dependencies already exposed by the application. Confirm PostgreSQL, Milvus, Redis, and LLM/embedding configuration are usable. Treat unavailable CLS MCP as unrelated and do not require `/ready` to return 200.

Expected: the backend responds, PostgreSQL and Milvus are healthy, and no secret values are printed.

- [ ] **Step 3: Execute the live batch importer once**

From `apps/backend`:

```powershell
uv run python scripts/import_knowledge_batch.py --source-dir ../../../docs/knowledge-candidates
```

Expected: exit 0; the JSON summary reports `succeeded: 7`, `failed: 0`, and seven items with final status `succeeded`. Do not re-run automatically if the command response is ambiguous; inspect persisted state first to avoid duplicates.

- [ ] **Step 4: Verify PostgreSQL and Milvus evidence read-only**

Use the repository's existing database/vector inspection path or a narrowly scoped read-only query. PostgreSQL evidence must show each reported task ID as `succeeded` and each document as `indexed`. Milvus evidence must show at least one owner/tenant/knowledge-base-scoped chunk for every newly reported document ID.

Expected: all seven document IDs have both persistence and vector evidence. If any item is absent, report it as incomplete instead of changing the summary manually or importing it again blindly.

- [ ] **Step 5: Update operational documentation and OpenSpec task state**

Document the dry-run and live commands, response-envelope distinction, retry/deadline behavior, non-CI live requirement, and verification rule. Mark an OpenSpec task complete only after its associated command/evidence succeeds.

- [ ] **Step 6: Validate all specifications and final code**

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate --all
cd apps/backend
uv run pytest tests/test_knowledge_index_client.py tests/test_seed_ecommerce_aiops_sop.py tests/test_import_knowledge_batch.py tests/test_document_indexing_api.py -q
uv run ruff check .
uv run pyright
```

Expected: every command exits 0.

- [ ] **Step 7: Commit verification documentation without generated/runtime data**

```powershell
git add -- apps/backend/README.md openspec/changes/add-knowledge-batch-import/tasks.md
git diff --cached --check
git commit -m "docs: document knowledge batch operations"
```

Do not add `config/project.json`, `config/user.project.json`, `apps/backend/var`, database dumps, Milvus data, logs, tokens, or API responses.

- [ ] **Step 8: Inspect final workspace state**

```powershell
git status --short
git log -6 --oneline
```

Expected: no uncommitted tracked task changes remain. Ignored local configuration and runtime files remain uncommitted.
