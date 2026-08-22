# Order Pool Prometheus Auto-Remediation Design

## 1. Objective

Build one evidence-backed, fully automatic, isolated AIOps vertical slice:

```text
fault injection
  -> Prometheus rule evaluation
  -> Alertmanager
  -> authenticated alert ingestion
  -> durable diagnostic job
  -> Single Agent diagnosis with CLS/runtime/PostgreSQL/RAG evidence
  -> deterministic recovery authorization
  -> idempotent restart of live-eval-order-api
  -> independent business/database/monitoring verification
  -> Alertmanager resolved
  -> verified incident closure
```

This design closes the gap between the current alert-ingestion acceptance and a true remediation
loop. The current acceptance proves transport, persistence, diagnosis execution, duplicate
suppression, and resolved handling, but its real report ended with `unsupported_decision`,
`no_action`, and `executionPermitted=false`; the resolved notification was external rather than a
consequence of Agent-authorized recovery.

The first version is intentionally restricted to `APY-LIVE-ORDER-POOL-LEAK-001` and the isolated
Compose service `live-eval-order-api`. It does not grant Docker or recovery permissions to the
resident production Agent.

## 2. Confirmed Product Decisions

- Recovery authority belongs to the Live Eval orchestrator, not the FastAPI process or LLM.
- The final closed-loop acceptance uses Single Agent. Multi-Agent remains available for separate
  Benchmark canaries but is not a variable in this implementation.
- Prometheus must detect the injected fault from service metrics. The publisher script must not
  synthesize the firing or resolved notification in the final acceptance.
- A resolved alert is not proof of remediation. Incident verification is a separate persisted
  state set only after independent post-recovery checks pass.
- The LLM may diagnose and propose; it may not choose the recovery target or grant execution.
- The only executable action is an idempotent restart of `live-eval-order-api`.
- Ordinary alert-ingestion tasks continue to expose `executionPermitted=false`.
- Focused tests and one real closed-loop acceptance are required; a full pytest or full Benchmark
  campaign is outside this change.

## 3. Non-Goals

- Enabling production auto-remediation.
- Giving the backend access to the Docker socket or unrestricted Docker commands.
- Generalizing recovery to arbitrary services, commands, SQL sessions, or Kubernetes resources.
- Replacing Alertmanager, the current background job runtime, or the existing LangGraph graph.
- Enabling Multi-Agent by default or rerunning a Multi 3x3 campaign.
- Adding Grafana, a general incident-management product, or a chaos-engineering control plane.
- Treating cleanup performed by the harness as Agent recovery credit.

## 4. Reuse Assessment

Assessment date: 2026-08-22.

### 4.1 Existing project capabilities

Directly reuse:

- `OrderPoolLeakScenarioDriver` for preflight, baseline, injection, fault confirmation, verification,
  cleanup, and run-scoped safety checks.
- `ComposeServiceRestarter` for the fixed Compose restart and readiness wait.
- `OrderPoolRecoveryService` for component/mechanism/observation authorization.
- `LiveRunIdentity` and deterministic `run_token` derivation.
- Alertmanager, Nginx, authenticated ingestion, PostgreSQL incident idempotency, durable background
  jobs, Single Agent diagnostics, CLS MCP, RAG, reports, and evaluation history.
- `aiops_execution_records` as the final recovery idempotency and uncertain-outcome authority.

### 4.2 GitHub candidates

| Candidate | License and activity | Decision | Rationale |
| --- | --- | --- | --- |
| [prometheus/prometheus](https://github.com/prometheus/prometheus) | Apache-2.0; v3.14.0 released 2026-08-18 | Direct adoption | Fits Compose scraping, rules, Alertmanager routing, and `promtool` validation. |
| [prometheus/client_python](https://github.com/prometheus/client_python) | Apache-2.0; v0.26.0 released 2026-07-24 | Wrapped adoption | Small official instrumentation library; expose only a narrow fixture registry behind `order_api.py`. |
| [chaos-mesh/chaos-mesh](https://github.com/chaos-mesh/chaos-mesh) | Apache-2.0; v2.8.4 released 2026-08-18 | Reference only | Useful experiment/probe lifecycle, but Kubernetes-specific and too heavy for Compose. |
| [litmuschaos/litmus](https://github.com/litmuschaos/litmus) | Apache-2.0; v3.31.0 released 2026-07-15 | Reference only | Useful steady-state and cleanup patterns, but introduces an unnecessary platform. |

Adopt `prom/prometheus:v3.14.0` and `prometheus-client>=0.26.0,<1.0.0`. Do not add another
orchestrator, exporter, Docker SDK, queue, or native binary.

## 5. Architecture and Trust Boundaries

```text
Live Eval Orchestrator
  |-- owns validated LiveRunIdentity
  |-- calls existing driver to establish baseline and inject
  |-- waits for persisted Incident/Task/Report correlation
  |-- applies deterministic recovery contract
  |-- claims stable recovery execution record
  |-- invokes fixed ComposeServiceRestarter target
  `-- independently verifies and records closure

live-eval-order-api --safe metrics--> Prometheus --alerts--> Alertmanager
                                                               |
                                                               v
Nginx --> Alertmanager webhook --> PostgreSQL Incident + Task + Background Job
                                                               |
                                                               v
                                              existing Single Agent LangGraph
                                                | CLS | runtime | PG | RAG
                                                               |
                                                               v
                                                  persisted diagnostic report
```

Trust rules:

1. Alert payloads may carry public correlation labels but never authority.
2. `run_token`, fault token, webhook token, CLS credentials, database credentials, and Docker
   arguments never enter Prometheus labels, Alertmanager payloads, reports, or evaluation archives.
3. The fixed target `live-eval-order-api` comes from code and scenario configuration, never an LLM
   response or webhook field.
4. Prometheus, Alertmanager, FastAPI, LangGraph, and MCP tools receive no Docker write permission.
5. Only a process holding the current trusted Live driver state may request recovery.
6. PostgreSQL execution uniqueness remains authoritative; in-memory flags are only a fast guard.

## 6. Metrics and Alert Contract

`live-eval-order-api` exposes `GET /metrics` in Prometheus text format through the official Python
client. It exports only:

- `agentpy_order_pool_capacity`
- `agentpy_order_pool_checked_out`
- `agentpy_order_pool_free`
- `agentpy_order_pool_waiter_observed`
- `agentpy_order_pool_fault_active`
- `agentpy_order_business_probe_success`

Active-run series contain bounded public labels:

```text
service="order-api"
environment="live-eval"
scenario_id="APY-LIVE-ORDER-POOL-LEAK-001"
run_id="<validated public run ID>"
```

There is at most one active order-pool run, so the label set is bounded. Metrics omit inactive
run-specific series after cleanup.

Prometheus uses a two-second Live Eval scrape interval and evaluates
`OrderApiConnectionPoolExhausted` only when all conditions hold:

```text
fault_active == 1
AND pool_free == 0
AND checked_out == capacity
AND waiter_observed == 1
AND business_probe_success == 0
```

The rule must remain true for a short bounded interval before firing. Recovery or cleanup returns
the condition to false and allows Alertmanager to send resolved. Alert labels are exactly:

```text
alertname="OrderApiConnectionPoolExhausted"
service="order-api"
environment="live-eval"
scenario_id="APY-LIVE-ORDER-POOL-LEAK-001"
run_id="<validated public run ID>"
severity="critical"
```

`scenario_id` becomes a bounded parser label. No execution-related label is accepted.

### 6.1 Trusted CLS scope for the alert-created Task

The automatic closure acceptance uses `evidence_source=cls`. Local evidence remains available for
ordinary, directly invoked Live Benchmarks, but it cannot transfer the orchestrator's in-memory
observation into the resident backend process that owns an Alertmanager-created durable Task.

For the allowlisted order-pool scenario, alert ingestion persists a safe `liveEvidenceScope` in the
Task input. It contains only the already validated `run_id`, `scenario_id`, the deterministic Live
evidence incident identity, and a bounded time window derived from `startsAt`. It contains no CLS
credentials, topic, Region, answer, recovery target, or fault token.

`AiopsDiagnosticService` derives task-local trusted `SearchLog` arguments by combining that persisted
public scope with the backend-owned CLS Region and Topic configuration. The Query binds `run_id`,
`scenario_id`, and `incident_id`. The binding is accepted only when the discovered tool is exactly
`SearchLog` from server `cls` and the official tool JSON Schema accepts the complete argument mapping.
Planner or Replanner output cannot replace Region, Topic, time bounds, identity query, or Limit.

The binding is derived per Task and is never written into mutable singleton state, so concurrent
incidents cannot inherit each other's scope. Missing, malformed, traversing, overlength,
foreign-scenario, non-CLS, or schema-incompatible scope remains fail-closed and cannot fall back to a
wildcard query for automatic recovery authority.

## 7. Persistence and Correlation

Migration `202608220002` extends `aiops_alert_incidents` with nullable, bounded fields:

- `run_id`
- `scenario_id`
- `verification_status`
- `verified_at`
- `verification_summary`

`verification_status` is constrained to `pending`, `passed`, `failed`, or `not_applicable`.
Ordinary incidents use `not_applicable`. An allowlisted Live Eval scenario starts at `pending`.
Alert ingestion can set correlation fields but cannot set verification fields.

Add an owner/source/scenario/run index for exact orchestrator lookup. The lookup must not scan the
latest task or depend on Alertmanager's internal group-key serialization. The orchestrator resolves:

```text
owner_user_id + source_id + scenario_id + run_id
  -> incident_id
  -> diagnostic_task_id
  -> background_job_id and report_id
```

Reuse `aiops_evaluation_runs`, `aiops_evaluation_results`, diagnostic tasks/reports, execution
records, checkpoints, and audits. The evaluation artifact records the Incident, Task, Job, Report,
and Recovery Intent IDs without creating a parallel history subsystem.

## 8. Deterministic Recovery Authorization

Recovery executes only when every predicate is true:

```text
scenario_id == "APY-LIVE-ORDER-POOL-LEAK-001"
component == "order-api"
mechanism == "exception_path_connection_not_released"
fault observation checks == 6/6 passed
evidence sufficiency == "sufficient"
deterministic validator == passed
target == code-owned "live-eval-order-api"
driver owns the matching LiveRunIdentity
```

LLM semantic validation may add confidence but cannot replace a failed deterministic predicate.
An unavailable or failed semantic validator does not grant execution. Any mismatch produces a
non-executing `VALID_FAIL` or `MANUAL_REVIEW` result with a safe authorization code.

The stable recovery execution key includes the diagnostic Task, graph/workflow version, scenario,
action, fixed target, logical attempt, and canonical input fingerprint. The side-effecting execution
record is claimed before invoking Docker Compose.

## 9. Execution Lifecycle

1. **Preflight:** verify PostgreSQL, order-api, Prometheus, Alertmanager, webhook, CLS MCP, RAG, and
   model readiness.
2. **Baseline:** prove the business probe succeeds, the pool is free, and no active alert exists.
3. **Inject:** create the run-scoped order and exhaust all three fixture connections.
4. **Detect:** wait for Prometheus firing and the correlated PostgreSQL Incident.
5. **Diagnose:** wait for the durable Single Agent Task and Report.
6. **Authorize:** evaluate the fixed deterministic recovery contract.
7. **Recover:** claim the recovery execution and restart only `live-eval-order-api`.
8. **Verify:** prove generation changed, old sessions disappeared, the business probe recovered,
   PostgreSQL remains healthy, unrelated sessions remain, and recovery was recorded.
9. **Resolve:** wait for Prometheus recovery and Alertmanager resolved.
10. **Close:** set incident verification to `passed` only after both independent verification and
    resolved are present.
11. **Cleanup:** remove the run-scoped order and residual fixture state on every terminal path.

Stage budgets are:

| Stage | Budget |
| --- | ---: |
| Prometheus detection | 45 seconds |
| Single Agent diagnosis | 360 seconds |
| Recovery execution | 30 seconds |
| Independent verification | 60 seconds |
| Alertmanager resolved | 60 seconds |

The orchestrator polls in short bounded intervals and emits stage updates so a long Agent call does
not appear as a disconnected run.

## 10. Resume, Idempotency, and Uncertain Outcomes

- `run_token` remains deterministically derived from validated `run_id`; no secret resume material is
  persisted.
- Persist safe orchestration stage data, original order-api generation, scoped audit identifiers, and
  correlation IDs. Never persist the fault token.
- Add `--resume <run_id>` to reload the exact evaluation, incident, task, report, and execution state.
- A completed recovery execution is reused.
- After a network timeout, compare the execution record, order-api generation, and old run-scoped
  sessions before deciding whether the restart completed.
- If completion can be proven, record/reuse the completed outcome without restarting.
- If the side-effect outcome cannot be proven, mark the execution `uncertain` and enter
  `MANUAL_REVIEW`; never automatically replay the restart.
- Cleanup is scoped and idempotent. Repeating cleanup may not grant recovery credit.

## 11. Incident Semantics

`status=resolved` means only that Alertmanager delivered a resolved lifecycle. It does not mean the
fault was fixed by the system.

A true successful closure requires:

```text
incident.status == resolved
AND incident.verification_status == passed
AND LiveVerification.passed
AND cleanup audit is clean
```

Resolved before verification remains `pending`. Verification before resolved remains `pending`.
Verification failure sets `failed`; a later retry requires explicit resume and cannot erase the
immutable failure/audit history.

## 12. Failure Classification

- `VALID_PASS`: fault confirmed, correct diagnosis, authorized recovery executed once, independent
  verification passed, alert resolved, and cleanup passed.
- `VALID_FAIL`: infrastructure was valid, but diagnosis, evidence, authorization, recovery,
  verification, resolved wait, or cleanup did not satisfy the contract.
- `INFRA_INVALID`: Prometheus, Alertmanager, webhook, CLS, PostgreSQL, RAG, or model readiness failed;
  do not score this as Agent failure.
- `MANUAL_REVIEW`: a side-effecting recovery outcome is uncertain or the recovery authorization
  cannot be safely reconstructed.

Worker wake failures preserve the already committed job. Duplicate firing does not create another
Task or recovery. An Alertmanager resolved delivery never cancels an in-flight diagnosis.

## 13. Observability and Artifact

Record without credentials or raw payloads:

- stage start/end timestamps;
- Incident, Task, Job, Report, and Recovery Intent IDs;
- MTTD, diagnosis duration, recovery duration, resolved delay, and total MTTR;
- root-cause correctness and evidence coverage;
- model/tool call counts and actual investigation strategy (`single_agent`);
- authorization predicates and safe result codes;
- each independent recovery verification check;
- firing/resolved and incident verification states;
- cleanup audit and resume/reuse decisions.

Extend alert metrics with verified-closure counts and stage latency aggregates. Do not treat cleanup,
an alert disappearance, or a generated proposal as executed recovery.

## 14. Test Strategy

### 14.1 Unit tests

- Safe metric names, labels, values, and inactive-run cleanup.
- No credential, token, raw exception, or authority leakage through `/metrics`.
- `scenario_id` normalization and rejection of nested or disguised authority.
- Exact deterministic authorization predicates and fixed target.
- Stable recovery execution key and reuse.
- Resolved/verification ordering and state transitions.
- Uncertain side effects require manual review and are not replayed.
- Alert-created Task input carries only the bounded public Live evidence scope.
- The resident Single-Agent plan receives one exact CLS `SearchLog` binding for the matching run.
- LLM arguments, foreign run IDs, traversal, non-CLS tools, and concurrent Tasks cannot alter or
  share the trusted scope.

### 14.2 PostgreSQL and API integration

- Migration upgrade/downgrade, checks, lengths, and correlation index.
- Exact owner/source/scenario/run lookup and tenant isolation.
- Concurrent duplicate firing creates one Incident, Task, and Job.
- Resolved and verification arrive in either order and converge safely.
- Unique-conflict transaction recovery remains usable.

### 14.3 Infrastructure contracts

- `promtool check config` and `promtool check rules` pass.
- Compose Live Eval profile starts Prometheus and restricts its targets.
- Healthy baseline does not alert.
- The full conjunctive fault state alerts.
- Fixed Compose restart target and no Docker socket exposure to backend/Prometheus/Alertmanager.

### 14.4 Negative Live cases

- Wrong root cause or insufficient evidence does not recover.
- Simulated restart timeout with unprovable outcome does not restart twice.
- Prometheus, CLS, or Alertmanager unavailable yields `INFRA_INVALID`.

No full pytest is required. Run the new and adjacent focused tests, Ruff, Pyright, Compose render,
Promtool validation, and one real closed-loop acceptance.

## 15. Final Real Acceptance

The accepted run must not call the existing alert publisher. It must prove:

1. baseline business success;
2. real order-pool leak injection;
3. automatic Prometheus firing;
4. Alertmanager webhook returns 202;
5. exactly one correlated Incident, Task, and Job;
6. CLS/runtime/PostgreSQL/RAG evidence reaches one persisted Single Agent report;
7. the root cause is `order-api / exception_path_connection_not_released`;
8. one authorized recovery restart executes;
9. order-api generation changes and all independent verification checks pass;
10. Prometheus recovers and Alertmanager sends resolved;
11. incident verification becomes `passed`;
12. cleanup leaves no active run, test order, old generation session, or unrelated damage.

The terminal result and artifact are persisted under the existing evaluation history contract.
