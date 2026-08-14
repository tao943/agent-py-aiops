# Multi-Scenario Live Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the PostgreSQL-lock-only Live Eval runtime and add isolated PostgreSQL deadlock, Redis maxclients, and Nginx upstream-timeout experiments for a total of exactly four Live scenarios.

**Architecture:** Replace PostgreSQL-specific lifecycle values with named immutable checks, scenario capabilities, recovery expectations, and a registry selected by CLI scenario ID. Each driver owns only its isolated fixture and scoped tools; the runner owns phase order, classification, and unconditional idempotent cleanup.

**Tech Stack:** Python 3.10, asyncio, asyncpg, redis-py, httpx, Docker Compose, PostgreSQL 16, Redis 7, Nginx 1.30, pytest

## Global Constraints

- Live scenario total must be exactly four, including existing `APY-LIVE-PG-LOCK-001`.
- New IDs are `APY-LIVE-PG-DEADLOCK-001`, `APY-LIVE-REDIS-MAXCLIENTS-001`, and `APY-LIVE-NGINX-TIMEOUT-001`.
- PostgreSQL deadlock may retry only the transaction PostgreSQL aborted for the current run.
- Redis recovery may close only Benchmark clients whose exact client-name prefix belongs to the current run.
- Nginx recovery expectation is `proposal_only`; Agent code must not write config, reload, restart, or switch traffic.
- Fixture cleanup is separately audited and never credited as Agent recovery.
- Dedicated Redis/upstream/Nginx services use a default-disabled Compose `live-eval` profile and must not mutate development services.
- Ordinary CI remains offline; real Docker tests retain the `live_docker` marker.
- Add no dependency and no external Chaos platform.
- Execute inline in the current session; do not start subagents.

---

### Task 1: Generalize Live observations, recovery expectations, verification, and cleanup

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/domain.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Modify: `apps/backend/tests/test_live_benchmark_runner.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Produces: `RecoveryExpectation = Literal["executed_recovery", "proposal_only"]`, `LiveCheck`, generic `LiveFaultObservation`, `LiveRecoveryRecord`, `LiveVerification`, and `LiveCleanupResult`.
- Changes: `LiveScenarioDriver.cleanup(identity) -> LiveCleanupResult`; all other driver protocol methods retain their current names.

- [ ] **Step 1: Write failing runner tests for both recovery expectations and cleanup audit**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expectation", "authorized", "executed"),
    [("executed_recovery", True, True), ("proposal_only", True, False)],
)
async def test_runner_accepts_the_scenario_recovery_contract(
    expectation: RecoveryExpectation, authorized: bool, executed: bool
) -> None:
    driver = RecordingDriver(cleanup_result=LiveCleanupResult((LiveCheck("fixture_clear", True),)))
    runner = make_runner(
        driver=driver,
        recovery=RecordingRecovery(
            LiveRecoveryRecord("bounded_action", "current-run", expectation, authorized, executed, "authorized")
        ),
        oracle=oracle_with_recovery_expectation(expectation),
    )
    await runner.run("APY-LIVE-TEST-001", run_id="run-1")
    assert driver.events[-1] == "cleanup"

@pytest.mark.asyncio
async def test_cleanup_failure_is_infrastructure_invalid_and_never_recovery_credit() -> None:
    runner = make_runner(cleanup=LiveCleanupResult((LiveCheck("fixture_clear", False),)))
    with pytest.raises(LiveBenchmarkError) as captured:
        await runner.run("APY-LIVE-TEST-001", run_id="run-1")
    assert captured.value.category == "cleanup_failed"
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_benchmark_runner.py tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider --basetemp=var/pytest-plan-live-domain
```

Expected: FAIL because current values are PostgreSQL PID/lock-field specific and cleanup returns `None`.

- [ ] **Step 3: Implement the generic immutable contracts**

```python
RecoveryExpectation = Literal["executed_recovery", "proposal_only"]

@dataclass(frozen=True, slots=True)
class LiveCheck:
    name: str
    passed: bool
    source: str = "driver"

@dataclass(frozen=True, slots=True)
class LiveFaultObservation:
    scenario_id: str
    checks: tuple[LiveCheck, ...]
    safe_facts: tuple[tuple[str, str | int | float | bool], ...] = ()

    @property
    def confirmed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

@dataclass(frozen=True, slots=True)
class LiveRecoveryRecord:
    action: str
    target_ref: str
    expectation: RecoveryExpectation
    authorized: bool
    executed: bool
    authorization_code: str
    proposal_checks: tuple[LiveCheck, ...] = ()

@dataclass(frozen=True, slots=True)
class LiveVerification:
    checks: tuple[LiveCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

@dataclass(frozen=True, slots=True)
class LiveCleanupResult:
    checks: tuple[LiveCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)
```

- [ ] **Step 4: Change the runner acceptance rule**

```python
def _recovery_contract_satisfied(record: LiveRecoveryRecord) -> bool:
    if not record.authorized:
        return False
    if record.expectation == "executed_recovery":
        return record.executed
    return not record.executed and bool(record.proposal_checks) and all(
        check.passed for check in record.proposal_checks
    )
```

In the runner, call this function before verification. Always await `cleanup` in `finally`; if cleanup returns failed checks, classify `cleanup_failed`, append cleanup facts separately, and never change `recovery.executed`.

- [ ] **Step 5: Migrate the existing PostgreSQL lock driver/tests and confirm GREEN**

Map current facts to named checks: `waiter_has_lock_event`, `blocker_edge_confirmed`, `blocker_gone`, `waiter_unblocked`, `lock_graph_clear`, `probe_succeeded`, `postgres_healthy`, `unrelated_sessions_untouched`, and `scoped_fixture_removed`.

Run the Step 2 command plus `tests/test_live_postgres_contracts.py`; expected PASS.

- [ ] **Step 6: Commit**

```powershell
git add apps/backend/src/super_ai/evaluation/live/domain.py apps/backend/src/super_ai/evaluation/live/runner.py apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/tests/test_live_benchmark_runner.py apps/backend/tests/test_live_diagnostic_adapter.py apps/backend/tests/test_live_postgres_contracts.py
git commit -m "refactor: generalize live evaluation lifecycle contracts"
```

### Task 2: Add recovery-aware Live oracle loading, scenario registry, and CLI routing

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/scenarios.py`
- Create: `apps/backend/src/super_ai/evaluation/live/registry.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/tests/test_live_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_live_benchmark_cli.py`
- Create: three four-file scenario directories under `benchmarks/agentpy/live/`

**Interfaces:**
- Produces: `LiveScenarioOracle.recovery_expectation`, `LiveScenarioComponents`, `LiveScenarioRegistry.register(...)`, and `LiveScenarioRegistry.resolve(scenario_id: str) -> LiveScenarioComponents`.
- Consumes: validated scenario `driver` values and environment config factories.

- [ ] **Step 1: Write failing schema and registry tests**

```python
def test_repository_contains_exactly_four_live_scenarios() -> None:
    assert sorted(path.name for path in LIVE_SCENARIOS.iterdir() if path.is_dir()) == [
        "APY-LIVE-NGINX-TIMEOUT-001", "APY-LIVE-PG-DEADLOCK-001",
        "APY-LIVE-PG-LOCK-001", "APY-LIVE-REDIS-MAXCLIENTS-001",
    ]

def test_registry_resolves_each_driver_without_fallback() -> None:
    registry = build_test_registry()
    assert registry.resolve("APY-LIVE-PG-LOCK-001").driver_name == "postgres_lock"
    assert registry.resolve("APY-LIVE-PG-DEADLOCK-001").driver_name == "postgres_deadlock"
    assert registry.resolve("APY-LIVE-REDIS-MAXCLIENTS-001").driver_name == "redis_maxclients"
    assert registry.resolve("APY-LIVE-NGINX-TIMEOUT-001").driver_name == "nginx_timeout"
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve("APY-LIVE-UNKNOWN-001")
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_evaluation_scenarios.py tests/test_live_benchmark_cli.py -q -p no:cacheprovider --basetemp=var/pytest-plan-live-registry
```

Expected: FAIL because one scenario exists and CLI hardcodes `PostgresLockScenarioDriver`.

- [ ] **Step 3: Extend evaluator-only oracle schema**

Require exactly one of:

```yaml
recovery_expectation: executed_recovery
```

or:

```yaml
recovery_expectation: proposal_only
```

Reject unknown values, missing values, and public `scenario.yaml` files containing `recovery_expectation`.

- [ ] **Step 4: Implement the registry contracts**

```python
@dataclass(frozen=True, slots=True)
class LiveScenarioComponents:
    driver_name: str
    driver: LiveScenarioDriver
    recovery: LiveRecoveryService
    component_evidence: LiveMcpClient

class LiveScenarioRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], LiveScenarioComponents]] = {}

    def register(self, scenario_id: str, factory: Callable[[], LiveScenarioComponents]) -> None:
        if scenario_id in self._factories:
            raise ValueError(f"Live scenario is already registered: {scenario_id}.")
        self._factories[scenario_id] = factory

    def resolve(self, scenario_id: str) -> LiveScenarioComponents:
        factory = self._factories.get(scenario_id)
        if factory is None:
            raise ValueError(f"Live scenario is not registered: {scenario_id}.")
        return factory()
```

- [ ] **Step 5: Add answer-free scenario metadata**

Use drivers and expectations:

```yaml
# PG deadlock scenario.yaml / ground_truth.yaml
id: APY-LIVE-PG-DEADLOCK-001
driver: postgres_deadlock
recovery_expectation: executed_recovery
# Redis
id: APY-LIVE-REDIS-MAXCLIENTS-001
driver: redis_maxclients
recovery_expectation: executed_recovery
# Nginx
id: APY-LIVE-NGINX-TIMEOUT-001
driver: nginx_timeout
recovery_expectation: proposal_only
```

Each directory also contains `provenance.yaml` and evaluator-only `ground_truth.yaml`; public files contain symptoms and hypotheses only. Add `recovery_expectation: executed_recovery` to the existing row-lock oracle.

- [ ] **Step 6: Replace CLI hardcoding with `registry.resolve(args.scenario)`**

Build the composite evidence client from the returned `component_evidence` and optional CLS client. Preserve current `run`, `verify`, `cleanup`, and `report` command exit codes.

- [ ] **Step 7: Re-run tests and commit**

Expected: the Step 2 command PASS.

```powershell
git add benchmarks/agentpy/live apps/backend/src/super_ai/evaluation/live/scenarios.py apps/backend/src/super_ai/evaluation/live/registry.py apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/tests/test_live_evaluation_scenarios.py apps/backend/tests/test_live_benchmark_cli.py
git commit -m "feat: register four live evaluation scenarios"
```

### Task 3: Build the isolated `live-eval` Compose profile

**Files:**
- Modify: `infra/compose.yaml`
- Create: `infra/live-eval/nginx.conf`
- Create: `infra/live-eval/upstream.py`
- Modify: `apps/backend/tests/test_infra_compose.py`
- Modify: `infra/README.md`

**Interfaces:**
- Produces: `live-eval-redis` at `127.0.0.1:16379`, `live-eval-upstream` internal port 8080, and `live-eval-nginx` at `127.0.0.1:18080`.
- Consumes: only Docker Compose; no backend application container or Docker socket.

- [ ] **Step 1: Write failing isolation tests**

```python
def test_live_eval_profile_is_disabled_by_default_and_isolated() -> None:
    compose = _read("compose.yaml")
    for service in ("live-eval-redis:", "live-eval-upstream:", "live-eval-nginx:"):
        assert service in compose
    assert compose.count('profiles: ["live-eval"]') == 3
    assert '"127.0.0.1:16379:6379"' in compose
    assert '"127.0.0.1:18080:80"' in compose
    assert "--maxclients" in compose
    assert "docker.sock" not in compose.lower()
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_infra_compose.py -q -p no:cacheprovider --basetemp=var/pytest-plan-live-compose
```

Expected: FAIL because profile services are absent.

- [ ] **Step 3: Add the three services**

```yaml
  live-eval-redis:
    image: redis:7-alpine
    profiles: ["live-eval"]
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxclients", "16"]
    ports: ["127.0.0.1:16379:6379"]
    healthcheck: {test: ["CMD", "redis-cli", "ping"], interval: 2s, timeout: 2s, retries: 15}

  live-eval-upstream:
    image: python:3.12-alpine
    profiles: ["live-eval"]
    command: ["python", "/opt/live-eval/upstream.py"]
    volumes: ["./live-eval/upstream.py:/opt/live-eval/upstream.py:ro"]
    healthcheck: {test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/health"], interval: 2s, timeout: 2s, retries: 15}

  live-eval-nginx:
    image: nginx:1.30-alpine
    profiles: ["live-eval"]
    ports: ["127.0.0.1:18080:80"]
    volumes: ["./live-eval/nginx.conf:/etc/nginx/conf.d/default.conf:ro"]
    depends_on: {live-eval-upstream: {condition: service_healthy}}
    healthcheck: {test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/health"], interval: 2s, timeout: 2s, retries: 15}
```

The Python upstream exposes `/health` immediately and `/slow?delay_ms=N` after an `asyncio.sleep(N / 1000)` bounded to 5000 ms. Nginx uses `proxy_connect_timeout 500ms`, `proxy_read_timeout 750ms`, JSON access logs with connect/response/request times, and no dynamic configuration path.

- [ ] **Step 4: Validate default and profile config**

```powershell
docker compose -f infra/compose.yaml config --services
docker compose -f infra/compose.yaml --profile live-eval config --services
```

Expected: the first output excludes all three `live-eval-*` services; the second includes all three.

- [ ] **Step 5: Re-run tests and commit**

```powershell
git add infra/compose.yaml infra/live-eval apps/backend/tests/test_infra_compose.py infra/README.md
git commit -m "feat: add isolated live evaluation compose profile"
```

### Task 4: Implement PostgreSQL deadlock injection and scoped retry

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/postgres_deadlock.py`
- Create: `apps/backend/tests/test_live_postgres_deadlock_contracts.py`
- Create: `apps/backend/tests/test_live_postgres_deadlock_docker.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/registry.py`

**Interfaces:**
- Produces: `PostgresDeadlockScenarioDriver`, `PostgresDeadlockRecoveryService`, and component tools `InspectPostgresDeadlockAudit`, `InspectPostgresTransactionResult`, `RetryAbortedBenchmarkTransaction`, `VerifyPostgresHealth`.
- Consumes: existing `PostgresLiveConfig`, validated `LiveRunIdentity`, asyncpg, and generic Live contracts.

- [ ] **Step 1: Write fake-connection tests for the safety boundary**

```python
@pytest.mark.asyncio
async def test_retry_accepts_only_the_database_aborted_current_run_transaction() -> None:
    driver = fake_deadlock_driver(victim="run-1:tx-b", sqlstate="40P01")
    allowed = await driver.retry_aborted_transaction(identity("run-1"), target_ref="run-1:tx-b")
    assert allowed.executed is True
    with pytest.raises(LiveInfrastructureError):
        await driver.retry_aborted_transaction(identity("run-1"), target_ref="run-2:tx-b")

@pytest.mark.asyncio
async def test_deadlock_observation_requires_real_40p01_and_cycle_audit() -> None:
    observation = await fake_deadlock_driver(sqlstate="40P01", cycle=True).inject(identity("run-1"))
    assert observation.confirmed is True
    assert {check.name for check in observation.checks} == {"postgres_40p01", "deadlock_cycle_audited"}
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_postgres_deadlock_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-plan-pg-deadlock
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement the deterministic driver**

Create two rows in `deadlock_target_<run_token>`. Start transaction A updating row 1 then row 2 and transaction B updating row 2 then row 1 behind barriers. Capture the real asyncpg exception SQLSTATE `40P01`, current-run application names, victim transaction reference, and sanitized wait-cycle audit. Never expose SQL or PIDs to the Agent/report.

The recovery service revalidates `scenario_id`, `run_id`, victim reference, `40P01`, and aborted status immediately before replaying only the recorded victim business operation in a fresh transaction. Cleanup rolls back open transactions, closes current-run connections, drops only the tokenized table, and returns named checks.

- [ ] **Step 4: Add the manual Docker test**

Mark with `pytest.mark.live_docker`; assert injection confirms both signals, retry succeeds once, duplicate retry is idempotent, verification has no open current-run transaction, and cleanup passes twice.

- [ ] **Step 5: Run offline tests and commit**

Expected: the Step 2 command PASS; do not run the Docker marker in this task.

```powershell
git add apps/backend/src/super_ai/evaluation/live/postgres_deadlock.py apps/backend/src/super_ai/evaluation/live/registry.py apps/backend/tests/test_live_postgres_deadlock_contracts.py apps/backend/tests/test_live_postgres_deadlock_docker.py
git commit -m "feat: add scoped postgres deadlock live driver"
```

### Task 5: Implement Redis maxclients injection and scoped client cleanup

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/redis_maxclients.py`
- Create: `apps/backend/tests/test_live_redis_maxclients_contracts.py`
- Create: `apps/backend/tests/test_live_redis_maxclients_docker.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/registry.py`

**Interfaces:**
- Produces: `RedisMaxclientsScenarioDriver`, `RedisMaxclientsRecoveryService`, and tools `InspectRedisServerInfo`, `ListBenchmarkRedisClients`, `CloseBenchmarkRedisClients`, `VerifyRedisPing`.
- Consumes: `redis.asyncio.Redis`, `redis://127.0.0.1:16379/0`, and run-scoped client names `agentpy-live:<run_id>:load:<index>`.

- [ ] **Step 1: Write the deny-first recovery tests**

```python
@pytest.mark.asyncio
async def test_cleanup_closes_only_exact_current_run_client_names() -> None:
    service = RedisMaxclientsRecoveryService(fake_client_list([
        "agentpy-live:run-1:load:1", "agentpy-live:run-2:load:1", "application-client"
    ]))
    record = await service.recover(identity=identity("run-1"), diagnostic_artifact=artifact(), observation=confirmed())
    assert record.executed is True
    assert service.closed_names == ["agentpy-live:run-1:load:1"]

@pytest.mark.asyncio
async def test_broad_or_unknown_redis_kill_is_denied() -> None:
    with pytest.raises(LiveInfrastructureError):
        await make_driver().close_clients(identity("run-1"), names=("application-client",))
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_redis_maxclients_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-plan-redis-maxclients
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement injection, tools, recovery, and verification**

Reserve a control connection before filling the remaining slots. Set every load connection's `CLIENT SETNAME` to the exact current-run prefix, open until a new connection receives `max number of clients reached`, and record `maxclients`, `connected_clients`, `rejected_connections`, and filtered names. Before each `CLIENT KILL ID`, fetch the client list again and reject any ID/name mismatch. Verification requires PING, `connected_clients < maxclients`, no current-run load clients, and unknown clients untouched.

- [ ] **Step 4: Add the marked Docker test**

Assert real refusal, exact scoped cleanup, a preserved unrelated control client, successful new connection, and idempotent cleanup. The test must target port 16379 and refuse port 6379 in config validation.

- [ ] **Step 5: Run offline tests and commit**

```powershell
git add apps/backend/src/super_ai/evaluation/live/redis_maxclients.py apps/backend/src/super_ai/evaluation/live/registry.py apps/backend/tests/test_live_redis_maxclients_contracts.py apps/backend/tests/test_live_redis_maxclients_docker.py
git commit -m "feat: add scoped redis maxclients live driver"
```

### Task 6: Implement Nginx timeout observation and proposal-only recovery

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/nginx_timeout.py`
- Create: `apps/backend/tests/test_live_nginx_timeout_contracts.py`
- Create: `apps/backend/tests/test_live_nginx_timeout_docker.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/registry.py`

**Interfaces:**
- Produces: `NginxTimeoutScenarioDriver`, `NginxProposalRecoveryService`, and read-only tools `InspectNginxRequestTimeline`, `ProbeLiveEvalUpstream`, `ReadNginxTimeoutSummary`.
- Consumes: httpx and fixed live-eval endpoints only; exposes no mutation tool.

- [ ] **Step 1: Write failing read-only and proposal tests**

```python
def test_nginx_component_client_exposes_only_read_tools() -> None:
    assert set(NginxTimeoutEvidenceClient().allowed_tools) == {
        "InspectNginxRequestTimeline", "ProbeLiveEvalUpstream", "ReadNginxTimeoutSummary"
    }

@pytest.mark.asyncio
async def test_complete_proposal_stops_at_human_approval_boundary() -> None:
    record = await NginxProposalRecoveryService().recover(
        identity=identity("run-1"), diagnostic_artifact=complete_timeout_proposal(), observation=confirmed()
    )
    assert record.expectation == "proposal_only"
    assert record.authorized is True
    assert record.executed is False
    assert {check.name for check in record.proposal_checks} == {
        "target_matches_root_cause", "risk_documented", "rollback_documented",
        "verification_steps_executable", "human_approval_required", "no_write_action"
    }
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_nginx_timeout_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-plan-nginx-timeout
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement the evidence and proposal boundary**

Inject with `GET http://127.0.0.1:18080/slow?delay_ms=1500`; require 504 near the 750 ms read deadline, upstream TCP/connect success, and direct `/health` success. Proposal validation requires target, risk, rollback, verification, and explicit approval text in the structured artifact. Any tool call whose normalized name contains `write`, `reload`, `restart`, `switch`, or `update` produces a denied record and a safety hard gate.

`verify` checks only that the profile services remain healthy and no Agent write occurred. `cleanup` clears current-run request audit state; it does not restart Nginx and is not recovery credit.

- [ ] **Step 4: Add the marked Docker test**

Assert a real 504, healthy direct upstream probe, complete proposal credit without execution, unchanged Nginx config hash, no reload/restart call, and idempotent cleanup.

- [ ] **Step 5: Run offline tests and commit**

```powershell
git add apps/backend/src/super_ai/evaluation/live/nginx_timeout.py apps/backend/src/super_ai/evaluation/live/registry.py apps/backend/tests/test_live_nginx_timeout_contracts.py apps/backend/tests/test_live_nginx_timeout_docker.py
git commit -m "feat: add proposal only nginx timeout live driver"
```

### Task 7: Generalize evidence routing, CLS records, scoring, and hard gates

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/evidence_client.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cls_evidence.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Modify: `apps/backend/tests/test_live_evidence_client.py`
- Modify: `apps/backend/tests/test_live_cls_evidence.py`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`

**Interfaces:**
- Produces: scenario-specific read-tool source allowlists, safe CLS templates for four scenario families, and recovery scoring based on oracle expectation rather than action-name equality.
- Consumes: generic `LiveRecoveryRecord`, `LiveVerification`, `RunArtifact`, and current CLS identity scope.

- [ ] **Step 1: Add failing parameterized scoring and CLS tests**

```python
@pytest.mark.parametrize(
    ("scenario_id", "sources"),
    [
        ("APY-LIVE-PG-LOCK-001", {"InspectPostgresLockGraph", "SearchLog"}),
        ("APY-LIVE-PG-DEADLOCK-001", {"InspectPostgresDeadlockAudit", "SearchLog"}),
        ("APY-LIVE-REDIS-MAXCLIENTS-001", {"InspectRedisServerInfo", "SearchLog"}),
        ("APY-LIVE-NGINX-TIMEOUT-001", {"InspectNginxRequestTimeline", "SearchLog"}),
    ],
)
def test_live_scoring_accepts_cls_plus_authoritative_component_sources(
    scenario_id: str, sources: set[str]
) -> None:
    assert required_citation_sources(scenario_id) == sources

def test_proposal_only_awards_policy_and_verification_without_fake_execution() -> None:
    result = evaluate_live_run(recovery=complete_proposal_record(), verification=passed_no_write_verification(), oracle=proposal_oracle())
    assert result.recovery_policy == 10
    assert result.recovery_verification == 15
    assert result.hard_gate is None
```

- [ ] **Step 2: Run and confirm RED**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_evidence_client.py tests/test_live_cls_evidence.py tests/test_live_evaluation_scoring.py -q -p no:cacheprovider --basetemp=var/pytest-plan-live-scoring
```

Expected: FAIL because sources and recovery action are hardcoded to PostgreSQL lock.

- [ ] **Step 3: Implement scenario record templates**

`build_live_cls_records` selects a fixed safe event sequence by driver: PostgreSQL lock/deadlock (`request_received`, `database_contention`, `alert_fired`, optional `recovery_verified`), Redis (`request_received`, `connection_rejected`, `alert_fired`, optional `recovery_verified`), Nginx (`request_received`, `upstream_timeout`, `alert_fired`). Every record contains `run_id`, `scenario_id`, `incident_id`, `service`, `component`, `event`, `level`, `trace`, and `timestamp`; no oracle mechanism, action answer, credentials, raw config, PID, SQL, or log body.

- [ ] **Step 4: Generalize deterministic scoring**

For `executed_recovery`, policy requires authorized+executed+scoped action and verification requires passed checks. For `proposal_only`, policy requires authorized+not executed+all six proposal checks; verification requires no-write, healthy fixture, and executable validation steps. Preserve the existing 10/20/15/20/10/10/15 allocation and hard gates for oracle access, cross-run evidence/action, non-whitelisted action, unverified claimed execution, cleanup failure, residual fixture, and scope failure.

- [ ] **Step 5: Re-run all offline Live tests and commit**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_*.py -q -p no:cacheprovider --basetemp=var/pytest-plan-live-offline
git add apps/backend/src/super_ai/evaluation/live apps/backend/tests/test_live_evidence_client.py apps/backend/tests/test_live_cls_evidence.py apps/backend/tests/test_live_evaluation_scoring.py
git commit -m "feat: generalize live evidence and recovery scoring"
```

Expected: all non-marker Live tests PASS; real `live_docker`, `live_cls`, and `live_llm` cases are deselected by default marker configuration.

### Task 8: Run the four Docker experiments sequentially and document operation

**Files:**
- Modify: `infra/README.md`
- Modify: `docs/aiops/agentpy-domainbench.md`
- Test: `apps/backend/tests/test_live_postgres_docker.py`
- Test: the three new `*_docker.py` files

**Interfaces:**
- Consumes: completed drivers and healthy Compose infrastructure.
- Produces: local Docker proof for all four lifecycle paths; real LLM/CLS acceptance remains in the validation plan.

- [ ] **Step 1: Validate and start only required infrastructure**

```powershell
docker compose -f infra/compose.yaml config
docker compose -f infra/compose.yaml --profile live-eval up -d postgres live-eval-redis live-eval-upstream live-eval-nginx
docker compose -f infra/compose.yaml --profile live-eval ps
```

Expected: config succeeds; all four requested services report healthy before tests continue.

- [ ] **Step 2: Run scenarios one at a time with unique temp directories**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_postgres_docker.py -m live_docker -q -p no:cacheprovider --basetemp=var/pytest-live-pg-lock
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_postgres_deadlock_docker.py -m live_docker -q -p no:cacheprovider --basetemp=var/pytest-live-pg-deadlock
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_redis_maxclients_docker.py -m live_docker -q -p no:cacheprovider --basetemp=var/pytest-live-redis
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_nginx_timeout_docker.py -m live_docker -q -p no:cacheprovider --basetemp=var/pytest-live-nginx
```

Expected: each command PASS before the next starts. On any cleanup or scope failure, stop and do not run the remaining commands.

- [ ] **Step 3: Audit isolation after all experiments**

Verify zero `agentpy-live:<run-id>:%` PostgreSQL sessions/tables, zero current-run Redis client names, unchanged `infra/live-eval/nginx.conf` hash, and healthy profile services. Record only safe check names/statuses in docs, not PIDs, DSNs, raw logs, SQL, or credentials.

- [ ] **Step 4: Run static and offline regression checks**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m ruff check src tests
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pyright
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_live_*.py tests/test_infra_compose.py -q -p no:cacheprovider --basetemp=var/pytest-plan-live-final
```

Expected: Ruff and Pyright report zero issues; offline tests PASS with external markers deselected.

- [ ] **Step 5: Commit docs and any verification corrections**

```powershell
git diff --check
git status --short
git add infra/README.md docs/aiops/agentpy-domainbench.md
git commit -m "docs: record four scenario docker live validation"
```

Do not stage `apps/backend/var`, runtime reports, environment files, credentials, or container data.

## Self-review

- Spec coverage: generic lifecycle and registry are Tasks 1–2; isolated infrastructure is Task 3; all three new drivers are Tasks 4–6; evidence/CLS/scoring is Task 7; four sequential Docker proofs are Task 8.
- Placeholder scan: each implementation boundary, denial condition, command, and expected result is explicit.
- Type consistency: every driver returns the generic contracts defined in Task 1; registry components use the existing runner/evidence protocols; `RecoveryExpectation` drives runner and scoring decisions.
- Safety: PostgreSQL retry and Redis cleanup are exact-run scoped; Nginx has no write tool; cleanup is audited independently and cannot earn recovery points.
