# Order Pool Prometheus Auto-Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` and `test-driven-development`. The primary Agent executes every task in order; do not dispatch implementation subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one real Single-Agent Live Eval in which Prometheus detects the order-pool leak, Alertmanager starts the durable diagnosis, a deterministic policy authorizes one fixed Compose restart, independent checks verify recovery, and the correlated Incident closes as verified.

**Architecture:** Instrument the isolated order API with bounded Prometheus metrics and add a Live Eval Prometheus profile. Extend alert persistence with safe run/scenario correlation and a verification state machine. A new process-local Live Eval orchestrator owns the driver and Docker authority, waits for the existing durable Single-Agent report, applies the existing deterministic recovery contract through `ExecutionCoordinator`, verifies the result, waits for Alertmanager resolved, and persists the terminal evaluation artifact.

**Tech Stack:** Python 3.12, FastAPI, prometheus-client 0.26.x, Prometheus 3.14.0, Alertmanager, PostgreSQL/SQLAlchemy/Alembic, LangGraph, Docker Compose, pytest, Ruff, Pyright.

## Global Constraints

- Recovery is restricted to scenario `APY-LIVE-ORDER-POOL-LEAK-001` and code-owned target `live-eval-order-api`.
- Only the Live Eval orchestrator receives Docker execution authority; backend, LLM, Prometheus, and Alertmanager receive none.
- Final acceptance uses Single Agent and never calls the manual alert publisher.
- PostgreSQL execution uniqueness is authoritative; an unprovable side-effect timeout becomes `MANUAL_REVIEW` and is never replayed automatically.
- A resolved alert is not remediation proof; verified closure requires resolved lifecycle plus persisted independent verification.
- Never expose webhook tokens, model/API keys, CLS credentials, database credentials, `run_token`, fault tokens, raw exceptions, or Docker arguments.
- Do not copy `config/user.project.json` into the worktree.
- Run focused tests only, not the full pytest suite.
- Each task ends with a separate commit.

## Reuse Decision

- **Direct adoption:** `prom/prometheus:v3.14.0` (Apache-2.0).
- **Wrapped adoption:** `prometheus-client>=0.26.0,<1.0.0` (Apache-2.0), isolated behind the order API fixture.
- **Internal reuse:** `OrderPoolLeakScenarioDriver`, `ComposeServiceRestarter`, `OrderPoolRecoveryService`, Alertmanager ingestion, background jobs, Single-Agent diagnostic pipeline, `ExecutionCoordinator`, and evaluation history.
- **Reference only:** Chaos Mesh and Litmus experiment lifecycle patterns; neither dependency is added.

## File Map

- `infra/live-eval/order_api.py`: owns the six safe fixture metrics and `/metrics` endpoint.
- `infra/live-eval/order-api.Dockerfile`, `apps/backend/pyproject.toml`: pin the official client dependency.
- `infra/prometheus/prometheus.yml`, `infra/prometheus/rules/live-eval-order-pool.yml`: scrape and rule contract.
- `infra/alertmanager/alertmanager.yml`, `infra/compose.yaml`: run-aware Alertmanager grouping and Live Eval Prometheus service.
- `apps/backend/alembic/versions/202608220002_add_live_alert_verification.py`: incident correlation and verification columns/index/check.
- `apps/backend/src/super_ai/alert_ingestion/{alertmanager.py,repositories.py,service.py,sqlalchemy.py,metrics.py,routes.py}`: safe correlation parsing, transactional persistence, lifecycle lookup, and metrics.
- `apps/backend/src/super_ai/evaluation/live/auto_closure.py`: orchestration contracts, polling, authorization, resume, recovery, verification, and failure classification.
- `apps/backend/src/super_ai/evaluation/live/cli.py`: `--auto-closure` and `--resume` wiring with fixed Single-Agent strategy.
- `apps/backend/src/super_ai/evaluation/{history.py,persistence.py}`: safe closure identifiers/timings in existing Live artifacts.
- Focused tests mirror each changed area under `apps/backend/tests/` and `infra/tests/`.

---

### Task 1: Safe Order API Metrics

**Files:**
- Modify: `apps/backend/pyproject.toml`
- Modify: `infra/live-eval/order-api.Dockerfile`
- Modify: `infra/live-eval/order_api.py`
- Create: `infra/tests/test_order_api_metrics.py`

**Interfaces:**
- Produces: `OrderApiMetrics.render(runtime: OrderApiRuntime) -> bytes` and `GET /metrics` with exactly six `agentpy_order_*` gauge families.
- Label set: `service`, `environment`, `scenario_id`, `run_id`; active-run series disappear after `clear_run`.

- [ ] **Step 1: Add failing contract tests**

```python
def test_metrics_expose_only_bounded_public_order_pool_state() -> None:
    app = create_app(fake_runtime_with_confirmed_fault())
    body = TestClient(app).get("/metrics").text
    assert 'agentpy_order_pool_fault_active{environment="live-eval"' in body
    assert 'scenario_id="APY-LIVE-ORDER-POOL-LEAK-001"' in body
    assert "run_token" not in body
    assert "fault_token" not in body

def test_clear_run_removes_run_label_series() -> None:
    runtime = fake_runtime_with_confirmed_fault(run_id="metrics-001")
    runtime.clear_run(...)
    assert "metrics-001" not in OrderApiMetrics().render(runtime).decode()
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest infra/tests/test_order_api_metrics.py -q`
Expected: FAIL because `/metrics` and `OrderApiMetrics` do not exist.

- [ ] **Step 3: Implement the narrow registry**

Add `prometheus-client>=0.26.0,<1.0.0`, create a private `CollectorRegistry`, rebuild gauges from a locked immutable runtime snapshot per request, and return `generate_latest(registry)` as `text/plain; version=0.0.4; charset=utf-8`. Validate the public `run_id` with the existing run ID rule before it reaches labels. Never export exception text or tokens.

- [ ] **Step 4: Verify green and dependency lock**

Run: `uv lock --project apps/backend && uv run --project apps/backend pytest infra/tests/test_order_api_metrics.py -q`
Expected: PASS; lock contains prometheus-client 0.26.x.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/pyproject.toml apps/backend/uv.lock infra/live-eval/order-api.Dockerfile infra/live-eval/order_api.py infra/tests/test_order_api_metrics.py
git commit -m "feat: expose safe order pool metrics"
```

### Task 2: Prometheus Detection and Alertmanager Grouping

**Files:**
- Create: `infra/prometheus/prometheus.yml`
- Create: `infra/prometheus/rules/live-eval-order-pool.yml`
- Modify: `infra/alertmanager/alertmanager.yml`
- Modify: `infra/compose.yaml`
- Create: `infra/tests/test_prometheus_contract.py`

**Interfaces:**
- Produces alert `OrderApiConnectionPoolExhausted` after all six fault facts remain true for the configured interval.
- Alertmanager `route.group_by` is exactly `alertname`, `service`, `environment`, `scenario_id`, `run_id`; firing and resolved therefore share one lifecycle while separate run IDs cannot collide.

- [ ] **Step 1: Write failing YAML/Compose contract tests**

```python
def test_alert_rule_requires_the_complete_fault_conjunction() -> None:
    rule = load_rule("OrderApiConnectionPoolExhausted")
    for metric in EXPECTED_SIX_METRICS:
        assert metric in rule["expr"]

def test_alertmanager_groups_live_runs_independently() -> None:
    route = load_alertmanager()["route"]
    assert route["group_by"] == [
        "alertname", "service", "environment", "scenario_id", "run_id"
    ]
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest infra/tests/test_prometheus_contract.py -q`
Expected: FAIL because Prometheus configuration is absent.

- [ ] **Step 3: Add configuration**

Use `prom/prometheus:v3.14.0`, two-second scrape/evaluation intervals, one target `live-eval-order-api:8000`, `send_resolved: true`, no Docker socket, and no host-published Prometheus port. The alert expression must require `fault_active == 1`, `pool_free == 0`, `checked_out == capacity`, `waiter_observed == 1`, and `business_probe_success == 0`; attach only the approved labels plus `severity=critical`.

- [ ] **Step 4: Validate YAML, rules, and rendered Compose**

Run: `docker run --rm --entrypoint promtool -v "${PWD}/infra/prometheus:/etc/prometheus:ro" prom/prometheus:v3.14.0 check config /etc/prometheus/prometheus.yml`
Expected: `SUCCESS`.

Run: `docker run --rm --entrypoint promtool -v "${PWD}/infra/prometheus:/etc/prometheus:ro" prom/prometheus:v3.14.0 check rules /etc/prometheus/rules/live-eval-order-pool.yml`
Expected: `SUCCESS`.

Run: `docker compose -f infra/compose.yaml --profile live-eval config --quiet`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add infra/prometheus infra/alertmanager/alertmanager.yml infra/compose.yaml infra/tests/test_prometheus_contract.py
git commit -m "feat: detect order pool exhaustion with prometheus"
```

### Task 3: Safe Alert Correlation Parsing

**Files:**
- Modify: `apps/backend/src/super_ai/alert_ingestion/alertmanager.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/service.py`
- Modify: `apps/backend/tests/alert_ingestion/test_alertmanager_parser.py`
- Modify: `apps/backend/tests/alert_ingestion/test_service.py`

**Interfaces:**
- `AlertmanagerAlert.scenario_id: str | None`, `AlertmanagerAlert.run_id: str | None`.
- `IngestionWrite.scenario_id: str | None`, `IngestionWrite.run_id: str | None`.
- Only exact top-level labels are accepted; nested fields, annotations, disguised `oracle`/`ground_truth`/execution authority, traversal, or overlength values are rejected.

- [ ] **Step 1: Add failing parser tests**

```python
def test_parser_accepts_allowlisted_live_correlation_labels() -> None:
    alert = parse_delivery(live_payload()).alerts[0]
    assert alert.scenario_id == "APY-LIVE-ORDER-POOL-LEAK-001"
    assert alert.run_id == "closure-001"

@pytest.mark.parametrize("mutation", [nested_scenario, traversal_run, authority_label])
def test_parser_rejects_disguised_or_unsafe_correlation(mutation) -> None:
    with pytest.raises(AlertPayloadError):
        parse_delivery(mutation(live_payload()))
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/alert_ingestion/test_alertmanager_parser.py apps/backend/tests/alert_ingestion/test_service.py -q`
Expected: FAIL on missing correlation fields.

- [ ] **Step 3: Implement normalization**

Accept `scenario_id` only when it equals `APY-LIVE-ORDER-POOL-LEAK-001`; validate `run_id` with `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`, reject `..`, `/`, and `\\`. Require scenario and run together. Propagate normalized values from the first alert only after verifying every alert in the group has the identical pair.

- [ ] **Step 4: Verify green**

Run: same focused pytest command.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/alert_ingestion apps/backend/tests/alert_ingestion/test_alertmanager_parser.py apps/backend/tests/alert_ingestion/test_service.py
git commit -m "feat: correlate live alerts safely"
```

### Task 4: Incident Correlation and Verification Schema

**Files:**
- Create: `apps/backend/alembic/versions/202608220002_add_live_alert_verification.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Create: `apps/backend/tests/memory/test_live_alert_verification_migration.py`

**Interfaces:**
- `AlertIncidentModel.run_id`, `scenario_id`, `verification_status`, `verified_at`, `verification_summary`.
- Verification values: `pending`, `passed`, `failed`, `not_applicable`.
- Index: `(owner_user_id, source_id, scenario_id, run_id)`; nullable for ordinary incidents.

- [ ] **Step 1: Write failing model/migration tests**

```python
def test_live_incident_columns_and_constraints_exist(upgraded_inspector) -> None:
    columns = upgraded_inspector.columns("aiops_alert_incidents")
    assert {"run_id", "scenario_id", "verification_status", "verified_at", "verification_summary"} <= columns
    assert upgraded_inspector.has_index(
        "ix_aiops_alert_incidents_live_correlation",
        ["owner_user_id", "source_id", "scenario_id", "run_id"],
    )
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/memory/test_live_alert_verification_migration.py -q`
Expected: FAIL because revision `202608220002` is absent.

- [ ] **Step 3: Implement reversible migration and ORM fields**

Use bounded strings (`run_id` 80, `scenario_id` 96, status 24, summary 512), a check constraint for the four statuses, and a non-unique lookup index. New ordinary rows default to `not_applicable`; ingestion explicitly writes `pending` for the allowlisted live scenario.

- [ ] **Step 4: Verify upgrade/downgrade/upgrade**

Run: focused migration test above.
Expected: PASS and Alembic returns to head.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/alembic/versions/202608220002_add_live_alert_verification.py apps/backend/src/super_ai/memory/models.py apps/backend/tests/memory/test_live_alert_verification_migration.py
git commit -m "feat: persist live incident verification"
```

### Task 5: Transactional Lifecycle Repository

**Files:**
- Modify: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Create: `apps/backend/tests/alert_ingestion/test_sqlalchemy_live_lifecycle.py`

**Interfaces:**
- `LiveAlertLifecycle(incident_id, diagnostic_task_id, background_job_id, report_id, status, verification_status)`.
- `get_live_lifecycle(*, owner_user_id: str, source_id: str, scenario_id: str, run_id: str) -> LiveAlertLifecycle | None`.
- `record_verification(..., status: Literal["passed", "failed"], summary: str, verified_at: datetime) -> LiveAlertLifecycle`.
- `close_verified(...)` changes no verification fact; it reports closure only when incident is resolved and verification passed.

- [ ] **Step 1: Add failing PostgreSQL integration tests**

```python
async def test_resolved_and_verification_converge_in_either_order(repository) -> None:
    lifecycle = await create_live_incident(repository)
    verified = await repository.record_verification(..., status="passed", summary="six checks passed", verified_at=NOW)
    assert verified.status == "firing"
    await deliver_resolved(repository, lifecycle)
    closed = await repository.get_live_lifecycle(...)
    assert (closed.status, closed.verification_status) == ("resolved", "passed")

async def test_commit_failure_rolls_back_and_maps_to_safe_error(repository, failing_session) -> None:
    with pytest.raises(AlertPersistenceError, match="persistence is unavailable"):
        await repository.apply(live_write())
    assert await count_partial_tasks() == 0
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/alert_ingestion/test_sqlalchemy_live_lifecycle.py -q`
Expected: FAIL on missing lifecycle APIs.

- [ ] **Step 3: Implement exact lookup and monotonic transitions**

Join Incident to its diagnostic Task, Background Job, and latest persisted Report by IDs, always filter owner and source, and never use “latest task”. `failed` verification is immutable except an explicit resume writes a new audit record; `passed` is idempotent. Wrap session creation, statements, flush, commit, and `session.begin()` exit `SQLAlchemyError`/driver errors as `AlertPersistenceError`; rollback before rethrow. A uniqueness race must re-read safely in a fresh transaction.

- [ ] **Step 4: Verify green and tenant isolation**

Run: focused test above plus `apps/backend/tests/alert_ingestion/test_sqlalchemy_repository.py`.
Expected: PASS; cross-owner lookup returns `None`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/alert_ingestion/repositories.py apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py apps/backend/tests/alert_ingestion/test_sqlalchemy_live_lifecycle.py
git commit -m "feat: persist correlated alert lifecycle"
```

### Task 6: Alert HTTP and Runtime Metrics Semantics

**Files:**
- Modify: `apps/backend/src/super_ai/alert_ingestion/metrics.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/routes.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/alert_ingestion/test_routes.py`
- Modify: `apps/backend/tests/alert_ingestion/test_metrics.py`

**Interfaces:**
- `webhookReceivedTotal`: increment once after route/path match and before auth/body/schema processing; includes 401, 413, 422, 503.
- `ingestionFailedTotal`: increment for authenticated request failures that prevent durable apply (413, 422, 503); excludes 401/404 and post-commit wake failures.
- `runtimeWakeFailedTotal`: committed job could not be signalled; HTTP remains 202.
- `verifiedClosureTotal`, `verificationFailedTotal`, and bounded stage-latency aggregates are explicit orchestration events.

- [ ] **Step 1: Add failing status/metric matrix tests**

```python
@pytest.mark.parametrize(
    ("case", "status", "received", "failed", "wake_failed"),
    [("bad_auth", 401, 1, 0, 0), ("too_large", 413, 1, 1, 0),
     ("bad_schema", 422, 1, 1, 0), ("db_down", 503, 1, 1, 0),
     ("wake_down", 202, 1, 0, 1)],
)
def test_alert_metric_semantics(case, status, received, failed, wake_failed) -> None:
    response, metrics = exercise_case(case)
    assert response.status_code == status
    assert metrics["webhookReceivedTotal"] == received
    assert metrics["ingestionFailedTotal"] == failed
    assert metrics["runtimeWakeFailedTotal"] == wake_failed
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/alert_ingestion/test_routes.py apps/backend/tests/alert_ingestion/test_metrics.py -q`
Expected: FAIL on the new counters and boundary semantics.

- [ ] **Step 3: Implement one boundary owner per counter**

Route records receipt and request failures; Service records disposition and Redis mode; Runtime wake wrapper records wake failure after commit. Catch `AlertPersistenceError` as 503 with a constant safe body. Never return raw SQLAlchemy/asyncpg details. Add orchestration methods for verification and stage latency without per-run metric labels.

- [ ] **Step 4: Verify green**

Run: same focused tests.
Expected: PASS for every matrix row.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/alert_ingestion/metrics.py apps/backend/src/super_ai/alert_ingestion/routes.py apps/backend/src/super_ai/api/app.py apps/backend/tests/alert_ingestion/test_routes.py apps/backend/tests/alert_ingestion/test_metrics.py
git commit -m "fix: define alert lifecycle metric boundaries"
```

### Task 7: Auto-Closure Orchestrator and Failure Classification

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/auto_closure.py`
- Create: `apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py`

**Interfaces:**
- `AutoClosureBudgets(detection_seconds=45, diagnosis_seconds=360, recovery_seconds=30, verification_seconds=60, resolved_seconds=60, poll_seconds=2)`.
- `AutoClosureCorrelation(incident_id, diagnostic_task_id, background_job_id, report_id, recovery_intent_id)`.
- `OrderPoolAutoClosureOrchestrator.run(scenario_id: str, *, run_id: str, resume: bool = False) -> LiveAutoClosureResult`.
- Validities: `VALID_PASS`, `VALID_FAIL`, `INFRA_INVALID`, `MANUAL_REVIEW`.

- [ ] **Step 1: Write failing lifecycle tests with fake clock/adapters**

```python
async def test_waits_for_automatic_alert_report_then_recovers_and_closes() -> None:
    result = await orchestrator.run(SCENARIO_ID, run_id="auto-001")
    assert result.validity == "VALID_PASS"
    assert result.strategy == "single_agent"
    assert result.correlation.report_id == "report-1"
    assert result.verification.passed
    assert restarter.calls == ["live-eval-order-api"]

async def test_resolved_does_not_cancel_inflight_diagnosis_or_start_another() -> None:
    result = await orchestrator.run(SCENARIO_ID, run_id="auto-early-resolved")
    assert result.diagnostic_task_id == "task-1"
    assert diagnostic_calls_for("task-1") == expected_original_calls
    assert created_diagnostic_task_ids() == ["task-1"]
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py -q`
Expected: FAIL because module is absent.

- [ ] **Step 3: Implement bounded stage machine**

Perform preflight/baseline/inject with `OrderPoolLeakScenarioDriver`; poll exact lifecycle correlation; wait for persisted Report rather than invoking the Agent directly; emit a progress callback at every stage and bounded poll interval. Classify dependency readiness/timeouts as `INFRA_INVALID`, valid-but-unsatisfied diagnosis/recovery/verification as `VALID_FAIL`, and uncertain side effects as `MANUAL_REVIEW`. Resolved never cancels a Task. Keep safe stage timestamps and IDs only.

- [ ] **Step 4: Add timeout, duplicate, early-resolved, and cleanup tests**

```python
@pytest.mark.parametrize("dependency", ["prometheus", "alertmanager", "cls", "rag", "model"])
async def test_readiness_failure_is_infra_invalid(dependency) -> None:
    assert (await orchestrator_with_down(dependency).run(...)).validity == "INFRA_INVALID"

async def test_cleanup_runs_on_every_terminal_path() -> None:
    await orchestrator_with_failed_report().run(...)
    assert driver.cleanup_calls == 1
```

- [ ] **Step 5: Verify green**

Run: focused orchestrator test.
Expected: PASS without network or Docker.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/evaluation/live/auto_closure.py apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py
git commit -m "feat: orchestrate automatic live remediation"
```

### Task 8: Deterministic Authorization, Idempotent Restart, and Resume

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/auto_closure.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/order_pool_leak.py`
- Modify: `apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py`
- Modify: `apps/backend/tests/evaluation/live/test_order_pool_leak.py`

**Interfaces:**
- `authorize_order_pool_recovery(report: RunArtifact, observation: LiveFaultObservation, identity: LiveRunIdentity) -> RecoveryAuthorization`.
- Stable execution identity uses diagnostic Task ID, graph version, scenario, action `compose_restart`, fixed target, logical attempt 0, and canonical safe facts.
- `resume=True` reloads exact run/lifecycle/execution state; no restart if complete, no replay if uncertain.

- [ ] **Step 1: Add failing authorization matrix tests**

```python
@pytest.mark.parametrize("broken", [
    "scenario", "component", "mechanism", "observation", "evidence_sufficiency",
    "deterministic_validator", "target", "driver_identity",
])
async def test_every_authorization_predicate_is_required(broken) -> None:
    authorization = authorize_order_pool_recovery(*case_with(broken))
    assert not authorization.execution_permitted
    assert restarter.calls == []
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py apps/backend/tests/evaluation/live/test_order_pool_leak.py -q`
Expected: FAIL on missing exact authorization/result fields.

- [ ] **Step 3: Implement recovery through `ExecutionCoordinator`**

Require all eight predicates; semantic Validator may only reduce confidence. Construct the target in code. Call `run_once(..., outcome_known_on_error=False)`. On reuse return the recorded outcome. After timeout, compare execution record, order-api generation, and old run-scoped sessions: proven restart becomes completed/reused; unprovable outcome remains uncertain and raises `UnsafeExecutionReplay` mapped to `MANUAL_REVIEW`.

- [ ] **Step 4: Add restart-once and resume tests**

```python
async def test_concurrent_and_resumed_runs_restart_at_most_once() -> None:
    first, second = await asyncio.gather(run(), run(resume=True))
    assert restarter.calls == ["live-eval-order-api"]
    assert first.recovery_intent_id == second.recovery_intent_id

async def test_unprovable_timeout_never_replays_restart() -> None:
    assert (await timed_out_run()).validity == "MANUAL_REVIEW"
    assert (await resume_run()).validity == "MANUAL_REVIEW"
    assert len(restarter.calls) == 1
```

- [ ] **Step 5: Verify green**

Run: focused tests above plus `apps/backend/tests/aiops/test_execution.py`.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/evaluation/live/auto_closure.py apps/backend/src/super_ai/evaluation/live/order_pool_leak.py apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py apps/backend/tests/evaluation/live/test_order_pool_leak.py
git commit -m "feat: authorize idempotent live recovery"
```

### Task 9: Independent Verification and Verified Closure

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/auto_closure.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Modify: `apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py`
- Modify: `apps/backend/tests/alert_ingestion/test_sqlalchemy_live_lifecycle.py`

**Interfaces:**
- Verification checks: generation changed, old sessions gone, business probe succeeds, PostgreSQL healthy, unrelated sessions unchanged, recovery execution recorded.
- Verified closure is true only for `incident.status == "resolved"`, `verification_status == "passed"`, `LiveVerification.passed`, and clean cleanup audit.

- [ ] **Step 1: Add failing six-check tests**

```python
async def test_alert_disappearance_alone_cannot_pass_verification() -> None:
    result = await orchestrator_with(resolved=True, generation_changed=False).run(...)
    assert result.validity == "VALID_FAIL"
    assert result.incident_verification == "failed"

async def test_verification_before_resolved_stays_pending_then_closes() -> None:
    result = await orchestrator_with(verification_first=True).run(...)
    assert result.closed_verified
    assert result.verification.check_count == 6
```

- [ ] **Step 2: Verify red**

Run: focused orchestrator and lifecycle tests.
Expected: FAIL on incomplete verification semantics.

- [ ] **Step 3: Implement independent checks and monotonic persistence**

Run driver verification after the restart, add execution-record proof and unrelated-session preservation, persist a bounded summary containing only check names/results, then wait for Prometheus rule inactivity and correlated Alertmanager resolved. If checks fail, persist `failed`; never rewrite it to passed without explicit resume/audit.

- [ ] **Step 4: Verify green**

Run: focused tests.
Expected: PASS for both arrival orders and every individual failed check.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/evaluation/live/auto_closure.py apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py apps/backend/tests/alert_ingestion/test_sqlalchemy_live_lifecycle.py
git commit -m "feat: verify automatic incident closure"
```

### Task 10: CLI and Existing Evaluation History Integration

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Modify: `apps/backend/src/super_ai/evaluation/persistence.py`
- Modify: `apps/backend/tests/evaluation/live/test_cli.py`
- Modify: `apps/backend/tests/evaluation/test_history.py`

**Interfaces:**
- CLI: existing Live command gains `--auto-closure` and `--resume RUN_ID`; `--auto-closure` forces `investigationStrategy=single_agent` and rejects forced Multi.
- Live metrics add bounded timings/IDs through approved schema keys: MTTD, diagnosis/recovery/resolved/MTTR milliseconds and safe correlation IDs.
- Every terminal run, including `VALID_FAIL`, `INFRA_INVALID`, `MANUAL_REVIEW`, and interruption, is persisted to PostgreSQL and archive.

- [ ] **Step 1: Add failing CLI/history tests**

```python
def test_auto_closure_forces_single_agent_and_persists_correlation() -> None:
    result = invoke_cli("live", SCENARIO_ID, "--auto-closure", "--run-id", "cli-001")
    assert result.exit_code == 0
    artifact = load_artifact("cli-001")
    assert artifact.metadata["investigationStrategy"] == "single_agent"
    assert artifact.result_payload["correlation"]["incidentId"] == "incident-1"

def test_resume_requires_exact_existing_run_id() -> None:
    assert invoke_cli("live", SCENARIO_ID, "--resume", "missing").exit_code != 0
```

- [ ] **Step 2: Verify red**

Run: `uv run --project apps/backend pytest apps/backend/tests/evaluation/live/test_cli.py apps/backend/tests/evaluation/test_history.py -q`
Expected: FAIL on unknown options/schema fields.

- [ ] **Step 3: Wire production adapters and extend allowlists**

Build the existing driver, lifecycle repository, report loader, `ExecutionCoordinator`, restarter, evaluator, archive, and database persistence from current project configuration. Reject `--auto-closure` with Multi flags. Serialize only safe IDs, timings, predicate results, verification checks, resolved/verification states, cleanup audit, and reuse decisions. Use existing `terminal_envelope`/persistence paths on every exit.

- [ ] **Step 4: Verify green and interruption persistence**

Run: focused CLI/history tests.
Expected: PASS; Ctrl-C fixture produces an `interrupted` record.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/src/super_ai/evaluation/history.py apps/backend/src/super_ai/evaluation/persistence.py apps/backend/tests/evaluation/live/test_cli.py apps/backend/tests/evaluation/test_history.py
git commit -m "feat: expose automatic live closure command"
```

### Task 11: Security and Focused Regression Gate

**Files:**
- Create: `apps/backend/tests/evaluation/live/test_auto_closure_security.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- CI adds one focused offline auto-closure job; real Docker/LLM acceptance remains manual and credential-gated.

- [ ] **Step 1: Add negative security tests**

```python
@pytest.mark.parametrize("payload", [
    {"scenario_id": "../APY-LIVE-ORDER-POOL-LEAK-001"},
    {"oracle": {"primary_cause": "answer"}},
    {"recovery_target": "backend"},
])
def test_untrusted_inputs_cannot_select_answer_or_action(payload) -> None:
    with pytest.raises((ValueError, AlertPayloadError)):
        parse_or_authorize(payload)

def test_serialized_artifact_and_metrics_contain_no_secret_markers() -> None:
    serialized = combined_public_outputs().casefold()
    assert not any(marker in serialized for marker in SECRET_MARKERS)
```

- [ ] **Step 2: Verify focused suite**

Run: `uv run --project apps/backend pytest infra/tests/test_order_api_metrics.py infra/tests/test_prometheus_contract.py apps/backend/tests/alert_ingestion apps/backend/tests/evaluation/live/test_auto_closure_orchestrator.py apps/backend/tests/evaluation/live/test_auto_closure_security.py apps/backend/tests/evaluation/live/test_order_pool_leak.py apps/backend/tests/aiops/test_execution.py -q`
Expected: PASS.

- [ ] **Step 3: Run static checks on changed Python**

Run: `uv run --project apps/backend ruff check infra/live-eval/order_api.py apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/evaluation/live apps/backend/tests/alert_ingestion apps/backend/tests/evaluation/live`
Expected: exit 0.

Run: `uv run --project apps/backend pyright apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/evaluation/live infra/live-eval/order_api.py`
Expected: 0 errors.

- [ ] **Step 4: Add focused CI commands and rerun workflow-equivalent checks**

The CI job installs locked backend dependencies, runs the Task 11 pytest list, Ruff, Pyright, Compose render, and Promtool checks. It must not require model, CLS, webhook, Docker socket, or private project configuration.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/tests/evaluation/live/test_auto_closure_security.py .github/workflows/ci.yml
git commit -m "test: gate automatic live closure offline"
```

### Task 12: Real Single-Agent Closed-Loop Acceptance

**Files:**
- Create: `scripts/run_order_pool_auto_closure.ps1`
- Modify: `docs/runbooks/live-eval.md`
- Generated but not committed: existing evaluation archive location and PostgreSQL evaluation rows.

**Interfaces:**
- Script starts required profiles, performs readiness checks, invokes the CLI once, prints short stage updates, and tears down only run-scoped fixture state.
- It never calls the manual alert publisher and never reads or prints secrets.

- [ ] **Step 1: Add script contract test**

```python
def test_acceptance_script_has_no_manual_alert_or_secret_output() -> None:
    script = Path("scripts/run_order_pool_auto_closure.ps1").read_text()
    assert "publish_alertmanager" not in script
    assert "user.project.json" not in script
    assert "--auto-closure" in script
```

- [ ] **Step 2: Start isolated infrastructure**

Run: `docker compose -f infra/compose.yaml --profile live-eval up -d --build postgres redis backend nginx alertmanager prometheus live-eval-order-api`
Expected: all named services become healthy; backend/Prometheus/Alertmanager have no Docker socket mount.

- [ ] **Step 3: Validate baseline**

Run: `docker compose -f infra/compose.yaml --profile live-eval ps`
Expected: services healthy; Prometheus has no active `OrderApiConnectionPoolExhausted` alert.

- [ ] **Step 4: Run one real acceptance without publisher**

Run: `powershell -ExecutionPolicy Bypass -File scripts/run_order_pool_auto_closure.ps1 -RunId "auto-closure-<timestamp>"`
Expected: automatic firing; exactly one correlated Incident/Task/Job; one persisted Single-Agent Report; diagnosis `order-api / exception_path_connection_not_released`; one authorized restart; six verification checks pass; automatic resolved; `verification_status=passed`; terminal `VALID_PASS` artifact saved.

- [ ] **Step 5: Verify attributed model behavior and idempotency**

Query by `diagnostic_task_id`, not global call totals: the firing lifecycle owns one diagnostic task; resolved creates no second task/job and triggers no new model call for another Task. Re-run `--resume` with the same run ID and verify the same Incident, Task, Report, recovery intent, and completed execution are reused with restart count unchanged. Start a distinct run ID and verify Alertmanager creates a separate group/lifecycle because `run_id` is in `group_by`.

- [ ] **Step 6: Verify cleanup and safe persistence**

Confirm no active fixture run, test order, old generation session, or unrelated damage. Scan the saved artifact, report public fields, `/metrics`, and alert labels for secret markers. Do not print matched secret values.

- [ ] **Step 7: Document evidence and commit**

Record run ID, Git SHA, service image IDs, safe lifecycle IDs, timings, score/result, and verification check names/results in `docs/runbooks/live-eval.md`; omit credentials and raw payloads.

```bash
git add scripts/run_order_pool_auto_closure.ps1 docs/runbooks/live-eval.md
git commit -m "docs: verify automatic order pool remediation"
```

## Final Completion Gate

- [ ] `git status --short` shows only intentional generated/untracked evaluation artifacts, or is clean.
- [ ] All Task 11 focused tests, Ruff, Pyright, Compose render, Promtool config, and Promtool rules checks pass.
- [ ] Real Task 12 artifact is persisted and reports `VALID_PASS` with `single_agent`.
- [ ] Exactly one restart is attributable to the recovery execution; cleanup is not credited as recovery.
- [ ] Incident is both `resolved` and `verification_status=passed`.
- [ ] No manual publisher, forced Multi, secret output, Docker authority expansion, or full pytest run occurred.
