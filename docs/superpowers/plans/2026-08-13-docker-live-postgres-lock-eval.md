# Docker Live PostgreSQL Lock Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a manually triggered Docker Live Eval that injects a deterministic PostgreSQL lock wait, runs the production diagnostic workflow against live evidence, safely terminates only the current run's synthetic blocker, verifies recovery, cleans up, and produces an answer-isolated scorecard.

**Architecture:** Add a focused `super_ai.evaluation.live` package whose runner owns the lifecycle while protocols isolate scenario driving, evidence, diagnosis, recovery, verification, persistence, and evaluator-only oracle access. The PostgreSQL implementation uses asyncpg and the existing Compose service; the CLI is a thin composition root. Local structured evidence is the default, with an interface reserved for a later CLS implementation.

**Tech Stack:** Python, asyncpg, PostgreSQL 16, Docker Compose, pytest, Ruff, Pyright, OpenSpec

## Global Constraints

- Implement only `APY-LIVE-PG-LOCK-001`; Redis, Nginx and CLS are out of scope.
- Use only `agent_py_live_eval`; never inject into the application or test database.
- Automatically terminate only the current run's exact synthetic blocker after live revalidation.
- Never expose Docker Socket, database credentials, DSNs, raw SQL, raw logs, injection internals, or oracle data to the Agent or report.
- Cleanup must run on success, failure, exception and cancellation, and be idempotent.
- Ordinary CI must remain offline and must not start Docker Live Eval or call real models.
- Reuse existing evaluation persistence, production diagnosis, artifacts and scoring where their contracts fit.
- Add no Python dependency and no chaos framework.
- Implement inline in the current session; do not start subagents.

---

### Task 1: Live scenario and run identity contracts

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/__init__.py`
- Create: `apps/backend/src/super_ai/evaluation/live/domain.py`
- Create: `apps/backend/src/super_ai/evaluation/live/scenarios.py`
- Create: `apps/backend/tests/test_live_evaluation_scenarios.py`
- Create: `benchmarks/agentpy/live/APY-LIVE-PG-LOCK-001/scenario.yaml`
- Create: `benchmarks/agentpy/live/APY-LIVE-PG-LOCK-001/ground_truth.yaml`

**Interfaces:**
- Produces: `LiveScenario`, `LiveRunIdentity`, `LiveRecoveryIntent`, `LiveVerification`, `load_live_scenario(path)`, `validate_run_id(value)` and `load_live_oracle(path)`.
- Consumes: existing `PublicHypothesis`, `RootCause`, and `ScenarioOracle` semantics.

- [ ] Write failing tests for the approved scenario, `live`-only mode, duplicate hypothesis IDs, nested oracle keys, scenario path traversal, invalid run IDs, deterministic run tokens, and evaluator-only oracle loading.
- [ ] Run `uv run pytest tests/test_live_evaluation_scenarios.py -q`; expect import/contract failures.
- [ ] Implement frozen dataclasses and strict YAML loaders. Restrict run IDs to `^[A-Za-z0-9][A-Za-z0-9-]{0,63}$`; derive the table token from a lowercase SHA-256 prefix rather than raw input.
- [ ] Add an answer-free public scenario with hypotheses `postgres_lock_blocking`, `postgres_slow_query_without_lock`, and `postgres_connectivity_failure`. Add an evaluator-only oracle whose primary mechanism is row-lock blocking and whose milestones require wait-event plus blocking-graph evidence.
- [ ] Re-run the Task 1 tests and expect PASS.
- [ ] Commit with `feat: define docker live evaluation scenario`.

### Task 2: Lifecycle runner and cleanup guarantees

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Create: `apps/backend/tests/test_live_benchmark_runner.py`

**Interfaces:**
- Consumes: `LiveScenario`, `LiveRunIdentity`, existing `AgentVersion`, `RunArtifact`, `EvaluationPersistence`, and `BenchmarkRunError` categories.
- Produces: `LiveScenarioDriver`, `LiveDiagnosticAdapter`, `LiveRecoveryService`, `LiveEvaluator` protocols and `LiveBenchmarkRunner.run(...)`.

- [ ] Write fake-driven failing tests for the exact phase order: preflight, baseline probe, inject, independent fault confirmation, diagnose, plan recovery, authorize/execute, verify, evaluate, finalize, cleanup.
- [ ] Add failure-path tests for preflight, injection, insufficient confirmation, diagnosis, recovery denial, recovery execution, verification, evaluator, persistence and cleanup. Assert cleanup runs once in `finally`, cancellation is re-raised after cleanup, and cleanup failure overrides a nominal pass with `cleanup_failed`.
- [ ] Run `uv run pytest tests/test_live_benchmark_runner.py -q`; expect missing runner failures.
- [ ] Implement the minimal runner state machine with typed phase results and safe failure categories. Do not load ground truth until after the Agent artifact exists and recovery/verification finish.
- [ ] Re-run tests and expect PASS.
- [ ] Commit with `feat: orchestrate docker live evaluation lifecycle`.

### Task 3: Recovery planning and policy hard gates

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/recovery.py`
- Create: `apps/backend/tests/test_live_recovery_policy.py`

**Interfaces:**
- Consumes: structured Agent root-cause decision, current lock graph, `LiveRunIdentity`, injected blocker PID and executor PID.
- Produces: `PostgresRecoveryPlanner.plan(...)`, `PostgresRecoveryPolicy.authorize(...)`, `RecoveryAuthorization`, and stable denial codes.

- [ ] Write failing planner tests proving no intent is generated for the wrong mechanism, ambiguous blockers, missing lock graph or missing decision. A unique live blocker produces `terminate_postgres_backend` with the observed PID.
- [ ] Write parameterized policy tests covering allow plus denials for action, missing/stale PID, wrong database, wrong `application_name`, cross-run target, executor PID, waiter PID, system process, no current blocking edge and mismatch with injected PID.
- [ ] Run `uv run pytest tests/test_live_recovery_policy.py -q`; expect missing contracts.
- [ ] Implement pure planner/policy functions. Policy input must be a fresh `PostgresSessionState`; it must not trust the intent's reason text.
- [ ] Re-run tests and expect PASS.
- [ ] Commit with `feat: enforce synthetic postgres recovery policy`.

### Task 4: PostgreSQL lock driver, evidence and recovery executor

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/postgres.py`
- Create: `apps/backend/tests/test_live_postgres_contracts.py`
- Create: `apps/backend/tests/test_live_postgres_docker.py`

**Interfaces:**
- Consumes: validated `LiveRunIdentity`, asyncpg connection factory, recovery authorization.
- Produces: `PostgresLockScenarioDriver`, `LocalDockerPostgresEvidenceCollector`, `PostgresRecoveryExecutor`, `PostgresFaultObservation`, structured safe evidence and idempotent cleanup.

- [ ] Write offline failing tests with fake asyncpg connections for generated identifiers, application names, statement timeouts, structured evidence redaction, dual confirmation, executor authorization requirement and cleanup scoping.
- [ ] Run `uv run pytest tests/test_live_postgres_contracts.py -q`; expect missing implementation.
- [ ] Implement the driver using parameterized values and project-generated identifiers. Keep blocker/waiter connections private; expose only typed observations.
- [ ] Implement queries for `pg_stat_activity`, `pg_blocking_pids`, lock summaries and `pg_terminate_backend`, with database/application-name predicates repeated at execution time.
- [ ] Add `@pytest.mark.live_docker` tests that create a real blocker/waiter, prove both confirmation signals, terminate the authorized blocker, verify the probe succeeds and call cleanup twice.
- [ ] Run offline tests and expect PASS. Do not run the Docker marker until Task 8.
- [ ] Commit with `feat: drive postgres lock live experiment`.

### Task 5: Live diagnostic evidence adapter and recovery artifact

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Create: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Consumes: `AiopsDiagnosticService`, `LocalDockerPostgresEvidenceCollector`, existing repositories/retrieval tool and current 30-card RAG.
- Produces: `ApplicationLiveDiagnosticAdapter` and a `RunArtifact(mode="live")` extended with approved recovery and verification facts without exposing injector state.

- [ ] Write failing tests that the Agent receives public scenario plus collector tools only, `benchmarkMode=live`, no oracle/DSN/PID ownership hint, and that persisted tool calls/evidence become the artifact.
- [ ] Add tests that recovery facts are appended by the runner/executor boundary, not synthesized by the Agent, and preserve L1 `approved`/`verified` audit semantics.
- [ ] Run `uv run pytest tests/test_live_diagnostic_adapter.py -q`; expect missing adapter/fields.
- [ ] Implement the adapter and the smallest backwards-compatible artifact extension needed for injection confirmation and recovery verification.
- [ ] Re-run existing Snapshot artifact/runner tests together with the new tests; expect PASS.
- [ ] Commit with `feat: connect live evidence to diagnostic workflow`.

### Task 6: Live scoring and safe CLI

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Create: `apps/backend/scripts/run_live_benchmark.py`
- Create: `apps/backend/tests/test_live_evaluation_scoring.py`
- Create: `apps/backend/tests/test_live_benchmark_cli.py`

**Interfaces:**
- Consumes: completed live artifact, evaluator-only oracle, injection confirmation, recovery authorization/execution and `LiveVerification`.
- Produces: 100-point live scorecard, hard gates, `run`, `verify`, `cleanup`, `report` commands, and exit codes 0/1/2.

- [ ] Write failing scoring tests for the exact 10/20/15/20/10/10/15 allocation and each hard gate: oracle access, non-whitelisted action, cross-run termination, unverified recovery, residual blocker, cleanup failure and scope failure.
- [ ] Write failing CLI tests for subcommand parsing, explicit scenario/run ID, safe JSON, no DSN/password/raw logs/oracle, and exit codes.
- [ ] Run the two new test files; expect missing modules.
- [ ] Implement deterministic live scoring as an adapter around reusable diagnosis/evidence facts; injection failure remains infra failure rather than Agent score loss.
- [ ] Implement a thin CLI composition root. `run` performs full lifecycle; `verify`, `cleanup`, and `report` use validated IDs and persistence/driver services.
- [ ] Re-run tests and expect PASS.
- [ ] Commit with `feat: score and run postgres live benchmark`.

### Task 7: Compose isolation, markers, OpenSpec and operator docs

**Files:**
- Modify: `infra/postgres/init/001-create-test-database.sql`
- Modify: `infra/compose.yaml`
- Modify: `apps/backend/pyproject.toml`
- Modify: `apps/backend/tests/test_infra_compose.py`
- Modify: `apps/backend/tests/test_environment_examples.py`
- Create: `openspec/changes/add-docker-live-postgres-lock-eval/.openspec.yaml`
- Create: `openspec/changes/add-docker-live-postgres-lock-eval/proposal.md`
- Create: `openspec/changes/add-docker-live-postgres-lock-eval/design.md`
- Create: `openspec/changes/add-docker-live-postgres-lock-eval/tasks.md`
- Create: `openspec/changes/add-docker-live-postgres-lock-eval/specs/live-sre-evaluation/spec.md`
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `infra/README.md`

**Interfaces:**
- Produces: isolated database initialization, `live_docker` marker, normative contracts and exact manual commands.

- [ ] Write failing infrastructure tests requiring `agent_py_live_eval`, explicit health/preflight expectations, no Docker Socket mount, and exclusion of `live_docker` from ordinary pytest/CI.
- [ ] Run focused tests and observe RED.
- [ ] Add database initialization and marker configuration. Do not add a permanent chaos container or expose ports beyond the existing local PostgreSQL binding in this slice.
- [ ] Write OpenSpec requirements for answer isolation, dual-signal injection, recovery allowlist, cleanup, safe reports, local/CLS collector boundary and manual-only execution.
- [ ] Document setup, run/verify/cleanup/report, expected runtime, failure recovery and the statement that CLS is deferred.
- [ ] Run focused tests and `openspec validate add-docker-live-postgres-lock-eval --strict`; expect PASS.
- [ ] Commit with `docs: define postgres docker live evaluation`.

### Task 8: Verification and real Docker experiment

**Files:**
- Test: all files modified above
- Runtime output: `apps/backend/var/benchmarks/` remains ignored and must not be staged

**Interfaces:**
- Consumes: completed offline implementation and local Docker Compose.
- Produces: verified offline contracts plus one safe manual Live report.

- [ ] Run focused offline tests for all live modules plus existing Snapshot/Retrieval regressions.
- [ ] Run `uv run ruff check .`, `uv run pyright`, relevant OpenSpec validation and `docker compose -f infra/compose.yaml config`.
- [ ] Resolve the absolute Compose project and database targets; start only required PostgreSQL infrastructure and wait for health.
- [ ] Run the real `live_docker` PostgreSQL contract test. Confirm injection, two signals, authorized termination, recovery and double cleanup.
- [ ] Run the CLI infrastructure lifecycle without real LLM first. If it passes, run one real Agent/LLM Live Eval using the current configured model and RAG; save only the safe ignored report.
- [ ] Audit PostgreSQL for zero `agentpy-live:<run_id>:%` sessions and zero current-run tables. If cleanup fails, stop and report the exact safe residual type.
- [ ] Inspect `git diff`, `git status`, staged files and report for secrets, DSNs, raw logs, oracle text and runtime artifacts.
- [ ] Commit verification/docs adjustments only; do not commit runtime reports or credentials.

## Self-review

- Spec coverage: every architecture, security, lifecycle, scoring, CLI, Compose, CLS boundary and verification requirement maps to a task.
- Type consistency: runner, planner, policy, driver, collector, executor, adapter and scoring interfaces have stable names across tasks.
- Scope: only one PostgreSQL lock scenario is implemented; Redis, Nginx, CLS and generic chaos frameworks remain excluded.
- Safety: live target identity is revalidated immediately before termination and cleanup cannot remove broad resources.
- Placeholder scan: all behavior and commands are specified without deferred implementation placeholders.
