# AgentPy Benchmark Production Failure Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AgentPy Snapshot benchmark runs concurrency-safe, atomically finalized, explicitly failed, safely serialized by the CLI, and regression-protected against ground-truth access.

**Architecture:** Keep PostgreSQL authoritative and implement idempotency inside the existing SQLAlchemy repository using PostgreSQL `ON CONFLICT DO NOTHING` plus row locks. Add explicit failure/finalize operations to the evaluation persistence facade, make the Runner classify each production boundary, and move pure CLI contracts into an importable module. Preserve the evaluator-only oracle boundary and run all new coverage offline or against the existing PostgreSQL fixture.

**Tech Stack:** Python 3.10+, SQLAlchemy 2 async, PostgreSQL 16, Alembic, pytest, pytest-asyncio, existing Snapshot MCP and evaluation modules; no new dependency.

## Global Constraints

- PostgreSQL is the sole source of evaluation state and scorecard truth; Redis must not affect correctness.
- Default tests must not call DashScope, CLS, Alertmanager, Milvus, Docker API, or real services.
- Persist only allowlisted failure categories; never persist exception text, traceback, API keys, tokens, passwords, ground truth, or private Chain-of-Thought.
- Valid terminal statuses are `completed`, `agent_failed`, and `infra_failed`; a created failed run must not remain `pending`.
- The Runner must use one transaction to write a scorecard and transition its run to `completed`.
- Do not add CLI automatic retry, `retry_of_run_id`, Live fault execution, recovery execution, Judge, or new dependencies.
- Follow red-green-refactor and commit each independently reviewable task.

---

## File Structure

Create:

```text
apps/backend/alembic/versions/202608110001_add_evaluation_failure_category.py
apps/backend/src/super_ai/evaluation/cli.py
apps/backend/tests/test_evaluation_cli.py
```

Modify:

```text
apps/backend/src/super_ai/memory/models.py
apps/backend/src/super_ai/memory/repositories.py
apps/backend/src/super_ai/memory/sqlalchemy.py
apps/backend/src/super_ai/evaluation/persistence.py
apps/backend/src/super_ai/evaluation/runner.py
apps/backend/src/super_ai/evaluation/scenarios.py
apps/backend/src/super_ai/evaluation/__init__.py
apps/backend/scripts/run_snapshot_benchmark.py
apps/backend/tests/test_evaluation_persistence.py
apps/backend/tests/test_postgresql_migrations.py
apps/backend/tests/test_snapshot_benchmark_runner.py
apps/backend/tests/test_evaluation_scenarios.py
apps/backend/tests/test_snapshot_evaluation_tools.py
apps/backend/tests/test_evaluation_scoring.py
openspec/changes/add-agentpy-sre-benchmark/specs/agentpy-sre-benchmark/spec.md
openspec/changes/add-agentpy-sre-benchmark/tasks.md
```

---

### Task 1: Specify and persist explicit failure terminal states

**Files:**
- Create: `apps/backend/alembic/versions/202608110001_add_evaluation_failure_category.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/evaluation/persistence.py`
- Modify: `apps/backend/tests/test_postgresql_migrations.py`
- Modify: `apps/backend/tests/test_evaluation_persistence.py`
- Modify: `openspec/changes/add-agentpy-sre-benchmark/specs/agentpy-sre-benchmark/spec.md`
- Modify: `openspec/changes/add-agentpy-sre-benchmark/tasks.md`

**Interfaces:**
- Produces: `EvaluationRunRecord.failure_category: str | None`.
- Produces: `EvaluationRepository.fail_run(*, run_id: str, status: Literal["agent_failed", "infra_failed"], failure_category: str) -> EvaluationRunRecord`.
- Consumes: existing `EvaluationRunModel`, SQLAlchemy session factory, and Alembic head `202608100001`.

- [x] **Step 1: Add OpenSpec failure-state requirements and task checklist**

Add scenarios requiring `pending -> agent_failed|infra_failed`, allowlisted safe categories, idempotent repeated failure, and rejection of conflicting terminal transitions. Add implementation checklist entries for persistence, concurrency, Runner, CLI, isolation, and final verification.

- [x] **Step 2: Write failing migration and repository tests**

Add JSON/schema assertions and tests shaped as:

```python
async def test_failed_run_persists_only_safe_terminal_metadata(
    migrated_database_url: str,
) -> None:
    repository = evaluation_repository(migrated_database_url)
    await create_test_run(repository, "run-failed")

    failed = await repository.fail_run(
        run_id="run-failed",
        status="agent_failed",
        failure_category="adapter_error",
    )
    repeated = await repository.fail_run(
        run_id="run-failed",
        status="agent_failed",
        failure_category="adapter_error",
    )

    assert failed == repeated
    assert failed.status == "agent_failed"
    assert failed.failure_category == "adapter_error"
    assert "secret" not in repr(failed)
```

Also assert unknown categories, `pending`, and conflicting failure transitions raise `ValueError`.

- [x] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
cd apps/backend
uv run pytest tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py -q
```

Expected: FAIL because `failure_category`, the new migration, and `fail_run` do not exist.

- [x] **Step 4: Add the migration, model/record field, and fail_run contract**

Create revision `202608110001` with `down_revision = "202608100001"` and add nullable
`failure_category VARCHAR(80)`. Map it through `EvaluationRunModel` and
`EvaluationRunRecord`.

In the facade validate exact allowlists:

```python
FailureStatus = Literal["agent_failed", "infra_failed"]
FAILURE_CATEGORIES = frozenset({
    "adapter_error",
    "artifact_invalid",
    "scenario_error",
    "evaluation_error",
    "persistence_error",
})
```

In SQLAlchemy lock the run with `SELECT ... FOR UPDATE`; update only `pending`, return an
identical terminal transition, and reject a different terminal status/category.

- [x] **Step 5: Verify GREEN and static checks**

Run:

```powershell
uv run pytest tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py -q
uv run ruff check src/super_ai/evaluation/persistence.py src/super_ai/memory tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py alembic/versions/202608110001_add_evaluation_failure_category.py
uv run pyright src/super_ai/evaluation/persistence.py src/super_ai/memory tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py
```

Expected: PASS and zero type errors.

- [x] **Step 6: Commit**

```powershell
git add apps/backend/alembic apps/backend/src/super_ai/memory apps/backend/src/super_ai/evaluation/persistence.py apps/backend/tests/test_evaluation_persistence.py apps/backend/tests/test_postgresql_migrations.py openspec/changes/add-agentpy-sre-benchmark
git commit -m "feat: persist benchmark failure terminal states"
```

---

### Task 2: Make run creation concurrent-safe and score finalization atomic

**Files:**
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/evaluation/persistence.py`
- Modify: `apps/backend/tests/test_evaluation_persistence.py`

**Interfaces:**
- Produces: concurrent-safe `EvaluationRepository.create_run(...)`.
- Produces: `EvaluationRepository.finalize_run(*, run_id: str, result_id: str, result: EvaluationResult, diagnostic_task_id: str | None) -> tuple[EvaluationRunRecord, EvaluationResultRecord]`.
- Consumes: failure states from Task 1 and existing `EvaluationResult` serialization.

- [x] **Step 1: Write failing concurrency and recovery tests**

Use two repository instances backed by the same engine:

```python
first, second = await asyncio.gather(
    first_repository.create_run(**same_identity),
    second_repository.create_run(**same_identity),
)
assert first == second
```

Add a different-identity race using `asyncio.gather(..., return_exceptions=True)` and assert
exactly one stable business conflict. Immediately create/read another run with the losing
repository to prove the unique conflict did not poison its next session.

- [x] **Step 2: Write failing atomic finalize and duplicate-result tests**

Test:

```python
run, result = await repository.finalize_run(
    run_id="run-finalize",
    result_id="result-finalize",
    result=passing_result(),
    diagnostic_task_id=None,
)
repeated = await repository.finalize_run(
    run_id="run-finalize",
    result_id="result-finalize",
    result=passing_result(),
    diagnostic_task_id=None,
)
assert repeated == (run, result)
```

Then pass a different result ID/content and assert rejection. Inject a non-JSON score reason into
the raw SQLAlchemy method, assert flush fails, and verify the run remains `pending` with no result.

- [x] **Step 3: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_evaluation_persistence.py -q`

Expected: concurrent creation exposes an `IntegrityError` and `finalize_run` is missing.

- [x] **Step 4: Implement PostgreSQL conflict-safe creation**

Use the existing dependency only:

```python
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

statement = (
    postgresql_insert(EvaluationRunModel)
    .values(...)
    .on_conflict_do_nothing(index_elements=[EvaluationRunModel.run_id])
)
await session.execute(statement)
await session.commit()
row = await session.get(EvaluationRunModel, run_id)
```

Compare the complete immutable identity after selection. Never catch and expose the original
database exception as the normal concurrency path.

- [x] **Step 5: Implement one-transaction finalize_run**

Lock the run row, require `pending` or an equivalent already-completed result, add the scorecard,
set status/diagnostic task/timestamps, flush, and commit once. Compare persisted score fields for
idempotent repeats; reject any mismatch. Roll back automatically on flush/commit failure.

- [x] **Step 6: Verify GREEN and static checks**

Run:

```powershell
uv run pytest tests/test_evaluation_persistence.py -q
uv run ruff check src/super_ai/evaluation/persistence.py src/super_ai/memory tests/test_evaluation_persistence.py
uv run pyright src/super_ai/evaluation/persistence.py src/super_ai/memory tests/test_evaluation_persistence.py
```

Expected: PASS and zero static errors.

- [x] **Step 7: Commit**

```powershell
git add apps/backend/src/super_ai/memory apps/backend/src/super_ai/evaluation/persistence.py apps/backend/tests/test_evaluation_persistence.py
git commit -m "feat: make benchmark persistence concurrency safe"
```

---

### Task 3: Classify Runner production failures and remove half-complete runs

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`

**Interfaces:**
- Produces: `BenchmarkRunError(status: FailureStatus, category: str)` with a fixed safe message.
- Consumes: `EvaluationPersistence.fail_run` and `finalize_run` from Tasks 1–2.
- Changes: `SnapshotBenchmarkRunner.run` uses atomic finalize instead of `complete_run` + `save_result`.

- [x] **Step 1: Extend the fake persistence and write failing adapter/artifact tests**

Make `RecordingPersistence` record `failed` and `finalized` calls. Add:

```python
with pytest.raises(BenchmarkRunError) as captured:
    await runner_with_raising_adapter().run("APY-003", agent_version=version)
assert captured.value.status == "agent_failed"
assert captured.value.category == "adapter_error"
assert persistence.failed[-1][1:] == ("agent_failed", "adapter_error")
assert not persistence.finalized
```

Repeat for wrong scenario/mode as `artifact_invalid`.

- [x] **Step 2: Write failing evaluator/finalize tests**

Inject evaluator and persistence failure boundaries into the Runner constructor using small
callable protocols/defaults. Assert evaluator failure records `infra_failed/evaluation_error` and
finalize failure attempts `infra_failed/persistence_error`. The raised error message must not
contain an injected sentinel secret.

- [x] **Step 3: Run tests and verify RED**

Run: `uv run pytest tests/test_snapshot_benchmark_runner.py -q`

Expected: FAIL because Runner still leaks raw exceptions, leaves pending runs, and calls two-step
completion.

- [x] **Step 4: Implement stage-specific safe failure handling**

Define fixed `BenchmarkRunError`, evaluator injection defaults to `load_scenario_oracle` plus
`score_run`, and a `_record_failure` helper. Suppress external exception text while retaining
exception chaining for local debugging. Classify only by the current stage, not by parsing messages.

Call `finalize_run` on success. If failure persistence itself fails, raise a fixed
`BenchmarkRunError("infra_failed", "persistence_error")`; never report that the failure was stored.

- [x] **Step 5: Verify GREEN and related regressions**

Run:

```powershell
uv run pytest tests/test_snapshot_benchmark_runner.py tests/test_evaluation_scoring.py tests/test_evaluation_persistence.py -q
uv run ruff check src/super_ai/evaluation/runner.py tests/test_snapshot_benchmark_runner.py
uv run pyright src/super_ai/evaluation/runner.py tests/test_snapshot_benchmark_runner.py
```

Expected: PASS and zero static errors.

- [x] **Step 6: Commit**

```powershell
git add apps/backend/src/super_ai/evaluation/runner.py apps/backend/tests/test_snapshot_benchmark_runner.py
git commit -m "feat: classify benchmark runner failures"
```

---

### Task 4: Harden CLI contracts and ground-truth isolation

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/cli.py`
- Create: `apps/backend/tests/test_evaluation_cli.py`
- Modify: `apps/backend/scripts/run_snapshot_benchmark.py`
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`
- Modify: `apps/backend/src/super_ai/evaluation/scenarios.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_snapshot_evaluation_tools.py`
- Modify: `apps/backend/tests/test_evaluation_scoring.py`

**Interfaces:**
- Produces: `evaluation_exit_code(results: Sequence[Mapping[str, object]]) -> int`.
- Produces: `evaluation_result_payload(...) -> dict[str, object]`.
- Produces: `safe_failure_payload(error: BaseException) -> dict[str, object]`.
- Consumes: `BenchmarkRunError` from Task 3.

- [ ] **Step 1: Write failing pure CLI contract tests**

Cover pass/fail/invalid as 0/1/2 and fixed safe serialization:

```python
error = RuntimeError("api-key-secret ground_truth C:\\private\\oracle.yaml")
payload = safe_failure_payload(error)
serialized = json.dumps(payload)
assert payload["validity"] == "invalid"
assert payload["category"] == "infrastructure_error"
assert "api-key-secret" not in serialized
assert "ground_truth" not in serialized
assert "C:\\private" not in serialized
```

For `BenchmarkRunError`, assert its allowlisted category is preserved without its cause text.

- [ ] **Step 2: Write failing path and nested-answer tests**

Parametrize Runner inputs `../APY-003`, `..\\APY-003`, `/tmp/APY-003`, and
`C:\\tmp\\APY-003`; assert rejection occurs before persistence `create_run`.

Parametrize deeply nested normalized variants of `oracle`, `primary_cause`,
`required_evidence`, and `required-rule-outs`; assert `load_public_scenario` rejects all.

- [ ] **Step 3: Write failing ReadGroundTruth/application boundary tests**

Assert Snapshot discovery excludes `ReadGroundTruth` and direct calls raise `McpClientError` without
an observation. Assert the application diagnostic input serialized from `PublicScenario` excludes
`ground_truth`, oracle mechanism, and trigger. Preserve the existing scorer test proving a malicious
persisted `ReadGroundTruth` audit yields `validity == "invalid"` and
`hard_gate == "ground_truth_access"`.

- [ ] **Step 4: Run tests and verify RED**

Run:

```powershell
uv run pytest tests/test_evaluation_cli.py tests/test_evaluation_scenarios.py tests/test_snapshot_evaluation_tools.py tests/test_snapshot_benchmark_runner.py tests/test_evaluation_scoring.py -q
```

Expected: FAIL because `evaluation.cli` is absent and the complete isolation matrix is not covered.

- [ ] **Step 5: Implement the pure CLI module and thin script delegation**

Move result payload/exit-code logic to `super_ai.evaluation.cli`. Implement
`safe_failure_payload` with fixed text and allowlisted `BenchmarkRunError.category`; all other
exceptions map to `infrastructure_error`. Keep dependency construction and output writing in the
script.

- [ ] **Step 6: Harden normalized answer keys and application input construction**

Normalize public YAML keys with lowercase plus hyphen/space-to-underscore conversion before the
recursive forbidden-key check. Extract application diagnostic input construction into a focused
function that consumes only `PublicScenario`; do not accept oracle or filesystem paths.

- [ ] **Step 7: Verify GREEN and the focused suite**

Run:

```powershell
uv run pytest tests/test_evaluation_cli.py tests/test_evaluation_scenarios.py tests/test_snapshot_evaluation_tools.py tests/test_snapshot_benchmark_runner.py tests/test_evaluation_scoring.py -q
uv run python scripts/run_snapshot_benchmark.py --help
uv run ruff check src tests scripts
uv run pyright
```

Expected: all focused tests pass, CLI help exits 0, and static checks report zero errors.

- [ ] **Step 8: Commit**

```powershell
git add apps/backend/src/super_ai/evaluation apps/backend/scripts/run_snapshot_benchmark.py apps/backend/tests
git commit -m "test: harden benchmark failure and isolation contracts"
```

---

### Task 5: Full verification and PR update

**Files:**
- Modify: `openspec/changes/add-agentpy-sre-benchmark/tasks.md`

**Interfaces:**
- Consumes: all production failure-path work from Tasks 1–4.
- Produces: fresh CI-equivalent verification evidence and an updated remote Draft PR.

- [ ] **Step 1: Run the complete offline backend lane once**

Run from `apps/backend`:

```powershell
uv sync --frozen
uv run ruff check .
uv run pyright
uv run pytest
```

Expected: exit 0; `live_llm` remains deselected by default.

- [ ] **Step 2: Validate OpenSpec strictly**

Run from repository root:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-agentpy-sre-benchmark --strict
```

Expected: `Change 'add-agentpy-sre-benchmark' is valid`.

- [ ] **Step 3: Mark the new OpenSpec checklist complete and revalidate**

Change only the production-failure checklist entries proven by Steps 1–2 to `[x]`, then rerun strict
validation.

- [ ] **Step 4: Commit final task state**

```powershell
git add openspec/changes/add-agentpy-sre-benchmark/tasks.md
git commit -m "docs: complete benchmark failure hardening tasks"
```

- [ ] **Step 5: Push the existing branch and verify Draft PR checks start**

```powershell
git push origin feat/nginx-rate-limit-gateway
gh pr view 3 --repo tao943/agent-py-aiops --json url,isDraft,state,statusCheckRollup
```

Expected: PR #3 remains open/draft and new CI checks are queued or running for the pushed head.
