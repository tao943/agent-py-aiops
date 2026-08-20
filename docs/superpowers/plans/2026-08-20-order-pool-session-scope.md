# Order Pool Session Scope Truncation Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace overlong Order Pool PostgreSQL `application_name` values with a stable 51-byte run/generation scope so long Live Run IDs remain observable and recoverable.

**Architecture:** Keep the complete Run ID in HTTP, events, orders, CLS, and Evaluation Artifacts. Derive only the PostgreSQL session label from `SHA-256(run_id)[:16]` and `generation[:16]`; make order-api and the backend observer use the same deterministic format and verify parity with contract tests.

**Tech Stack:** Python 3.10+, hashlib, asyncpg/PostgreSQL 16, FastAPI order-api Docker fixture, pytest/pytest-asyncio, Ruff, Pyright.

## Global Constraints

- The session label is exactly `agentpy-order-api:<run_hash_16>:<generation_prefix_16>` and at most 51 ASCII bytes for valid inputs.
- Full Run IDs remain unchanged in HTTP paths, order rows, events, CLS, PostgreSQL evaluation history, and Evaluation Artifacts.
- `agentpy-order-api:idle` and unrelated-session exclusion semantics remain unchanged.
- Do not modify control-token authorization, recovery authorization, cleanup, scoring, Agent Workflow, RAG, CLS, or Artifact contracts.
- Do not add dependencies, services, configuration, database migrations, or external code.
- Use TDD; do not run full pytest.
- Rebuild only `live-eval-order-api`, run the real Docker Order Pool contract, then run one unique real Single canary. On failure, immediately Verify/Cleanup and stop without a second run.

---

## File Structure

- Modify `infra/live-eval/order_api.py`: derive the bounded session application name used by checked-out fault connections.
- Modify `apps/backend/src/super_ai/evaluation/live/order_pool_leak.py`: derive the same run/generation scope for run count, lock wait, generation verification, cleanup, and audit queries.
- Modify `apps/backend/tests/test_live_order_api_service.py`: validate exact length, deterministic parity inputs, and separation for different Run IDs/generations.
- Modify `apps/backend/tests/test_live_order_pool_contracts.py`: verify observer query arguments use the bounded format for a 64-character Run ID.
- Modify `docs/aiops/agentpy-domainbench.md`: record the real evidence, repair verification, and one canary outcome.

### Task 1: Implement and Contract-Test the Bounded Session Scope

**Files:**
- Modify: `infra/live-eval/order_api.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/order_pool_leak.py`
- Test: `apps/backend/tests/test_live_order_api_service.py`
- Test: `apps/backend/tests/test_live_order_pool_contracts.py`

**Interfaces:**
- Consumes: validated ASCII `run_id: str` and order-api `generation: str`.
- Produces in both runtime modules: `_session_application_name(run_id: str, generation: str) -> str`; backend also produces `_session_run_pattern(run_id: str) -> str` for run-scoped LIKE queries.

- [ ] **Step 1: Write RED order-api length and separation tests**

Add tests that load the standalone order-api module and assert the wished-for contract:

```python
def test_fault_session_application_name_is_bounded_for_maximum_run_id() -> None:
    module = _load_order_api()
    run_id = "r" * 64
    generation = "0123456789abcdef" * 2

    application_name = module._session_application_name(run_id, generation)

    assert application_name == "agentpy-order-api:c9ea6f42c8efcb14:0123456789abcdef"
    assert len(application_name.encode("ascii")) == 51
    assert len(application_name.encode("ascii")) <= 63


def test_fault_session_application_name_separates_runs_and_generations() -> None:
    module = _load_order_api()
    assert module._session_application_name("run-1", "generation-a") != (
        module._session_application_name("run-2", "generation-a")
    )
    assert module._session_application_name("run-1", "generation-a") != (
        module._session_application_name("run-1", "generation-b")
    )
```

The expected hash literal is fixed and must not be calculated with the production helper inside the assertion.

- [ ] **Step 2: Write RED observer query tests**

Create a recording async connection/config boundary in `test_live_order_pool_contracts.py`. For `run_id = "r" * 64` and a 32-character generation, call `run_scoped_session_count`, `lock_wait_observed`, and `generation_session_count`; assert the SQL arguments are exactly:

```python
expected_run_pattern = "agentpy-order-api:c9ea6f42c8efcb14:%"
expected_generation_name = (
    "agentpy-order-api:c9ea6f42c8efcb14:0123456789abcdef"
)
```

Also assert no query argument contains the complete 64-character Run ID and the generation name is at most 63 ASCII bytes.

- [ ] **Step 3: Run focused tests and confirm RED**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py -q -p no:cacheprovider
```

Expected: tests fail because `_session_application_name`/`_session_run_pattern` do not exist and current query arguments use the full Run ID.

- [ ] **Step 4: Implement the minimal shared algorithm in both runtime boundaries**

In `infra/live-eval/order_api.py`, import `hashlib`, replace `_app_name`, and use:

```python
_SESSION_PREFIX = "agentpy-order-api"
_SESSION_TOKEN_LENGTH = 16


def _session_application_name(run_id: str, generation: str) -> str:
    run_scope = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:_SESSION_TOKEN_LENGTH]
    generation_scope = generation[:_SESSION_TOKEN_LENGTH]
    return f"{_SESSION_PREFIX}:{run_scope}:{generation_scope}"
```

Update `execute_fault()` to call `_session_application_name`.

In `order_pool_leak.py`, reuse the existing `hashlib` import and define the same constants plus:

```python
def _session_run_scope(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:_SESSION_TOKEN_LENGTH]


def _session_run_pattern(run_id: str) -> str:
    return f"{_SESSION_PREFIX}:{_session_run_scope(run_id)}:%"


def _session_application_name(run_id: str, generation: str) -> str:
    return (
        f"{_SESSION_PREFIX}:{_session_run_scope(run_id)}:"
        f"{generation[:_SESSION_TOKEN_LENGTH]}"
    )
```

Use `_session_run_pattern` in `run_scoped_session_count` and `lock_wait_observed`; use `_session_application_name` in `generation_session_count`. Keep unrelated-session SQL as `application_name NOT LIKE 'agentpy-order-api:%'` and idle pool settings unchanged.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the Step 3 command.

Expected: all selected tests pass.

- [ ] **Step 6: Run targeted static checks**

```powershell
uv run ruff check ../../infra/live-eval/order_api.py src/super_ai/evaluation/live/order_pool_leak.py tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py
uv run pyright ../../infra/live-eval/order_api.py src/super_ai/evaluation/live/order_pool_leak.py tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py
```

Expected: Ruff passes; Pyright reports zero errors.

- [ ] **Step 7: Commit Task 1**

```powershell
git add infra/live-eval/order_api.py apps/backend/src/super_ai/evaluation/live/order_pool_leak.py apps/backend/tests/test_live_order_api_service.py apps/backend/tests/test_live_order_pool_contracts.py
git commit -m "fix: bound order pool session scope"
```

### Task 2: Rebuild, Verify the Real Docker Contract, and Run One Canary

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Verify: Task 1 runtime and tests

**Interfaces:**
- Consumes: the bounded session scope implementation and existing real ignored project configuration.
- Produces: a rebuilt healthy order-api image, one persisted unique Single canary terminal result, cleanup evidence, and a documented outcome.

- [ ] **Step 1: Re-run the complete bounded regression**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py tests/test_live_order_pool_docker.py::test_real_order_pool_leak_recovery_and_idempotent_cleanup -q -p no:cacheprovider -m live_docker
```

Expected before the Docker test: rebuild is required; do not accept a pass from a stale container image.

- [ ] **Step 2: Rebuild only order-api and run the real Docker contract**

```powershell
cd ../..
docker compose -f infra/compose.yaml up -d --build live-eval-order-api
cd apps/backend
uv run pytest tests/test_live_order_pool_docker.py::test_real_order_pool_leak_recovery_and_idempotent_cleanup -o addopts='' -q -p no:cacheprovider -m live_docker
```

Expected: `1 passed`; order-api and PostgreSQL are healthy; cleanup audit is clean.

- [ ] **Step 3: Recheck real prerequisites and preflight one unique Run**

Require Docker health, RAG 30 documents/180 chunks/0 mismatch, configured models unchanged, and absence of the generated Run ID in both PostgreSQL and Evaluation Archive. Record the immutable checksum/status of `order-pool-diagnostics-single-20260820-225508` before the new run.

- [ ] **Step 4: Run exactly one real Single canary**

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runId = "order-pool-bounded-single-$stamp"
$campaignId = "order-pool-bounded-$stamp"
$config = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\config\project.json'
uv run python scripts/run_live_benchmark.py run --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id $runId --owner-user-id user_c88807ff36b74a038b9e1ea31a389cfc --knowledge-base-id kb_user_c88807ff36b74a038b9e1ea31a389cfc --evidence-source cls --strategy single --campaign-id $campaignId --config $config
```

Expected injection boundary: all six checks pass and `runScopedSessionCount >= 3`, allowing the Agent/LLM phase to begin. Final success still requires `VALID_PASS`, requested Single/effective non-Multi, `verificationPassed=true`, `cleanupSucceeded=true`, and `securityHardGatePassed=true`.

On any failure, immediately run scoped Verify then Cleanup, persist the result, compare the old Run fingerprint, and stop without a second canary.

- [ ] **Step 5: Record the exact result and commit**

Update DomainBench with the previous failed Run evidence, bounded format, target/Docker verification, exact new Run ID/Git SHA/safe outcome/checks, persistence checksum, and cleanup state. Do not include credentials, raw CLS logs, Prompt, Oracle, or model responses.

```powershell
git diff --check
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record bounded order pool canary"
git status --short --branch
```

Expected: worktree is clean; no push, PR, merge, full pytest, or second canary occurred.

## Self-Review

- Spec coverage: exact 51-byte format, full Run ID preservation, idle/unrelated compatibility, long-ID regression, query parity, static checks, rebuild, Docker contract, one canary, failure cleanup, and documentation each map to a step.
- Type consistency: both runtime modules expose `_session_application_name(run_id: str, generation: str) -> str`; backend alone exposes `_session_run_pattern(run_id: str) -> str`.
- Scope: no dependency, configuration, migration, authorization, Agent, RAG, CLS, scoring, or Artifact behavior change is included.
- Placeholder scan: runtime IDs are generated by the exact PowerShell commands in Task 2; all file paths, code interfaces, and test assertions are concrete.
