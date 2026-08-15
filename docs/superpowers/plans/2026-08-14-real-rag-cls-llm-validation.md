# Real RAG, CLS, and LLM Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce reproducible real-service Retrieval, RAG off/on Snapshot, and sequential LLM+CLS Live results for the approved expanded benchmark without weakening labels or leaking answers.

**Architecture:** Add an explicit RAG mode at the Snapshot composition boundary, then use the existing overwrite importer, real hybrid Retrieval CLI, Snapshot runner, Live runner, CLS uploader/poller, and official MCP SearchLog path. Store only ignored safe reports and publish aggregate metrics plus classified bad cases in documentation.

**Tech Stack:** Python 3.10, PostgreSQL, Milvus, DashScope Chat/Embedding/Rerank, Tencent CLS, Docker Compose, pytest, existing AgentPy CLIs

## Global Constraints

- This plan starts only after the Snapshot/Retrieval and multi-scenario Live plans pass offline and Docker verification.
- Use exactly 30 active indexed knowledge cards and exactly 64 Retrieval queries.
- Run the four new Snapshot scenarios once with RAG off and once with RAG on using the same model, prompt, workflow, tools, and fixture.
- Run all four Live scenarios once with real LLM and CLS, sequentially, never concurrently.
- A `VALID_FAIL` is retained as model behavior; an `INFRA_INVALID` is retained as infrastructure invalidity and does not receive an Agent zero score.
- Do not change labels, delete difficult queries, insert oracle text into prompts, or copy evaluator mappings into RAG to improve results.
- Reports must omit API keys, Tencent credentials, DSNs, raw configuration, oracle fields, raw logs, SQL, PIDs, and full retrieved chunks.
- Ordinary CI must continue to exclude real model, Docker, and CLS execution.
- Execute inline in the current session; do not start subagents.

---

### Task 1: Add an explicit, testable Snapshot RAG off/on boundary

**Files:**
- Modify: `apps/backend/scripts/run_snapshot_benchmark.py`
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Create: `apps/backend/tests/test_snapshot_benchmark_cli.py`

**Interfaces:**
- Produces: CLI `--rag-mode {off,on}` (default `on`) and a `NullKnowledgeRetrievalTool` that returns no hits/citations without calling embedding, Milvus, or rerank services.
- Consumes: unchanged `SnapshotBenchmarkRunner` and production Agent workflow; no prompt or oracle change.

- [ ] **Step 1: Write failing parser and no-call tests**

```python
def test_snapshot_cli_has_explicit_rag_mode_defaulting_on() -> None:
    parser = MODULE.build_parser()
    assert parser.parse_args(["--scenario", "APY-013"]).rag_mode == "on"
    assert parser.parse_args(["--scenario", "APY-013", "--rag-mode", "off"]).rag_mode == "off"

@pytest.mark.asyncio
async def test_rag_off_never_calls_embedding_vector_store_or_rerank() -> None:
    tool = NullKnowledgeRetrievalTool()
    result = await tool.run(
        KnowledgeRetrievalToolInput(query="public symptom", top_k=3),
        owner_user_id="owner-a",
        accessible_knowledge_base_ids=("kb-owner-a",),
    )
    assert result.results == []
    assert result.citations == []
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_snapshot_benchmark_runner.py tests/test_snapshot_benchmark_cli.py -q -p no:cacheprovider --basetemp=var/pytest-plan-rag-mode
```

Expected: FAIL because `--rag-mode` and the null tool do not exist.

- [ ] **Step 3: Implement the null retrieval contract**

```python
class NullKnowledgeRetrievalTool:
    async def run(
        self,
        input: KnowledgeRetrievalToolInput,
        *,
        owner_user_id: str,
        accessible_knowledge_base_ids: tuple[str, ...],
    ) -> KnowledgeRetrievalToolResult:
        del owner_user_id, accessible_knowledge_base_ids
        return KnowledgeRetrievalToolResult(
            query=input.query,
            top_k=cast(int, input.top_k),
            results=[],
            citations=[],
        )
```

The CLI constructs this tool only for `off`. For `on`, retain the current `KnowledgeRetrievalTool` with real embedding, Milvus, hybrid retrieval, and rerank. Add `ragMode` to safe report metadata; never add retrieved content.

- [ ] **Step 4: Run tests and commit**

Expected: Step 2 command PASS.

```powershell
git add apps/backend/scripts/run_snapshot_benchmark.py apps/backend/src/super_ai/evaluation/runner.py apps/backend/tests/test_snapshot_benchmark_runner.py apps/backend/tests/test_snapshot_benchmark_cli.py
git commit -m "feat: add explicit snapshot rag comparison mode"
```

### Task 2: Restore and safely overwrite-import exactly thirty cards

**Files:**
- Modify after successful Docker validation: `docs/knowledge-candidates/postgres-deadlock.md`
- Modify after successful Docker validation: `docs/knowledge-candidates/redis-maxclients-pressure.md`
- Modify after successful Docker validation: `docs/knowledge-candidates/nginx-upstream-timeout.md`
- Runtime only: `apps/backend/var/` remains ignored

**Interfaces:**
- Consumes: verified Docker check summaries from the Live runtime plan and existing `import_knowledge_batch.py` overwrite semantics.
- Produces: exactly 30 active indexed documents in the authorized knowledge base; retry-storm remains `docker_validation: pending`.

- [ ] **Step 1: Verify prerequisites without mutation**

```powershell
$env:AGENTPY_EVAL_OWNER_USER_ID | ForEach-Object { if (-not $_) { throw 'AGENTPY_EVAL_OWNER_USER_ID is required.' } }
$OwnerUserId = $env:AGENTPY_EVAL_OWNER_USER_ID
$KnowledgeBaseId = "kb_$OwnerUserId"
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' scripts/audit_knowledge_catalog.py
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' scripts/import_knowledge_batch.py --source-dir ../../../docs/knowledge-candidates --dry-run
```

Expected: catalog audit PASS; dry-run reports `total: 30` and the expected filenames. If either count differs, stop before import.

- [ ] **Step 2: Update only non-answerized validation metadata**

In the three cards, set:

```yaml
docker_validation: verified
docker_validation_date: 2026-08-14
docker_validation_scope: isolated_live_eval_fixture
```

Add one sentence naming only the verified mechanism family and safe check categories. Do not add scenario IDs, run IDs, exact fixture values, evidence IDs, triggers, tool arguments, oracle language, or the recovery answer. Keep `http-rate-limit-retry-storm.md` at `docker_validation: pending`.

- [ ] **Step 3: Run card safety and chunk audits**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_knowledge_candidate_safety.py tests/test_knowledge_catalog.py tests/test_document_indexing.py -q -p no:cacheprovider --basetemp=var/pytest-plan-card-validation
```

Expected: PASS; catalog remains 30 and governance-only sections produce no answer-bearing chunks.

- [ ] **Step 4: Execute overwrite import and bounded indexing poll**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' scripts/import_knowledge_batch.py --source-dir ../../../docs/knowledge-candidates
```

Expected: JSON summary reports total 30, succeeded 30, failed 0; every document finishes active/indexed within configured polling deadline. Multiple active documents with the same filename cause a classified stop, not another import.

- [ ] **Step 5: Commit only source-card changes**

```powershell
git add docs/knowledge-candidates/postgres-deadlock.md docs/knowledge-candidates/redis-maxclients-pressure.md docs/knowledge-candidates/nginx-upstream-timeout.md
git commit -m "docs: record isolated docker knowledge validation"
```

Do not stage database exports, Milvus data, API responses, config files, or `apps/backend/var`.

### Task 3: Run and preserve the real 64-query Retrieval baseline

**Files:**
- Runtime output: `apps/backend/var/benchmarks/retrieval-64-2026-08-14.json` (ignored)
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: 64 reviewed queries, 30 active indexed documents, real embedding/Milvus/BM25/RRF/rerank path.
- Produces: content-free ranking/citation report and updated measured metrics.

- [ ] **Step 1: Run the real benchmark sequentially**

```powershell
$OwnerUserId = $env:AGENTPY_EVAL_OWNER_USER_ID
if (-not $OwnerUserId) { throw 'AGENTPY_EVAL_OWNER_USER_ID is required.' }
$KnowledgeBaseId = "kb_$OwnerUserId"
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' scripts/run_retrieval_benchmark.py --owner-user-id $OwnerUserId --knowledge-base-id $KnowledgeBaseId --output var/benchmarks/retrieval-64-2026-08-14.json
```

Expected: 64 runs; 58 answerable, 6 no-answer; process exit 0 only if Recall@1 ≥ 0.80, Recall@3 ≥ 0.95, MRR ≥ 0.85, forbidden Top-1 ≤ 0.05, and Citation Completeness = 1.00.

- [ ] **Step 2: Classify every regression instead of relabeling it**

For each miss record query ID, target document, ranked document filenames, vector/BM25/rerank ranks, and forbidden-hit status. Classify into one of `knowledge_gap`, `chunk_boundary`, `vector_recall`, `bm25_recall`, `fusion_order`, or `rerank_order`. Do not copy query text or chunk content into the public report.

- [ ] **Step 3: Update the measured baseline documentation**

Record date, exact 30/64 corpus counts, model names without keys/base credentials, all aggregate metrics, channel coverage, threshold result, and the safe bad-case table. Clearly retain the prior 60-query numbers as historical rather than overwriting them.

- [ ] **Step 4: Commit documentation only**

```powershell
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record sixty four query retrieval baseline"
```

### Task 4: Run four controlled Snapshot RAG off/on comparisons

**Files:**
- Runtime output: eight ignored JSON reports under `apps/backend/var/benchmarks/rag-comparison/`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: `--rag-mode`, fixed production model configuration, four new Snapshot fixtures, and the indexed 30-card knowledge base.
- Produces: paired results for root cause, evidence, forbidden claims, tools, citations, score, duration, and model usage.

- [ ] **Step 1: Record immutable comparison metadata**

Before the first run, save the current git SHA, workflow version, prompt version, configured chat/embedding/rerank model names, knowledge catalog hash, and scenario file hashes to the ignored comparison directory. Never store the API key or config body.

- [ ] **Step 2: Run off then on for each scenario, sequentially**

```powershell
$SnapshotScript = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\benchmark-rag-live-expansion\apps\backend\scripts\run_snapshot_benchmark.py'
$Python = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe'
foreach ($Scenario in @('APY-013','APY-014','APY-015','APY-016')) {
  & $Python $SnapshotScript --scenario $Scenario --suite-version v1 --runs 1 --rag-mode off --output "var/benchmarks/rag-comparison/$Scenario-off.json"
  if ($LASTEXITCODE -eq 2) { throw "$Scenario RAG-off infrastructure invalid." }
  & $Python $SnapshotScript --scenario $Scenario --suite-version v1 --runs 1 --rag-mode on --output "var/benchmarks/rag-comparison/$Scenario-on.json"
  if ($LASTEXITCODE -eq 2) { throw "$Scenario RAG-on infrastructure invalid." }
}
```

Expected: eight reports. Exit 1 is retained as `VALID_FAIL`; exit 2 stops the comparison as `INFRA_INVALID`.

- [ ] **Step 3: Audit comparison validity**

Confirm each pair has identical git/workflow/prompt/model/scenario metadata and differs only in `ragMode`. Confirm RAG-off has zero retrieval calls/citations; RAG-on citations belong to the authorized knowledge base and contain no Snapshot ID, oracle field, evaluator mapping, or cross-run reference.

- [ ] **Step 4: Document paired outcomes without claiming universal improvement**

For each scenario record root-cause correctness, required-evidence coverage, forbidden-claim status, tool call count, citation validity, total score, duration, and token/model usage for off/on. Report regressions as observed; do not change labels or prompts in this task.

- [ ] **Step 5: Commit documentation**

```powershell
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: compare rag off and on for expanded snapshots"
```

### Task 5: Run real LLM + CLS acceptance for all four Live scenarios

**Files:**
- Runtime output: four ignored reports under `apps/backend/var/benchmarks/live/`
- Modify: `docs/tutorials/real-log-and-alert.md`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: healthy Docker fixtures, indexed knowledge base, real LLM configuration, real CLS SDK upload, bounded indexing poll, official MCP `SearchLog`, and scenario registry.
- Produces: one classified safe result per Live scenario.

- [ ] **Step 1: Run credential-safe preflight**

```powershell
$OwnerUserId = $env:AGENTPY_EVAL_OWNER_USER_ID
$ClsConfig = $env:LIVE_CLS_CONFIG
if (-not $OwnerUserId) { throw 'AGENTPY_EVAL_OWNER_USER_ID is required.' }
if (-not $ClsConfig -or -not (Test-Path -LiteralPath $ClsConfig)) { throw 'LIVE_CLS_CONFIG must name the local ignored config file.' }
$KnowledgeBaseId = "kb_$OwnerUserId"
docker compose -f ../../../infra/compose.yaml --profile live-eval ps
```

Expected: PostgreSQL and all three `live-eval` services healthy. The command prints no credentials.

- [ ] **Step 2: Run the four scenarios in fixed order**

```powershell
$Python = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe'
$LiveScript = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\benchmark-rag-live-expansion\apps\backend\scripts\run_live_benchmark.py'
$Scenarios = @(
  'APY-LIVE-PG-LOCK-001',
  'APY-LIVE-PG-DEADLOCK-001',
  'APY-LIVE-REDIS-MAXCLIENTS-001',
  'APY-LIVE-NGINX-TIMEOUT-001'
)
foreach ($Scenario in $Scenarios) {
  $RunId = "accept-20260814-$($Scenario.ToLower().Replace('apy-live-',''))"
  & $Python $LiveScript run --scenario $Scenario --run-id $RunId --owner-user-id $OwnerUserId --knowledge-base-id $KnowledgeBaseId --evidence-source cls --config $ClsConfig
  if ($LASTEXITCODE -eq 2) { throw "$Scenario is INFRA_INVALID; stop before the next scenario." }
  & $Python $LiveScript verify --scenario $Scenario --run-id $RunId
  if ($LASTEXITCODE -ne 0) { throw "$Scenario verification failed; stop before the next scenario." }
}
```

Expected per scenario: CLS SDK upload succeeds; bounded polling finds the complete current-run record set; Agent calls official MCP `SearchLog` with region/topic/time and all three identity fields; foreign-run records are filtered; report is `VALID_PASS` or `VALID_FAIL`; verification and cleanup pass.

- [ ] **Step 3: Audit every safe report**

Require `evidenceSource=cls`, valid `scenarioId/runId`, citation to both `SearchLog` and the scenario's authoritative component source, no cross-run evidence, correct recovery expectation, and `cleanupSucceeded=true`. For Nginx require `executed=false`, complete proposal checks, and zero write/reload/restart/switch action.

- [ ] **Step 4: Document results and failure classification**

Record score breakdown, hard gate, validity, safe failure category, CLS readiness attempts/duration, total duration, model name, citation-source names, recovery expectation, and cleanup status. Never paste raw SearchLog output, config, keys, topic secrets, PIDs, client IDs, or full model prompts/responses.

- [ ] **Step 5: Commit documentation**

```powershell
git add docs/tutorials/real-log-and-alert.md docs/aiops/agentpy-domainbench.md
git commit -m "docs: record four scenario llm cls acceptance"
```

### Task 6: Full regression, safety audit, and execution handoff

**Files:**
- Test: full repository changes from all three plans
- Modify only if results require clarification: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: all implemented fixtures, runtime contracts, and real safe reports.
- Produces: final evidence that ordinary CI stays offline and the expanded benchmark is reproducible.

- [ ] **Step 1: Run the full offline regression**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --basetemp=var/pytest-plan-expansion-final
```

Expected: all default tests PASS; `live_docker`, `live_cls`, and `live_llm` external tests are deselected by configured markers and no network credential is read.

- [ ] **Step 2: Run code and spec validation**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m ruff check .
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pyright
openspec validate --all --strict
docker compose -f ../../../infra/compose.yaml config
docker compose -f ../../../infra/compose.yaml --profile live-eval config
```

Expected: Ruff and Pyright report zero issues; OpenSpec and both Compose configs validate.

- [ ] **Step 3: Run answer-isolation and secret scans**

```powershell
rg -n -i "ground_truth|primary_cause|evidence_id|snapshot_knowledge_coverage|secretid|secretkey|api[_-]?key|postgresql://" docs/knowledge-candidates apps/backend/var/benchmarks
git status --short
git diff --check
```

Expected: no answer token in knowledge cards and no secret/DSN in safe reports; known documentation field names are reviewed manually; runtime reports remain ignored and unstaged; `git diff --check` emits no output.

- [ ] **Step 4: Commit only final documentation corrections**

```powershell
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: finalize expanded benchmark acceptance"
```

Skip this commit if no tracked file changed. Do not commit local configs, environment files, reports, caches, or credentials.

## Self-review

- Spec coverage: Task 1 establishes controlled RAG off/on; Task 2 restores exactly 30 indexed cards; Task 3 measures 64-query Retrieval; Task 4 compares four new Snapshots; Task 5 performs four sequential real LLM+CLS Live runs; Task 6 verifies offline CI and safety.
- Placeholder scan: commands use validated environment variables for user-specific IDs and ignored config paths; no deferred implementation marker or fake credential value appears.
- Type consistency: `NullKnowledgeRetrievalTool` implements the existing runner contract; CLI outputs remain safe evaluation payloads; Live validity categories remain `VALID_PASS`, `VALID_FAIL`, and `INFRA_INVALID`.
- Measurement integrity: real failures are classified and preserved; labels, query set, prompt, workflow, and RAG corpus are not altered to manufacture improvement.
