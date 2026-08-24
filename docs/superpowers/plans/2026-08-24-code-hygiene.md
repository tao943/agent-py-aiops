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
- Do not run the full local backend pytest suite. Record successful baseline run `32702871796` for `ac68239`, run local Ruff and strict Pyright on the final branch, and expect the path-filtered PR to select frontend rather than backend jobs.
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

- [ ] **Step 4: Record the successful backend baseline and run local backend quality gates**

Run from the repository root:

```powershell
$run = gh run view 32702871796 --json headSha,conclusion,jobs | ConvertFrom-Json
if ($run.conclusion -ne 'success' -or -not $run.headSha.StartsWith('ac68239')) {
  throw 'Reviewed main CI run is not a successful ac68239 baseline'
}
$requiredJobs = @('backend-quality', 'backend-tests')
foreach ($name in $requiredJobs) {
  $job = $run.jobs | Where-Object name -eq $name
  if ($null -eq $job -or $job.conclusion -ne 'success') {
    throw "Baseline backend job did not pass: $name"
  }
}
Push-Location apps/backend
uv sync --frozen
uv run ruff check .
uv run pyright
Pop-Location
```

Expected: GitHub run `32702871796` is a successful full-suite run for merge commit `ac68239`; local Ruff and strict Pyright both pass. Do not run local full pytest.

- [ ] **Step 5: Reproduce the exact Knip candidates without modifying files**

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

- [ ] **Step 6: Reproduce and classify the Python Vulture findings**

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

- [ ] **Step 7: Reproduce and classify dependency-only findings**

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

- [ ] **Step 8: Confirm Task 1 produced no tracked changes**

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

- [ ] **Step 1: Apply the five-gate evidence check to all nine candidates across every tracked path**

Run:

```powershell
$placeholderFiles = @(git ls-files '*WorkspacePlaceholderView.vue')
if ($placeholderFiles.Count -ne 1 -or $placeholderFiles[0] -ne 'apps/frontend/src/views/WorkspacePlaceholderView.vue') {
  throw "Placeholder file target changed: $($placeholderFiles -join ', ')"
}
$viewHits = git grep -n -E 'WorkspacePlaceholderView|placeholder-view' -- . `
  ':(exclude)apps/frontend/src/views/WorkspacePlaceholderView.vue' `
  ':(exclude)docs/superpowers/**'
if ($LASTEXITCODE -eq 0) { throw "Unexpected external placeholder reference: $viewHits" }
if ($LASTEXITCODE -ne 1) { throw 'Tracked placeholder reference scan failed' }

$expectedCounts = [ordered]@{
  WORKBENCH_NAVIGATION_LABELS = 1
  formatBytes = 3
  CreateAgentDraftRequest = 2
  AuthSnapshot = 2
  ProtectedDataSnapshot = 2
  PublicSpecialistStatus = 4
  PublicSpecialistResult = 2
  AsyncStatusTone = 2
}
foreach ($symbol in $expectedCounts.Keys) {
  $hits = @(git grep -n -w $symbol -- . ':(exclude)docs/superpowers/**')
  if ($hits.Count -ne $expectedCounts[$symbol]) {
    throw "Tracked references changed for ${symbol}: $($hits -join [Environment]::NewLine)"
  }
  $foreign = $hits | Where-Object {
    $_ -notmatch [regex]::Escape((@{
      WORKBENCH_NAVIGATION_LABELS = 'apps/frontend/src/foundation.ts'
      formatBytes = 'apps/frontend/src/knowledge/documentPolicy.ts'
      CreateAgentDraftRequest = 'apps/frontend/src/agentConfiguration/agentConfigurationClient.ts'
      AuthSnapshot = 'apps/frontend/src/authState.ts'
      ProtectedDataSnapshot = 'apps/frontend/src/protectedDataState.ts'
      PublicSpecialistStatus = 'apps/frontend/src/stores/aiops.ts'
      PublicSpecialistResult = 'apps/frontend/src/stores/aiops.ts'
      AsyncStatusTone = 'apps/frontend/src/ui/asyncStatus.ts'
    })[$symbol])
  }
  if ($foreign) { throw "Cross-file consumer found for ${symbol}: $($foreign -join ', ')" }
}

$router = Get-Content -Raw 'apps/frontend/src/router/index.ts'
if ($router -match 'WorkspacePlaceholder') { throw 'Current router still registers the placeholder view' }
$realViews = @(
  'AuthView', 'AgentConfigurationView', 'ChatView', 'IncidentCenterView',
  'IncidentWorkspaceView', 'IntegrationsView', 'KnowledgeView', 'SystemStatusView'
)
foreach ($view in $realViews) {
  if ($router -notmatch [regex]::Escape($view)) { throw "Expected concrete route view missing: $view" }
}
$viewHistory = git log --all --oneline --follow -- apps/frontend/src/views/WorkspacePlaceholderView.vue
$labelHistory = git log --all --oneline -S'WORKBENCH_NAVIGATION_LABELS' -- apps/frontend/src/foundation.ts
if (-not $viewHistory -or -not $labelHistory) { throw 'Candidate history evidence is missing' }
```

Expected: the file has no external tracked reference; every symbol is referenced only within its defining file, including scans of `.github/`, `infra/`, `config/`, `openspec/`, tests, scripts, docs, packages, and apps. The current router registers concrete product views, while Git history identifies the placeholder/label list as obsolete shell-era assets. The seven internalized symbols keep their implementations and consumers, so product capability is preserved.

- [ ] **Step 2: Remove the tightly related obsolete workbench-shell assets**

Use `apply_patch` to delete the complete tracked view:

```diff
*** Delete File: apps/frontend/src/views/WorkspacePlaceholderView.vue
```

In the same tightly related shell cleanup, delete this complete block from `apps/frontend/src/foundation.ts` and leave all health/contract functions unchanged:

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

Verify and commit only this shell group:

Run:

```powershell
if (Test-Path 'apps/frontend/src/views/WorkspacePlaceholderView.vue') { throw 'Placeholder view still exists' }
npm --workspace apps/frontend run test -- `
  tests/foundation.test.ts tests/contracts.test.ts `
  tests/appShellRouter.test.ts tests/appShellComponents.test.ts
npm run frontend:typecheck
git add -- apps/frontend/src/views/WorkspacePlaceholderView.vue apps/frontend/src/foundation.ts
git diff --cached --check
git commit -m "chore: remove obsolete workbench shell surface"
```

Expected: the shell group is independently green and committed; concrete router views and foundation health functions remain unchanged.

- [ ] **Step 3: Internalize the knowledge-document formatting helper**

In `apps/frontend/src/knowledge/documentPolicy.ts`, make only this declaration change:

```diff
-export function formatBytes(bytes: number): string {
+function formatBytes(bytes: number): string {
```

Run and commit:

```powershell
npm --workspace apps/frontend run test -- tests/knowledgeComponents.test.ts
npm run frontend:typecheck
git add -- apps/frontend/src/knowledge/documentPolicy.ts
git diff --cached --check
git commit -m "chore: internalize knowledge format helper"
```

Expected: upload validation behavior passes; `formatBytes` remains used internally but is no longer public.

- [ ] **Step 4: Internalize the Agent configuration draft request type**

In `apps/frontend/src/agentConfiguration/agentConfigurationClient.ts`, make only this declaration change:

```diff
-export interface CreateAgentDraftRequest {
+interface CreateAgentDraftRequest {
```

Run and commit:

```powershell
npm --workspace apps/frontend run test -- `
  tests/agentConfigurationClient.test.ts `
  tests/agentConfigurationStore.test.ts `
  tests/agentConfigurationView.test.ts
npm run frontend:typecheck
git add -- apps/frontend/src/agentConfiguration/agentConfigurationClient.ts
git diff --cached --check
git commit -m "chore: internalize agent draft request type"
```

Expected: the public `AgentConfigurationClient` remains structurally usable and all Agent configuration tests pass.

- [ ] **Step 5: Internalize the two legacy state snapshot types**

Make only these declaration changes:

```diff
// apps/frontend/src/authState.ts
-export interface AuthSnapshot {
+interface AuthSnapshot {

// apps/frontend/src/protectedDataState.ts
-export interface ProtectedDataSnapshot {
+interface ProtectedDataSnapshot {
```

Run and commit this tightly related state-layer group:

```powershell
npm --workspace apps/frontend run test -- tests/auth.test.ts tests/protectedData.test.ts
npm run frontend:typecheck
git add -- apps/frontend/src/authState.ts apps/frontend/src/protectedDataState.ts
git diff --cached --check
git commit -m "chore: internalize frontend state snapshots"
```

Expected: both state factories retain the same `snapshot()` return shape and all state behavior tests pass.

- [ ] **Step 6: Internalize the AIOps specialist projection types**

In `apps/frontend/src/stores/aiops.ts`, make only these two declaration changes:

```diff
-export type PublicSpecialistStatus =
+type PublicSpecialistStatus =

-export interface PublicSpecialistResult {
+interface PublicSpecialistResult {
```

Run and commit:

```powershell
npm --workspace apps/frontend run test -- `
  tests/aiopsStore.test.ts tests/investigationWorkspace.test.ts
npm run frontend:typecheck
git add -- apps/frontend/src/stores/aiops.ts
git diff --cached --check
git commit -m "chore: internalize aiops specialist projections"
```

Expected: the exported `PublicInvestigationResult` and projection function retain the same runtime/public shape.

- [ ] **Step 7: Internalize the asynchronous-status tone type**

In `apps/frontend/src/ui/asyncStatus.ts`, make only this declaration change:

```diff
-export type AsyncStatusTone =
+type AsyncStatusTone =
```

Run and commit:

```powershell
npm --workspace apps/frontend run test -- tests/chineseWorkspace.test.ts
npm run frontend:typecheck
git add -- apps/frontend/src/ui/asyncStatus.ts
git diff --cached --check
git commit -m "chore: internalize async status tone"
```

Expected: all user-visible Chinese status labels and tones remain unchanged.

- [ ] **Step 8: Prove the exact Knip issue set is gone**

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

- [ ] **Step 9: Run frontend/static gates and inspect the exact diff**

Run:

```powershell
npm run contracts:typecheck
npm run frontend:typecheck
npm run frontend:test
npm run frontend:build
git diff --check origin/main...HEAD
$changed = git diff --name-only origin/main...HEAD
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

Expected: all gates pass and exactly the eight reviewed paths differ from `origin/main`. `git status --short` is clean because each independently reviewable group has already been committed.

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
Push-Location apps/backend
uv run ruff check .
uv run pyright
Pop-Location
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: Knip produces no finding; frontend, shared-contract, Ruff, and strict Pyright checks pass; the worktree is clean and ahead of `origin/main` only by the reviewed commits. The PR's path-filtered remote run is expected to skip backend jobs, so run `32702871796` remains the recorded successful backend full-suite baseline.

- [ ] **Step 4: Prepare publication without performing unapproved remote operations**

Summarize in the PR body:

- Knip started with 1 unused file, 2 unused exports, and 5 unused exported-type groups, then finished with zero findings in the two workspaces.
- Vulture's four findings were required Protocol keyword parameters and were preserved.
- Deptry's seven dependency candidates were package-name, dynamic-import, framework-runtime, or CLI false positives and were preserved.
- Main run `32702871796` passed backend Ruff, strict Pyright, and the full offline pytest suite at baseline commit `ac68239`; final local Ruff/Pyright also passed.
- Runtime behavior, backend, contracts, manifests, locks, benchmarks, RAG cards, specs, infrastructure, CI, and GitHub settings were not changed.
- List the exact commands from Task 3 Step 3.

Do not push, create a PR, merge, delete a branch, or change GitHub settings until the user explicitly requests that external action.
