# Conversation Agent And Automatic Alert Closure Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Execute this plan inline in the primary
> agent. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the Conversation Agent and adaptive RAG query rewriting with the
updated automatic alert/verification closure on `main`, without weakening either path.

**Architecture:** Keep the automatic alert closure as the direct AIOps Single-Agent path,
and keep Conversation Chat Live as a separate user-entry adapter that can invoke the same
diagnostic runtime. Merge at shared repository, history, and CLI boundaries while preserving
path-specific orchestration and metrics. Linearize Conversation-owned Alembic revisions after
the automatic-closure head so existing PostgreSQL installations can upgrade safely.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, LangGraph,
pytest, Ruff, Pyright, Docker Compose.

## Global Constraints

- Do not add dependencies or external services.
- Preserve automatic closure as a direct AIOps flow; do not route it through query rewriting.
- Preserve Conversation Chat Live, `conversationMetrics`, and adaptive query rewrite metrics.
- Preserve automatic closure correlation, recovery, verification, and MTTR metrics.
- Keep the existing `main` migration revisions `202608220002` and `202608220003` unchanged.
- Reject the unsupported `chat + auto-closure` CLI combination explicitly; never silently route it.
- Run focused regression suites, not the full pytest suite.
- Do not run real LLM, CLS, or fault-injection Live evaluations in this integration.

---

### Task 1: Create the integration branch and expose merge conflicts

**Files:**
- Modify through merge: all files changed by `feat/conversation-aiops-copilot`

**Interfaces:**
- Consumes: `main` at `efb0fe2199d171231d4f7566f7d6bede5e542be0` and Conversation commit
  `5c38b82`
- Produces: an integration branch containing both histories and only expected conflicts

- [ ] **Step 1: Commit this plan on the Conversation branch**

  Run: `git add docs/superpowers/plans/2026-08-23-conversation-auto-alert-integration.md && git commit -m "docs: plan conversation alert integration"`

- [ ] **Step 2: Create an isolated integration branch from updated main**

  Run: `git switch -c integration/conversation-auto-alert main`

- [ ] **Step 3: Merge the Conversation branch without finalizing the commit**

  Run: `git merge --no-ff --no-commit feat/conversation-aiops-copilot`

  Expected: content conflicts only in `alert_ingestion/sqlalchemy.py` and
  `evaluation/live/cli.py`.

### Task 2: Resolve shared persistence and CLI semantics

**Files:**
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Verify: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Verify: `apps/backend/src/super_ai/evaluation/history.py`
- Verify: `apps/backend/src/super_ai/api/app.py`
- Test: `apps/backend/tests/test_live_benchmark_cli.py`
- Test: `apps/backend/tests/test_chat_aiops_live_cli.py`

**Interfaces:**
- Consumes: Conversation scheduling APIs and automatic-closure lifecycle APIs
- Produces: one repository implementing both contracts and one CLI supporting both entry paths

- [ ] **Step 1: Resolve the alert repository conflict**

  Preserve `list_active`, `get_owned`, and `schedule_for_incident`, plus correlation by
  `scenario_id`/`run_id`, `task_input_payload`, `get_live_lifecycle`, and
  `record_verification`. Keep verification state and report lookup in the same transaction
  boundaries used by `main`.

- [ ] **Step 2: Resolve the Live CLI conflict**

  Preserve `_run_live_command(..., enter_through_chat=True)`,
  `ChatEntryLiveDiagnosticAdapter`, and `conversationMetrics`. Preserve `--auto-closure`,
  resume/state orchestration, Prometheus/Alertmanager lifecycle, independent verification,
  and recovery closure. Ensure `--auto-closure` does not implicitly set
  `enter_through_chat=True`. Explicitly reject `enter_through_chat=True` together with
  `--auto-closure` as an invalid argument combination.

- [ ] **Step 3: Test the CLI entry-mode matrix**

  Cover Direct Live, Chat Live, Direct Auto-Closure, and Chat + Auto-Closure. Assert the first
  three select the intended path; assert the fourth is rejected before constructing a Chat
  adapter, query rewriter, or automatic-closure runner. Assert Auto-Closure remains Single-Agent.

- [ ] **Step 4: Verify the merged history contract**

  Ensure artifact schema v2 remains readable alongside legacy v1; preserve
  `conversation_model`, `conversationMetrics`, closure timing/verification metrics,
  correlation keys, `rewriteAppliedCount`, `rewriteModelCallCount`, `averageDurationMs`,
  `p95DurationMs`, and retrieval `corpusMetadata`.

- [ ] **Step 5: Run overlapping focused tests**

  Run from `apps/backend`:

  `uv run pytest tests/test_live_benchmark_cli.py tests/test_chat_aiops_live_cli.py tests/test_evaluation_history.py tests/test_alert_ingestion_service.py -q`

  Expected: all collected tests pass.

### Task 3: Linearize the Alembic migration chain

**Files:**
- Rename/Modify: `apps/backend/alembic/versions/202608220002_add_chat_agent_runs.py`
  to `202608220004_add_chat_agent_runs.py`
- Rename/Modify: `apps/backend/alembic/versions/202608220003_add_pending_chat_actions.py`
  to `202608220005_add_pending_chat_actions.py`
- Rename/Modify: `apps/backend/alembic/versions/202608220004_add_structured_chat_memory.py`
  to `202608220006_add_structured_chat_memory.py`
- Rename/Modify: `apps/backend/alembic/versions/202608220005_add_chat_memory_compaction_status.py`
  to `202608220007_add_chat_memory_compaction_status.py`
- Test: `apps/backend/tests/test_postgresql_migrations.py`
- Test: `apps/backend/tests/test_postgresql_alert_ingestion.py`
- Test: `apps/backend/tests/test_live_alert_verification_schema.py`

**Interfaces:**
- Consumes: automatic-closure head `202608220003`
- Produces: one Alembic head, `202608220007`, upgradeable from the current database state

- [ ] **Step 1: Rename Conversation migrations and update revision links**

  Set the chain to:

  `202608220003 -> 202608220004 -> 202608220005 -> 202608220006 -> 202608220007`.

- [ ] **Step 2: Inspect the configured database revision without mutation**

  Run from `apps/backend`: `uv run alembic current` and query `alembic_version` read-only.
  If it reports main `202608220003`, continue normally. If it reports an old Conversation
  revision `202608220002` through `202608220005`, do not stamp, rebuild, or claim compatibility;
  preserve it and use a disposable PostgreSQL database unless a data-preserving bridge is
  separately approved.

- [ ] **Step 3: Verify a single migration head**

  Run from `apps/backend`: `uv run alembic heads`

  Expected: exactly `202608220007 (head)`.

- [ ] **Step 4: Test both supported migration paths**

  Run `upgrade head` on a disposable fresh PostgreSQL database and on a disposable database
  first advanced through the unchanged main chain to `202608220003`.

  Expected: fresh → `202608220007` and main `202608220003` → `202608220007` both succeed.
  If the configured development database was confirmed as main `202608220003`, upgrade it
  normally and require `alembic current` to report `202608220007`.

- [ ] **Step 5: Run migration and repository contract tests**

  Run: `uv run pytest tests/test_postgresql_migrations.py tests/test_postgresql_alert_ingestion.py tests/test_live_alert_verification_schema.py -q`

  Expected: all collected tests pass. Repository coverage must exercise correlated apply,
  owner-scoped list/get, scheduling/reuse, lifecycle lookup, verification recording, rollback,
  tenant isolation, and preservation of `task_input_payload`.

### Task 4: Focused cross-feature regression

**Files:**
- Test: `apps/backend/tests/test_query_rewrite_benchmark.py`
- Test: `apps/backend/tests/test_live_auto_closure.py`
- Test: `apps/backend/tests/test_live_auto_closure_security.py`
- Test: `apps/backend/tests/test_live_order_pool_contracts.py`
- Test: `apps/backend/tests/test_live_order_api_service.py`
- Test: `apps/backend/tests/test_alertmanager_ingestion_parser.py`
- Test: Conversation Agent and memory tests selected from the committed feature suite
- Test: `packages/api-contracts` typecheck and tests
- Test: frontend typecheck, tests, and production build

**Interfaces:**
- Consumes: integrated repository, CLI, history, migrations, and query rewriting
- Produces: evidence that both feature paths retain their contracts

- [ ] **Step 1: Run automatic-closure regression tests**

  Run from `apps/backend`:

  `uv run pytest tests/test_live_auto_closure.py tests/test_live_auto_closure_security.py tests/test_live_order_pool_contracts.py tests/test_live_order_api_service.py tests/test_alertmanager_ingestion_parser.py tests/test_alert_ingestion_service.py -q`

  Expected: all collected tests pass.

- [ ] **Step 2: Run Conversation and query rewrite regression tests**

  Run:

  `uv run pytest tests/test_chat_aiops_bridge.py tests/test_chat_turn_execution.py tests/test_chat_sessions_api.py tests/test_chat_runs_repository.py tests/test_chat_runs_api.py tests/test_chat_react_budget.py tests/test_chat_query_rewrite.py tests/test_chat_memory_jobs.py tests/test_chat_memory.py tests/test_chat_live_entry_adapter.py tests/test_chat_intent_router.py tests/test_chat_execution_policy.py tests/test_chat_aiops_live_cli.py tests/test_conversation_model_eval.py tests/test_conversation_eval.py tests/test_structured_chat_memory.py tests/test_stream_rag_chat_api.py tests/test_query_rewrite_benchmark.py tests/test_pending_chat_actions.py tests/test_memory_repositories.py tests/test_memory_migrations.py tests/test_llm_provider.py -q`

  Expected: the prior 125 focused tests pass after integration.

- [ ] **Step 3: Verify API contracts and frontend integration**

  Run from the repository root: `npm run contracts:typecheck`,
  `npm --workspace packages/api-contracts run test`, `npm run frontend:typecheck`,
  `npm run frontend:test`, and `npm run frontend:build`.

  Expected: API contract tests/typecheck and frontend tests/typecheck/build all exit 0.

- [ ] **Step 4: Run static checks**

  Run from `apps/backend`: `uv run ruff check src tests` and `uv run pyright`.

  Expected: Ruff exits 0; Pyright reports 0 errors and 0 warnings.

- [ ] **Step 5: Render deployment configuration**

  Run the repository's existing Docker Compose config/render command without starting
  services.

  Expected: configuration renders successfully with no missing variable or schema error.

### Task 5: Finalize the integration commit

**Files:**
- Modify: `docs/superpowers/reports/2026-08-23-conversation-query-rewrite-retrieval-ab.md`
  only to remove Markdown trailing whitespace
- Create: `docs/superpowers/reports/2026-08-23-conversation-auto-alert-integration.md`
- Verify: all merged files

**Interfaces:**
- Consumes: verified integrated worktree
- Produces: one reviewable integration commit; no push or merge back to local `main`

- [ ] **Step 1: Remove whitespace warnings and inspect the merge**

  Run: `git diff --check` and `git status --short`.

  Expected: no whitespace errors and no unresolved files.

- [ ] **Step 2: Record the migration mapping and verification evidence**

  Document that historical Conversation revisions `202608220002–005` are superseded in the
  integrated chain by `202608220004–007`; record supported upgrade paths and explicitly note
  that databases stamped with the old Conversation-only revisions require a separately tested
  data-preserving bridge.

- [ ] **Step 3: Commit the merge**

  Run: `git commit` with message `merge: integrate conversation agent with alert closure`.

- [ ] **Step 4: Report evidence and request the next Git action**

  Report the integration commit, focused test/static/migration results, and any skipped real
  external evaluations. Do not push, open a PR, or merge into local `main` until explicitly
  requested.
