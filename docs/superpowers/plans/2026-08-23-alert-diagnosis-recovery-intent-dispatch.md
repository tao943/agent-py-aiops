# Alert-triggered Recovery Intent Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect successful Alertmanager-created diagnostics to the existing governed `RecoveryIntent` and `production_recovery` worker without granting recovery authority to manual or Chat diagnostics.

**Architecture:** Alert ingestion stamps an unforgeable server-side source marker on the diagnostic task. A small dispatcher checks that marker and terminal diagnostic state, delegates all proposal and policy decisions to the existing `RecoveryIntentService`, and returns a safe classification. The durable `aiops_diagnosis` handler invokes the dispatcher after diagnosis and, on retry, skips the already-completed Agent run and only compensates the Intent dispatch.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async ORM, PostgreSQL, existing Background Job Runtime, pytest/pytest-asyncio, Ruff, Pyright, OpenSpec CLI.

## Global Constraints

- Only diagnostics created by Alertmanager ingestion may auto-dispatch a Recovery Intent.
- Manual API and Chat-created diagnostics must never auto-dispatch recovery.
- Reuse `RecoveryIntentService`, `production_recovery` worker, PostgreSQL uniqueness, and Background Job events; add no dependency or external service.
- Compose may become `queued` only when existing global and target policy permits it; PostgreSQL remains `awaiting_approval`.
- Never expose credentials, DSN, PID, SQL, absolute Compose paths, stdout/stderr, raw exceptions, benchmark ground truth, or trusted snapshots in public events.
- Do not add production write tools to LangGraph, Chat, MCP, or the frontend.
- Use TDD and run only focused pytest suites, focused Ruff, strict Pyright, and strict OpenSpec validation; do not run full pytest.
- Continue using JSON project configuration; do not introduce `.env` configuration.

## File Structure

- Modify `openspec/changes/add-production-recovery-execution/specs/production-recovery/spec.md`: specify alert-triggered automatic dispatch, source isolation, retry compensation, and safe audit requirements.
- Modify `openspec/changes/add-production-recovery-execution/design.md`: record why dispatch remains inside the durable diagnosis Job and outside LangGraph.
- Modify `openspec/changes/add-production-recovery-execution/tasks.md`: add traceable dispatch implementation and acceptance tasks.
- Modify `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`: stamp `triggerSource=alertmanager` only on webhook-ingested diagnostic tasks.
- Create `apps/backend/src/super_ai/recovery/auto_dispatch.py`: source eligibility, terminal-state eligibility, delegation, and safe result/event projection.
- Modify `apps/backend/src/super_ai/api/app.py`: compose the dispatcher and invoke it from the durable diagnosis handler with completed-task retry compensation.
- Modify `apps/backend/tests/test_postgresql_alert_ingestion.py`: prove the server marker is applied and client payload cannot override it.
- Modify `apps/backend/tests/test_chat_aiops_bridge.py`: prove manual incident scheduling remains unmarked.
- Create `apps/backend/tests/test_recovery_auto_dispatch.py`: unit-test every dispatcher outcome and safe event serialization.
- Create `apps/backend/tests/test_aiops_recovery_dispatch_job.py`: test first-run dispatch, skipped manual dispatch, crash/retry compensation, and event safety.
- Modify `apps/backend/tests/live/test_production_compose_recovery.py`: verify the full Alertmanager-to-RecoveryIntent-to-recovered chain and idempotent redelivery.
- Modify `apps/backend/tests/test_postgres_recovery_integration.py`: prove alert-triggered PostgreSQL proposals stop at approval with no recovery Job or executor call.

---

### Task 1: Specify and persist the trusted Alertmanager source

**Files:**
- Modify: `openspec/changes/add-production-recovery-execution/specs/production-recovery/spec.md`
- Modify: `openspec/changes/add-production-recovery-execution/design.md`
- Modify: `openspec/changes/add-production-recovery-execution/tasks.md`
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Test: `apps/backend/tests/test_postgresql_alert_ingestion.py`
- Test: `apps/backend/tests/test_chat_aiops_bridge.py`

**Interfaces:**
- Consumes: `IngestionWrite.task_input_payload: Mapping[str, object] | None` and existing `DiagnosticTaskModel.input_payload` JSON.
- Produces: the exact trusted task field `input_payload["triggerSource"] == "alertmanager"` for webhook-created tasks only.

- [ ] **Step 1: Add failing source-isolation tests**

Add PostgreSQL assertions that a new Alertmanager incident persists this top-level payload even if the incoming task payload contains `"triggerSource": "manual"`:

```python
assert task.input_payload["triggerSource"] == "alertmanager"
```

Add or extend the manual `schedule_for_incident()` test:

```python
task = await repositories.diagnostics.get_task(
    owner_user_id="owner-1", task_id=result.diagnostic_task_id
)
assert task is not None
assert "triggerSource" not in task.input_payload
```

- [ ] **Step 2: Run the focused tests and verify the trusted-source assertion fails**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_chat_aiops_bridge.py -q
```

Expected: the new Alertmanager marker assertion fails while the manual-source assertion already passes.

- [ ] **Step 3: Add OpenSpec requirements and the minimal server-side merge**

Add scenarios requiring webhook ingestion to override the source marker and requiring manual/Chat tasks to remain unmarked. In `_create_or_update_duplicate()`, build the payload from a copied mapping and force the trusted value last:

```python
task_input_payload = dict(
    write.task_input_payload
    or {"query": write.query, "alert": write.safe_alert}
)
task_input_payload["triggerSource"] = "alertmanager"
```

Pass `task_input_payload` to `DiagnosticTaskModel`. Do not change `schedule_for_incident()`.

- [ ] **Step 4: Re-run focused tests and OpenSpec validation**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_chat_aiops_bridge.py -q
openspec validate add-production-recovery-execution --strict
```

Expected: focused tests pass and OpenSpec reports the change valid.

- [ ] **Step 5: Commit the trusted-source slice**

```powershell
git add openspec/changes/add-production-recovery-execution apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_chat_aiops_bridge.py
git commit -m "feat: mark alert-triggered diagnostics"
```

### Task 2: Classify and dispatch governed Recovery Intents

**Files:**
- Create: `apps/backend/src/super_ai/recovery/auto_dispatch.py`
- Create: `apps/backend/tests/test_recovery_auto_dispatch.py`

**Interfaces:**
- Consumes: `DiagnosticMemoryRepository.get_task()` and `RecoveryIntentService.create_result(owner_user_id, diagnostic_task_id, note=None)`.
- Produces: `AutoRecoveryIntentDispatcher.dispatch(*, owner_user_id: str, diagnostic_task_id: str) -> AutoRecoveryDispatchResult` and `AutoRecoveryDispatchResult.public_event() -> dict[str, object]`.

- [ ] **Step 1: Write failing dispatcher contract tests**

Cover these exact outcomes:

```python
assert result.outcome == "skipped"
assert result.reason_code == "not_alert_triggered"
assert intent_service.calls == []

assert incomplete.reason_code == "diagnostic_not_succeeded"
assert unavailable.reason_code == "task_unavailable"
assert ineligible.reason_code == "proposal_not_eligible"
assert created.outcome == "created"
assert reused.outcome == "reused"
```

Recursively flatten `public_event()` keys and values and assert it contains only `type`, `outcome`, `reasonCode`, `intentId`, and `status`, with no trusted snapshot or exception material.

- [ ] **Step 2: Run the dispatcher tests and verify import failure**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_recovery_auto_dispatch.py -q
```

Expected: collection fails because `super_ai.recovery.auto_dispatch` does not exist.

- [ ] **Step 3: Implement immutable result types and minimal dispatch logic**

Create these public types and keep all policy decisions delegated:

```python
DispatchOutcome = Literal["created", "reused", "skipped"]
DispatchReason = Literal[
    "task_unavailable",
    "not_alert_triggered",
    "diagnostic_not_succeeded",
    "proposal_not_eligible",
]

@dataclass(frozen=True, slots=True)
class AutoRecoveryDispatchResult:
    outcome: DispatchOutcome
    reason_code: DispatchReason | None = None
    intent_id: str | None = None
    status: str | None = None

    def public_event(self) -> dict[str, object]:
        return {
            "type": "recovery.intent.dispatch",
            "outcome": self.outcome,
            "reasonCode": self.reason_code,
            "intentId": self.intent_id,
            "status": self.status,
        }
```

`dispatch()` must fetch the owner-scoped task, require exact source and `succeeded`, call `create_result(..., note=None)`, map `RecoveryIntentNotEligible` to a skip, and map `RecoveryIntentCreateResult.reused` to `reused`; every other exception must propagate for durable Job retry.

- [ ] **Step 4: Run dispatcher tests and focused static checks**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_recovery_auto_dispatch.py -q
uv run --project apps/backend ruff check apps/backend/src/super_ai/recovery/auto_dispatch.py apps/backend/tests/test_recovery_auto_dispatch.py
uv run --project apps/backend pyright apps/backend/src/super_ai/recovery/auto_dispatch.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit the dispatcher slice**

```powershell
git add apps/backend/src/super_ai/recovery/auto_dispatch.py apps/backend/tests/test_recovery_auto_dispatch.py
git commit -m "feat: dispatch governed recovery intents"
```

### Task 3: Connect durable diagnosis completion and crash compensation

**Files:**
- Modify: `apps/backend/src/super_ai/api/app.py`
- Create: `apps/backend/tests/test_aiops_recovery_dispatch_job.py`

**Interfaces:**
- Consumes: `AutoRecoveryIntentDispatcher`, existing `BackgroundJobContext.append_event()`, `MemoryRepositories.diagnostics`, and the existing diagnostic runner.
- Produces: `_aiops_job_handler(app)` that skips runner execution for an already-`succeeded` task, dispatches the Intent exactly once logically, and appends one safe dispatch event per successful handler attempt.

- [ ] **Step 1: Write failing handler tests with injected fakes**

Exercise the returned handler directly with a fake `BackgroundJobContext` and app state:

```python
await handler(context)
assert runner.stream_calls == 1
assert dispatcher.calls == [("owner-1", "diagnostic-1")]
assert context.events[-1]["type"] == "recovery.intent.dispatch"
```

For retry compensation, start with a `succeeded` task and a dispatcher returning `reused`:

```python
await handler(context)
assert runner.stream_calls == 0
assert context.events[-1]["outcome"] == "reused"
```

Also assert:

```python
assert failed_runner.stream_calls == 0
assert failed_dispatcher.calls == []
assert cancelled_runner.stream_calls == 0
assert cancelled_dispatcher.calls == []
```

Lock the handler state machine to these rules: only non-terminal `accepted`/`running` tasks may enter the runner; `failed` raises the existing safe diagnostic failure; `cancelled` raises `JobCancelled`; already-`succeeded` skips the runner and enters compensation dispatch. Add a cancellation-race test where `context.raise_if_cancelled()` raises after diagnosis persistence but before dispatch and assert zero dispatcher calls. Add a dispatcher-reread race test returning `task_unavailable`; the handler must raise `RuntimeError("Diagnostic task is unavailable.")` rather than append a normal skip event or let the Job succeed. Also assert an unexpected dispatcher error propagates and recursively scanning every emitted event finds no forbidden key or value.

- [ ] **Step 2: Run the handler tests and verify missing composition/invocation failures**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_aiops_recovery_dispatch_job.py -q
```

Expected: tests fail because the app does not expose or invoke the dispatcher and the handler reruns succeeded tasks.

- [ ] **Step 3: Compose the dispatcher and split handler execution from dispatch**

After constructing `RecoveryIntentService`, assign:

```python
app.state.auto_recovery_intent_dispatcher = AutoRecoveryIntentDispatcher(
    diagnostics=repositories.diagnostics,
    recovery_intents=recovery_intent_service,
)
```

In `_aiops_job_handler`, branch before running: `succeeded` goes directly to compensation dispatch, `failed` raises the existing diagnostic failure, `cancelled` raises `JobCancelled`, and only `accepted`/`running` invokes `runner.stream()`. Re-read the task after execution, preserve cancellation and failure behavior, and require the final task status to equal `succeeded`. Immediately before dispatch call `await context.raise_if_cancelled()` to close the post-diagnosis cancellation race. Invoke the dispatcher, convert `task_unavailable` to `RuntimeError("Diagnostic task is unavailable.")`, and append only `result.public_event()`. Do not catch unexpected dispatcher exceptions.

- [ ] **Step 4: Run handler, dispatcher, SSE, and recovery regression tests**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_aiops_recovery_dispatch_job.py apps/backend/tests/test_recovery_auto_dispatch.py apps/backend/tests/test_aiops_sse_delivery.py apps/backend/tests/test_recovery_intent_service.py apps/backend/tests/test_recovery_worker.py -q
```

Expected: all focused tests pass; no real LLM call is made.

- [ ] **Step 5: Run focused static checks and commit the connection**

Run:

```powershell
uv run --project apps/backend ruff check apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/recovery/auto_dispatch.py apps/backend/tests/test_aiops_recovery_dispatch_job.py
uv run --project apps/backend pyright apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/recovery/auto_dispatch.py
```

Expected: both commands pass.

```powershell
git add apps/backend/src/super_ai/api/app.py apps/backend/tests/test_aiops_recovery_dispatch_job.py
git commit -m "feat: connect alert diagnosis to recovery"
```

### Task 4: Prove the complete production recovery closure

**Files:**
- Modify: `apps/backend/tests/live/test_production_compose_recovery.py`
- Modify: `apps/backend/tests/test_postgres_recovery_integration.py`
- Modify: `openspec/changes/add-production-recovery-execution/tasks.md`

**Interfaces:**
- Consumes: Alertmanager webhook API, diagnostic Job runtime, persisted report/evidence, automatic dispatcher, formal Recovery Intent API/repository, and existing `production_recovery` worker.
- Produces: acceptance evidence correlating Incident, Diagnostic Task, Report/Evidence, formal RecoveryIntent, execution key, verification, and recovered terminal state.

- [ ] **Step 1: Extend the live test with a failing formal-Intent assertion**

Drive the existing isolated order-pool alert fixture through the webhook, then poll persisted state and assert:

```python
assert lifecycle.diagnostic_task_id is not None
assert intent.diagnostic_task_id == lifecycle.diagnostic_task_id
assert intent.status == "recovered"
assert intent.execution_key is not None
```

Redeliver the same alert and re-run the diagnosis Job claim path, then assert the same Intent ID and execution key are reused and the container identity changed only once.

Add a focused PostgreSQL automatic-dispatch integration case using a succeeded, alert-marked diagnostic and existing validated blocker report/evidence fixtures:

```python
result = await dispatcher.dispatch(
    owner_user_id=owner_user_id,
    diagnostic_task_id=diagnostic_task_id,
)
assert result.outcome == "created"
assert result.status == "awaiting_approval"
assert await jobs.find_for_resource(
    owner_user_id=owner_user_id,
    resource_type="recovery_intent",
    resource_id=result.intent_id,
) is None
assert executor.calls == []
```

The test must enter through the alert source marker and Dispatcher, not call `RecoveryIntentService` directly.

- [ ] **Step 2: Run the live test and verify it fails before relying on the new chain**

Run the repository's documented isolated Compose live-test command for `test_production_compose_recovery.py`, selecting only the new Alertmanager closure case.

Expected: before final fixture wiring, the test fails at the formal RecoveryIntent correlation rather than using the legacy Live Eval recovery key.

- [ ] **Step 3: Add only fixture wiring required by the production path**

Use the real webhook parser/repository, real Background Job repositories, real dispatcher, and real Compose recovery worker. Stub only diagnosis model output with the already validated report/evidence payload so the acceptance tests production control flow without spending model quota or reading ground truth. Do not call the executor directly and do not inject a recovery action, target, path, PID, or execution key.

- [ ] **Step 4: Run the final focused verification matrix**

Run:

```powershell
uv run --project apps/backend pytest apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_chat_aiops_bridge.py apps/backend/tests/test_recovery_auto_dispatch.py apps/backend/tests/test_aiops_recovery_dispatch_job.py apps/backend/tests/test_recovery_intent_service.py apps/backend/tests/test_recovery_worker.py apps/backend/tests/test_postgres_recovery_integration.py -q
uv run --project apps/backend ruff check apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py apps/backend/src/super_ai/recovery/auto_dispatch.py apps/backend/src/super_ai/api/app.py apps/backend/tests/test_recovery_auto_dispatch.py apps/backend/tests/test_aiops_recovery_dispatch_job.py
uv run --project apps/backend pyright apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py apps/backend/src/super_ai/recovery/auto_dispatch.py apps/backend/src/super_ai/api/app.py
openspec validate add-production-recovery-execution --strict
```

Then run only the new isolated live Compose closure case using the existing repository command documented beside that test.

Expected: unit/integration tests, Ruff, Pyright, OpenSpec, and the single live closure case pass. The live evidence ends at a formal `RecoveryIntent.status == "recovered"`, PostgreSQL remains approval-only, and no full pytest run occurs.

- [ ] **Step 5: Record evidence and commit acceptance**

Mark the new OpenSpec dispatch tasks complete and record only safe IDs, statuses, counts, timings, and command outcomes; exclude secrets and raw tool/executor output.

```powershell
git add apps/backend/tests/live/test_production_compose_recovery.py openspec/changes/add-production-recovery-execution/tasks.md
git commit -m "test: verify alert recovery closure"
```

## Final Acceptance Checklist

- [ ] Alertmanager ingestion alone stamps `triggerSource=alertmanager`, overriding any untrusted input.
- [ ] Manual API and Chat diagnostic paths cannot auto-dispatch an Intent.
- [ ] A successful alert diagnosis creates or reuses a formal Intent through `RecoveryIntentService`.
- [ ] A crash after Intent creation retries dispatch without rerunning Agent, RAG, MCP, or LLM.
- [ ] Cancellation after diagnosis persistence but before dispatch creates no Intent or recovery Job.
- [ ] Eligible Compose flows through the existing worker to `recovered`; PostgreSQL stays `awaiting_approval`.
- [ ] Alert-triggered PostgreSQL dispatch creates no `production_recovery` Job and invokes no executor before owner approval.
- [ ] Duplicate alert delivery and Job retry produce no second active Intent, recovery Job, or side effect.
- [ ] Public Job events contain only safe dispatch classification and public IDs/status.
- [ ] Focused tests and checks pass without full pytest or real model quota use.
