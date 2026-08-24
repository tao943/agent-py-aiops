# Agent Py Code Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Repository policy prohibits implementation subagents; only this completed plan receives one read-only subagent review. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove one unreachable Vue view and one unused frontend constant, internalize seven file-local TypeScript symbols, and prove that all scanner-reported Python/dependency candidates are required or false positives.

**Architecture:** Keep runtime behavior and module boundaries unchanged. Use pinned one-off Vulture, Deptry, and Knip scans as advisory discovery, require repository-wide evidence before deletion, make one focused frontend cleanup commit, then remove the temporary design/plan artifacts so the final branch tree contains only the code-governance result.

**Tech Stack:** Vue 3, TypeScript 5.6, `vue-tsc`, Vitest, Vite, Python 3.10+, Ruff, strict Pyright, Vulture 2.16, Deptry 0.25.1, Knip 6.32.2, npm workspaces, Git.

## Global Constraints

- Work only in `D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\code-hygiene` on branch `chore/code-hygiene`, based on `origin/main` commit `ac68239`.
- Do not read, stage, modify, or commit the primary worktree's local commits or seven untracked tutorial/plan files.
- Do not delete or modify any local or remote Git branch, tag, release, workflow run, artifact, or GitHub repository setting.
- Do not alter runtime behavior, API/SSE contracts, database schema, configuration fields, evaluation scoring, recovery policy, LangGraph topology, MCP tools, or dependency versions.
- Preserve all Alembic migrations, Benchmark fixtures, ground truth, provenance, RAG knowledge cards, Live fault injectors, Repository Protocols, recovery allowlists, Validators, audit logic, and tenant isolation logic.
- Vulture, Deptry, and Knip are fixed-version advisory tools only. Do not add them to `pyproject.toml`, `package.json`, `uv.lock`, or `package-lock.json`; never use Knip `--fix` or `--allow-remove-files`.
- The only allowed product-tree changes are the eight frontend paths named in Task 2. No backend, shared-contract, dependency, infrastructure, benchmark, config, OpenSpec, CI, or public documentation file may remain changed in the final diff.
- Local frontend checks must use sanitized configuration created by `scripts/ci/prepare_test_config.py`; never copy or read real `config/project.json` or `config/user.project.json` from another worktree.
- Do not run the full local backend pytest suite. This change has no backend diff; remote Linux/Python 3.13 CI remains the final full-suite gate.
- The temporary design and implementation plan are process assets. They must be removed from the final tree before publication, while remaining recoverable from branch history.

---

### Task 1: Reproduce the baseline and freeze the evidence ledger

**Files:**
- Verify only: `.github/workflows/ci.yml`
- Verify only: `apps/backend/pyproject.toml`
- Verify only: `package.json`
- Verify only: `apps/frontend/package.json`
- Verify only: `packages/api-contracts/package.json`
- Generated and ignored: `config/project.json`
- Generated and ignored: `config/user.project.json`

**Interfaces:**
- Consumes: clean `origin/main` tree, committed design `1845e93`, sanitized config templates, existing npm and Python manifests.
- Produces: a reproducible green frontend baseline and an exact candidate/disposition list for Task 2. No tracked file changes.

- [ ] **Step 1: Confirm isolation, ancestry, and a clean tracked worktree**

Run:

```powershell
$expectedRoot = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\code-hygiene'
if ((Resolve-Path '.').Path -ne $expectedRoot) { throw "Wrong worktree: $((Resolve-Path '.').Path)" }
if ((git branch --show-current) -ne 'chore/code-hygiene') { throw 'Wrong branch' }
git merge-base --is-ancestor ac68239 HEAD
if ($LASTEXITCODE -ne 0) { throw 'Branch is not based on the reviewed main commit' }
$unexpected = git status --short | Where-Object {
  $_ -notmatch 'docs/superpowers/(specs/2026-08-24-code-hygiene-design|plans/2026-08-24-code-hygiene)\.md$'
}
if ($unexpected) { throw "Unexpected worktree changes: $($unexpected -join ', ')" }
```

Expected: no exception. The only process documents are the already committed design and this plan.

- [ ] **Step 2: Install locked Node dependencies and create sanitized local config**

Run:

```powershell
npm ci
python scripts/ci/prepare_test_config.py --repo-root . --output-dir config
git diff --exit-code -- package-lock.json
```

Expected: npm installs successfully; `config/project.json` and `config/user.project.json` are ignored; `package-lock.json` is unchanged.

- [ ] **Step 3: Prove the pre-change frontend and shared-contract baseline**

Run:

```powershell
npm run contracts:typecheck
npm --workspace packages/api-contracts run test
npm run frontend:typecheck
npm run frontend:test
npm run frontend:build
```

Expected: all commands pass before cleanup. If any command fails, stop and report the baseline failure instead of editing product code.

- [ ] **Step 4: Reproduce the exact Knip candidates without modifying files**

Run:

```powershell
npx --yes knip@6.32.2 `
  --workspace apps/frontend `
  --workspace packages/api-contracts `
  --include files,exports,types `
  --no-progress `
  --reporter compact
```

Expected: exit code 1 with exactly these findings and no shared-contract finding:

```text
Unused files (1)
apps/frontend/src/views/WorkspacePlaceholderView.vue
Unused exports (2)
apps/frontend/src/foundation.ts: WORKBENCH_NAVIGATION_LABELS
apps/frontend/src/knowledge/documentPolicy.ts: formatBytes
Unused exported types (5)
apps/frontend/src/agentConfiguration/agentConfigurationClient.ts: CreateAgentDraftRequest
apps/frontend/src/authState.ts: AuthSnapshot
apps/frontend/src/protectedDataState.ts: ProtectedDataSnapshot
apps/frontend/src/stores/aiops.ts: PublicSpecialistStatus, PublicSpecialistResult
apps/frontend/src/ui/asyncStatus.ts: AsyncStatusTone
```

If the candidate set differs, stop and re-evaluate the design rather than broadening the deletion set.

- [ ] **Step 5: Reproduce and classify the Python Vulture findings**

Run from `apps/backend`:

```powershell
uvx --from vulture==2.16 vulture src/super_ai --min-confidence 80 --sort-by-size
```

Expected: only `nx`, `px`, `texts`, and `ex` are reported. Verify their definitions and call contracts:

```powershell
rg -n 'client\.set\(key, token, nx=True, px=|class RedisLeaseClient' src tests
rg -n 'aembed_documents\(|class EmbeddingModel' src tests
rg -n '\.set\([^\r\n]*ex=|class RedisJsonClient' src tests
```

Disposition: retain all four names. They are required keyword parameter names in `Protocol` boundaries matching redis-py or LangChain call signatures; deleting or renaming them would weaken or break structural typing. Do not edit backend files.

- [ ] **Step 6: Reproduce and classify dependency-only findings**

Run from `apps/backend`:

```powershell
uvx --from deptry==0.25.1 deptry . --ignore DEP001,DEP003,DEP004 --no-ansi
```

Expected: Deptry reports `greenlet`, `pymilvus`, `python-multipart`, `pyyaml`, `rank-bm25`, `tencentcloud-cls-sdk-python`, and `uvicorn` as DEP002 candidates. Verify their actual roles:

```powershell
rg -n 'import_module\("pymilvus"\)|import yaml|rank_bm25|import_module\("tencentcloud|uv run uvicorn|python-multipart|greenlet' `
  src tests scripts pyproject.toml ..\..\scripts ..\..\docs ..\..\README.md
```

Disposition: retain every dependency.

- `greenlet` is an explicit SQLAlchemy async runtime dependency.
- `pymilvus` and the Tencent CLS SDK are loaded through `import_module`.
- `python-multipart` is FastAPI's runtime parser for uploaded forms/files.
- `pyyaml` maps to the imported module name `yaml`.
- `rank-bm25` maps to `rank_bm25` and supports hybrid retrieval.
- `uvicorn` is the documented and scripted application server CLI.

Do not edit `pyproject.toml` or `uv.lock`.

- [ ] **Step 7: Confirm Task 1 produced no tracked changes**

Run from the repository root:

```powershell
git diff --exit-code -- package.json package-lock.json apps/backend/pyproject.toml apps/backend/uv.lock
git status --short
```

Expected: no manifest or lockfile diff; no uncommitted tracked file changes. Do not commit Task 1 because it intentionally produces no repository diff.

---

### Task 2: Remove the unreachable frontend asset and internalize file-local symbols

**Files:**
- Delete: `apps/frontend/src/views/WorkspacePlaceholderView.vue`
- Modify: `apps/frontend/src/foundation.ts`
- Modify: `apps/frontend/src/knowledge/documentPolicy.ts`
- Modify: `apps/frontend/src/agentConfiguration/agentConfigurationClient.ts`
- Modify: `apps/frontend/src/authState.ts`
- Modify: `apps/frontend/src/protectedDataState.ts`
- Modify: `apps/frontend/src/stores/aiops.ts`
- Modify: `apps/frontend/src/ui/asyncStatus.ts`

**Interfaces:**
- Consumes: the exact Knip candidate set and five-gate evidence from Task 1.
- Produces: the same runtime behavior with no unreachable view, no unused navigation constant, and no unnecessary public TypeScript symbols.

- [ ] **Step 1: Prove the view and navigation constant have no static, dynamic, test, script, or documentation entry**

Run:

```powershell
$viewHits = rg -n 'WorkspacePlaceholderView|placeholder-view' apps packages scripts docs README.md AGENTS.md `
  --glob '!apps/frontend/src/views/WorkspacePlaceholderView.vue' `
  --glob '!docs/superpowers/**'
if ($LASTEXITCODE -eq 0) { throw "Unexpected placeholder references: $viewHits" }
if ($LASTEXITCODE -ne 1) { throw 'Placeholder reference scan failed' }
$labelHits = rg -n 'WORKBENCH_NAVIGATION_LABELS' apps packages scripts docs README.md AGENTS.md --glob '!docs/superpowers/**'
if (($labelHits | Measure-Object).Count -ne 1) { throw "Navigation label references changed: $labelHits" }
```

Expected: no placeholder reference outside its own filename/content, and exactly one navigation-label hit at its definition. Git history shows both came from the obsolete workbench shell/baseline and current routes use explicit real views.

- [ ] **Step 2: Delete only the unreachable placeholder view**

Use `apply_patch` to delete the complete tracked file:

```diff
*** Delete File: apps/frontend/src/views/WorkspacePlaceholderView.vue
```

Then verify:

```powershell
if (Test-Path 'apps/frontend/src/views/WorkspacePlaceholderView.vue') { throw 'Placeholder view still exists' }
```

Expected: the file is absent; no other view or router file changes.

- [ ] **Step 3: Remove only the unused navigation constant**

In `apps/frontend/src/foundation.ts`, delete this complete block and leave all health/contract functions unchanged:

```typescript
export const WORKBENCH_NAVIGATION_LABELS = [
  "事件中心",
  "调查工作台",
  "运维助手",
  "知识中心",
  "Agent 配置",
  "集成中心",
  "系统状态"
] as const;
```

Expected: imports and the four existing exported foundation functions remain byte-for-byte unchanged.

- [ ] **Step 4: Internalize the seven symbols that are used only within their defining files**

Make these exact declaration changes; do not rename a symbol or alter its body:

```diff
-export function formatBytes(bytes: number): string {
+function formatBytes(bytes: number): string {

-export interface CreateAgentDraftRequest {
+interface CreateAgentDraftRequest {

-export interface AuthSnapshot {
+interface AuthSnapshot {

-export interface ProtectedDataSnapshot {
+interface ProtectedDataSnapshot {

-export type PublicSpecialistStatus =
+type PublicSpecialistStatus =

-export interface PublicSpecialistResult {
+interface PublicSpecialistResult {

-export type AsyncStatusTone =
+type AsyncStatusTone =
```

Expected: public functions/interfaces that legitimately consume these structural types remain exported; only the unnecessary export modifiers are removed.

- [ ] **Step 5: Run focused behavior tests before the broad frontend gate**

Run:

```powershell
npm --workspace apps/frontend run test -- `
  tests/foundation.test.ts `
  tests/contracts.test.ts `
  tests/knowledgeComponents.test.ts `
  tests/agentConfigurationClient.test.ts `
  tests/agentConfigurationStore.test.ts `
  tests/auth.test.ts `
  tests/protectedData.test.ts `
  tests/aiopsStore.test.ts `
  tests/chineseWorkspace.test.ts
```

Expected: every selected Vitest file passes. No new test is required because runtime behavior is unchanged and the red/green contract is the Knip issue set.

- [ ] **Step 6: Prove the exact Knip issue set is gone**

Run:

```powershell
npx --yes knip@6.32.2 `
  --workspace apps/frontend `
  --workspace packages/api-contracts `
  --include files,exports,types `
  --no-progress `
  --reporter compact
```

Expected: exit code 0 and no findings. Do not add ignores or a committed Knip configuration to obtain a clean result.

- [ ] **Step 7: Run frontend/static gates and inspect the exact diff**

Run:

```powershell
npm run contracts:typecheck
npm run frontend:typecheck
npm run frontend:test
npm run frontend:build
git diff --check
$changed = git diff --name-only
$expected = @(
  'apps/frontend/src/agentConfiguration/agentConfigurationClient.ts',
  'apps/frontend/src/authState.ts',
  'apps/frontend/src/foundation.ts',
  'apps/frontend/src/knowledge/documentPolicy.ts',
  'apps/frontend/src/protectedDataState.ts',
  'apps/frontend/src/stores/aiops.ts',
  'apps/frontend/src/ui/asyncStatus.ts',
  'apps/frontend/src/views/WorkspacePlaceholderView.vue'
) | Sort-Object
$difference = Compare-Object $expected ($changed | Sort-Object)
if ($difference) { throw "Unexpected product diff: $($difference | Out-String)" }
```

Expected: all gates pass and exactly the eight reviewed paths are changed.

- [ ] **Step 8: Commit the focused code-governance result**

Run:

```powershell
git add -- apps/frontend/src
git diff --cached --check
git commit -m "chore: remove unused frontend surface"
```

Expected: one commit containing one deleted Vue view, one deleted constant, and seven removed `export` modifiers.

---

### Task 3: Remove process artifacts and perform final branch acceptance

**Files:**
- Delete: `docs/superpowers/specs/2026-08-24-code-hygiene-design.md`
- Delete: `docs/superpowers/plans/2026-08-24-code-hygiene.md`
- Verify only: all product, manifest, lock, CI, docs, spec, benchmark, config, and infrastructure paths outside Task 2.

**Interfaces:**
- Consumes: the focused Task 2 commit and its green frontend gates.
- Produces: a clean PR diff against `origin/main` containing only the eight frontend paths, plus auditable scan/verification evidence for the PR description.

- [ ] **Step 1: Remove the temporary process documents from the final tree**

Run:

```powershell
git rm -- `
  docs/superpowers/specs/2026-08-24-code-hygiene-design.md `
  docs/superpowers/plans/2026-08-24-code-hygiene.md
git commit -m "chore: retire code hygiene process artifacts"
```

Expected: both documents remain available in commits `1845e93` and the plan commit, but are absent from the final tree.

- [ ] **Step 2: Verify final scope and protected assets**

Run:

```powershell
$finalChanged = git diff --name-only origin/main...HEAD | Sort-Object
$expected = @(
  'apps/frontend/src/agentConfiguration/agentConfigurationClient.ts',
  'apps/frontend/src/authState.ts',
  'apps/frontend/src/foundation.ts',
  'apps/frontend/src/knowledge/documentPolicy.ts',
  'apps/frontend/src/protectedDataState.ts',
  'apps/frontend/src/stores/aiops.ts',
  'apps/frontend/src/ui/asyncStatus.ts',
  'apps/frontend/src/views/WorkspacePlaceholderView.vue'
) | Sort-Object
$difference = Compare-Object $expected $finalChanged
if ($difference) { throw "Final diff is out of scope: $($difference | Out-String)" }
if (git diff --name-only origin/main...HEAD -- apps/backend packages benchmarks config infra scripts openspec .github package.json package-lock.json) {
  throw 'Protected backend, contract, data, config, infrastructure, script, spec, CI, or manifest path changed'
}
if ((Get-ChildItem 'docs/knowledge-candidates' -File -Filter '*.md').Count -ne 30) { throw 'RAG card corpus changed' }
if ((Get-ChildItem 'benchmarks/agentpy/scenarios' -Directory).Count -ne 10) { throw 'Snapshot corpus changed' }
if ((Get-ChildItem 'benchmarks/agentpy/live' -Directory).Count -ne 5) { throw 'Live corpus changed' }
```

Expected: only the eight Task 2 paths differ; protected assets and corpus counts remain unchanged.

- [ ] **Step 3: Re-run the final local acceptance suite**

Run:

```powershell
npx --yes knip@6.32.2 `
  --workspace apps/frontend `
  --workspace packages/api-contracts `
  --include files,exports,types `
  --no-progress `
  --reporter compact
npm run contracts:typecheck
npm --workspace packages/api-contracts run test
npm run frontend:typecheck
npm run frontend:test
npm run frontend:build
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: Knip produces no finding; every check passes; the worktree is clean and ahead of `origin/main` only by the reviewed commits.

- [ ] **Step 4: Prepare publication without performing unapproved remote operations**

Summarize in the PR body:

- Knip started with 1 unused file, 2 unused exports, and 5 unused exported-type groups, then finished with zero findings in the two workspaces.
- Vulture's four findings were required Protocol keyword parameters and were preserved.
- Deptry's seven dependency candidates were package-name, dynamic-import, framework-runtime, or CLI false positives and were preserved.
- Runtime behavior, backend, contracts, manifests, locks, benchmarks, RAG cards, specs, infrastructure, CI, and GitHub settings were not changed.
- List the exact commands from Task 3 Step 3.

Do not push, create a PR, merge, delete a branch, or change GitHub settings until the user explicitly requests that external action.
