# Production Recovery Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository policy permits exactly one read-only plan-review subagent before implementation; implementation remains single-agent.

**Goal:** 将已持久化的 AIOps 诊断提案安全地转化为可审批、幂等、可验证、可审计的生产恢复闭环，第一版仅支持受控 Compose 服务重启和 PostgreSQL blocker 终止。

**Architecture:** 诊断 LangGraph 只产出证据与恢复提案；新的 `RecoveryIntentService` 从 owner-scoped 持久化事实创建不可变 Intent，并由 PostgreSQL 状态机、durable Background Job 和现有 `ExecutionCoordinator` 执行。所有目标和参数均由服务端白名单与 fresh preflight 事实派生；副作用最多执行一次，未知结果转人工，恢复成功必须通过独立 verifier 与 Alertmanager resolved。

**Tech Stack:** Python 3.10、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL、asyncio subprocess、Docker Compose CLI、asyncpg/SQLAlchemy 参数化查询、Vue 3 typed contracts、pytest、Ruff、strict Pyright、OpenSpec。

## Global Constraints

- 不把生产写工具加入 LangGraph、Chat Agent、MCP 或前端可调用工具列表。
- 不读取 benchmark scenario、ground truth、Live Eval oracle、注入 PID 或测试专用授权信息。
- `productionRecovery.enabled` 默认 `false`，提交的 Compose/PostgreSQL 白名单默认空。
- 第一版仅允许 `restart_compose_service` 和 `terminate_postgres_blocker`；不得接受任意命令、路径、服务名、连接串、SQL、PID 或附加 flags。
- Compose 自动恢复只允许白名单目标显式 `automaticRecoveryEnabled=true`；PostgreSQL blocker 终止始终需要当前 Incident owner 的 10 分钟有效审批。
- Background Job 可以恢复调度，但副作用不自动重试；未知执行结果、身份漂移或验证失败必须转 `manual_intervention`。
- PostgreSQL 唯一约束是幂等最终真相；不新增 Redis 锁或外部工作流依赖。
- 配置只来自 `config/project.json` 与 `config/user.project.json`；模板不得包含凭据、主机详情或真实绝对路径。
- API、审计、事件和日志不得泄露连接串、Compose 绝对路径、原始 SQL、PID、凭据、原始异常或完整工具输出。
- 所有读写按认证用户 owner scope；跨 owner 资源统一表现为 not-found/forbidden，不可侧信道枚举。
- 共享 HTTP 类型先修改 `packages/api-contracts`，后端使用统一 envelope 和错误目录。
- 新 schema 使用 Alembic revision `202608230001`；前端 Agent 配置计划改用 `202608230002`。
- TDD 实施；每项任务只运行风险相称的聚焦测试，最终再运行计划列出的集成验收。

## Reuse Assessment

- **Direct adoption:** `BackgroundJobRuntime`、`ExecutionCoordinator.run_once`、`SQLAlchemyAiopsExecutionRepository`、Alert Incident 生命周期、owner-scoped repositories、统一错误 envelope 与 `recovery.execute` rate-limit 模式。
- **Wrapped adoption:** 从 Live Eval 的 Compose/PostgreSQL recovery 中提取“不依赖 scenario/oracle”的 argv、preflight 与 verification 思路，重新实现为 production module。
- **Reference only:** StackStorm/st2（Apache-2.0）的 action 状态与脱敏审计、Robusta（MIT）的显式 remediation playbook、Argo Workflows（Apache-2.0）的 suspend/resume；不复制代码、不新增服务。
- **Custom implementation:** 有界 `RecoveryIntent` 状态机、审批绑定、production executor registry 和公开审计投影，因为现有项目已有持久 job/execution 基础，接入通用自动化平台会引入不必要的第二控制面。

## File Map

- `apps/backend/src/super_ai/recovery/contracts.py`: 恢复领域类型、状态、公开检查与错误码。
- `apps/backend/src/super_ai/recovery/config.py`: 只读生产恢复配置和白名单解析。
- `apps/backend/src/super_ai/recovery/policy.py`: 创建与执行前确定性授权，不访问执行器。
- `apps/backend/src/super_ai/recovery/proposal_adapter.py`: 将已验证 component/mechanism/evidence 确定性映射为动作码与白名单目标。
- `apps/backend/src/super_ai/recovery/repository.py`: Recovery Intent/Approval/Audit Protocol。
- `apps/backend/src/super_ai/recovery/sqlalchemy.py`: PostgreSQL 原子状态转换与 owner scope。
- `apps/backend/src/super_ai/recovery/intent_service.py`: 从诊断持久化事实创建不可变 Intent。
- `apps/backend/src/super_ai/recovery/compose.py`: Compose fresh facts、单次副作用与验证。
- `apps/backend/src/super_ai/recovery/postgres.py`: blocker fresh probe、参数化终止与验证。
- `apps/backend/src/super_ai/recovery/worker.py`: durable job handler、ExecutionCoordinator 和状态机编排。
- `apps/backend/src/super_ai/recovery/api.py`: owner-scoped HTTP API 与事件读取。
- `apps/backend/src/super_ai/recovery/__init__.py`: 公开构造接口，不建立外部连接。

---

### Task 1: 创建 OpenSpec change 并固定安全闭环契约

**Files:**
- Create: `openspec/changes/add-production-recovery-execution/proposal.md`
- Create: `openspec/changes/add-production-recovery-execution/design.md`
- Create: `openspec/changes/add-production-recovery-execution/tasks.md`
- Create: `openspec/changes/add-production-recovery-execution/specs/production-recovery/spec.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-08-23-production-recovery-execution-design.md` and current AIOps/background-job specs.
- Produces: the normative state machine, owner authorization, executor allowlist, at-most-once and verification requirements used by Tasks 2-11.

- [ ] **Step 1: Write the delta requirements**

Include exact requirements and scenarios for: immutable server-derived Intent, global-off default, Compose auto gate, PostgreSQL mandatory approval, ten-minute TTL, owner isolation, cancellation before execution claim only, unknown-outcome manual intervention, append-only audit, and multi-signal verification. The core scenario must read:

```markdown
### Requirement: Side effects execute at most once
系统 SHALL 使用 PostgreSQL 唯一 execution key 和 side-effecting execution claim 协调恢复动作。执行开始后若结果无法确认，系统 MUST 转入 `manual_intervention`，并 MUST NOT 自动重放该动作。

#### Scenario: Worker loses contact after dispatch
- **WHEN** Worker 已进入 `executing` 且执行器返回超时或连接中断，无法证明动作未发生
- **THEN** Intent MUST 进入 `manual_intervention`
- **AND** 相同 execution key 的后续 Worker MUST NOT 再次调用执行器
```

- [ ] **Step 2: Add an OpenSpec task checklist matching Tasks 2-11**

Use checkbox entries for contracts, migration, repository, policy, executors, worker, API, Live acceptance, documentation, and archive readiness. Do not mark implementation tasks complete yet.

- [ ] **Step 3: Validate the change**

Run: `openspec validate add-production-recovery-execution --strict`

Expected: validation succeeds with every Requirement containing at least one Scenario.

- [ ] **Step 4: Commit**

```bash
git add openspec/changes/add-production-recovery-execution
git commit -m "docs: specify production recovery execution"
```

---

### Task 2: 定义共享 API 契约、领域类型与安全错误目录

**Files:**
- Create: `packages/api-contracts/src/recovery.ts`
- Modify: `packages/api-contracts/src/index.ts`
- Modify: `packages/api-contracts/src/openapi.ts`
- Modify: `packages/api-contracts/src/errors.ts`
- Test: `packages/api-contracts/tests/api-contracts.test.ts`
- Create: `apps/backend/src/super_ai/recovery/contracts.py`
- Create: `apps/backend/src/super_ai/recovery/__init__.py`
- Test: `apps/backend/tests/test_recovery_contracts.py`

**Interfaces:**
- Produces: `RecoveryAction`, `RecoveryStatus`, `RecoveryIntent`, `RecoveryCheck`, `RecoveryAuditEvent`, `CreateRecoveryIntentResponse`, `ApproveRecoveryIntentRequest`; Python `RecoveryIntentRecord`, `RecoveryPolicyDecision`, `RecoveryExecutionResult`, `RecoveryVerificationResult`.
- Consumes: ISO timestamps, success/error envelope, existing Background Job summary.

- [ ] **Step 1: Write failing TypeScript contract tests**

```ts
const intent: RecoveryIntent = {
  id: "recovery_1",
  incidentId: "incident_1",
  diagnosticTaskId: "diagnostic_1",
  reportId: "report_1",
  action: "restart_compose_service",
  targetKey: "live-eval-order-api",
  riskTier: "low",
  automaticEligible: true,
  approvalRequired: false,
  status: "queued",
  proposalFingerprint: "a".repeat(64),
  createdAt: "2026-08-23T08:00:00Z",
  approvalExpiresAt: null,
  startedAt: null,
  completedAt: null,
  safeReasonCode: null,
  executionSummary: null,
  verification: []
};
expect(intent.status).toBe("queued");
```

Also assert that public types have no `command`, `composePath`, `connectionString`, `sql`, `pid`, or raw exception fields.

- [ ] **Step 2: Run the contract test and observe missing exports**

Run: `npm --workspace packages/api-contracts run test`

Expected: FAIL because `RecoveryIntent` and recovery error codes do not exist.

- [ ] **Step 3: Implement exact closed unions and exports**

```ts
export type RecoveryAction = "restart_compose_service" | "terminate_postgres_blocker";
export type RecoveryStatus =
  | "proposed" | "awaiting_approval" | "queued" | "revalidating"
  | "executing" | "verifying" | "recovered" | "denied" | "rejected"
  | "expired" | "cancelled" | "verification_failed" | "manual_intervention";
export interface RecoveryCheck {
  key: string;
  status: "passed" | "failed" | "pending";
  safeSummary: string;
  checkedAt: string | null;
}
```

Add stable error codes: `RECOVERY_DISABLED`, `RECOVERY_NOT_ELIGIBLE`, `RECOVERY_APPROVAL_REQUIRED`, `RECOVERY_APPROVAL_EXPIRED`, `RECOVERY_CONFIRMATION_MISMATCH`, `RECOVERY_INVALID_TRANSITION`, `RECOVERY_EXECUTION_UNCERTAIN`, `RECOVERY_TARGET_CHANGED`.

- [ ] **Step 4: Write and implement Python contract tests**

Assert `RecoveryIntentRecord` is frozen, `canonical_json()` sorts mapping keys, `proposal_fingerprint(...)` is stable under key order, and `public_payload()` omits private arguments and trusted facts.

- [ ] **Step 5: Run type and unit checks**

Run: `npm run contracts:typecheck && npm --workspace packages/api-contracts run test`

Expected: PASS.

Run from `apps/backend`: `uv run pytest tests/test_recovery_contracts.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/api-contracts apps/backend/src/super_ai/recovery apps/backend/tests/test_recovery_contracts.py
git commit -m "feat: define production recovery contracts"
```

---

### Task 3: 加入安全默认配置和受控目标解析

**Files:**
- Create: `apps/backend/src/super_ai/recovery/config.py`
- Modify: `config/project.template.json`
- Modify: `config/user.project.template.json`
- Test: `apps/backend/tests/test_recovery_config.py`
- Modify: `docs/installation.md` or the existing configuration reference discovered during implementation.

**Interfaces:**
- Produces: `ProductionRecoverySettings`, `ComposeRecoveryTarget`, `PostgresRecoveryTarget`, `load_production_recovery_settings(config_path)`.
- Consumes: `load_project_config`, project root and named existing database configuration identifiers.

- [ ] **Step 1: Write failing configuration tests**

Cover missing section -> disabled/empty; enabled without targets; duplicate target keys; `approvalTtlSeconds` outside 60..3600; relative/out-of-root Compose file; shell metacharacters in service; absolute health URL other than `http://127.0.0.1:<port>/...`; empty/duplicate diagnostic selectors; and a valid isolated target. A selector is a closed server configuration containing `component`, `mechanism`, required evidence fact keys and the target key; it never contains a command, PID or model-generated prose.

```python
settings = load_production_recovery_settings(config_path)
assert settings.enabled is False
assert settings.approval_ttl_seconds == 600
assert settings.compose_targets == {}
assert settings.postgres_targets == {}
```

- [ ] **Step 2: Run and observe missing loader**

Run from `apps/backend`: `uv run pytest tests/test_recovery_config.py -q`

Expected: FAIL on missing `super_ai.recovery.config`.

- [ ] **Step 3: Implement frozen validated settings**

Use `Path.resolve(strict=False)` plus `is_relative_to(project_root.resolve())`; service and target keys must match `^[a-z0-9][a-z0-9_.-]{0,95}$`. Store a private resolved path in the server-side target, but `public_summary()` returns only `targetKey`, `service`, auto flag and verification capability names. Validate diagnostic selectors at startup and reject two targets matching the same `(component, mechanism)` pair, so proposal routing cannot be ambiguous.

- [ ] **Step 4: Add safe templates**

```json
"productionRecovery": {
  "enabled": false,
  "approvalTtlSeconds": 600,
  "composeTargets": [],
  "postgresTargets": []
}
```

Do not put a real path, DSN, owner, credential, database host or service secret in either template.

- [ ] **Step 5: Verify**

Run from `apps/backend`: `uv run pytest tests/test_recovery_config.py -q && uv run ruff check src/super_ai/recovery/config.py tests/test_recovery_config.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/recovery/config.py apps/backend/tests/test_recovery_config.py config/*.template.json docs
git commit -m "feat: configure allowlisted recovery targets"
```

---

### Task 4: 新增 Recovery schema、兼容迁移与 Repository Protocol

**Files:**
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Create: `apps/backend/alembic/versions/202608230001_add_production_recovery.py`
- Create: `apps/backend/src/super_ai/recovery/repository.py`
- Create: `apps/backend/src/super_ai/recovery/sqlalchemy.py`
- Test: `apps/backend/tests/test_recovery_migration.py`
- Test: `apps/backend/tests/test_postgresql_recovery_repository.py`

**Interfaces:**
- Produces: `RecoveryIntentRepository.create_or_get_active`, `get_owned`, `transition`, `reject`, `cancel_before_claim`, `append_event`, `list_events`; transaction-owning `create_intent_with_job_and_event` and `approve_with_job_and_event`; `RecoveryRepositoryProvider` in `MemoryRepositories`.
- Consumes: `User`, `AlertIncidentModel`, `DiagnosticTaskModel`, `DiagnosticReportModel`, `BackgroundJobModel`, existing `RecoveryApprovalRequestModel` rows.

- [ ] **Step 1: Write migration tests**

Upgrade from `202608220007`, insert a legacy `recovery_approval_requests` record first, then assert upgrade preserves it and creates:

```text
production_recovery_intents
production_recovery_approvals
production_recovery_audit_events
```

Assert downgrade removes only the new tables. The migration MUST NOT rewrite or delete `recovery_approval_requests`.

- [ ] **Step 2: Define schema constraints**

`production_recovery_intents` includes immutable references/parameters, closed action/risk/status checks, 64-char proposal/execution fingerprints, timestamps, safe summaries, JSONB trusted snapshots, and optional job ID. Add a partial unique index:

```sql
UNIQUE (owner_user_id, proposal_fingerprint)
WHERE status IN ('proposed','awaiting_approval','queued','revalidating','executing','verifying')
```

Approvals are one-to-many audit records bound to fingerprint; audit events use unique stable `event_id` and monotonically ordered `sequence` per intent.

- [ ] **Step 3: Run migration test red then green**

Run from `apps/backend`: `uv run pytest tests/test_recovery_migration.py -q`

Expected before implementation: FAIL; after model/migration implementation: PASS.

- [ ] **Step 4: Write repository concurrency and owner-isolation tests**

Use real PostgreSQL fixtures to assert concurrent create yields one active Intent; concurrent approve yields one `queued` transition; expired approve yields `expired`; cross-owner get returns `None`; cancellation loses once execution claim changes status; event IDs deduplicate; Intent + job + first event creation is atomic. Inject a failure after each constituent insert/update and assert rollback leaves no `queued` Intent without a job, job without Intent, approval without queued transition, or missing first audit event.

- [ ] **Step 5: Implement conflict-safe repository**

Use PostgreSQL `INSERT ... ON CONFLICT DO NOTHING`, `SELECT ... FOR UPDATE`, expected-status predicates, and transaction rollback before conflict-safe reread. Do not catch broad exceptions to guess conflicts. The two composite methods own one `AsyncSession.begin()` and write the `BackgroundJobModel` row directly through a shared internal SQLAlchemy mapper; they MUST NOT call the existing `SQLAlchemyBackgroundJobRepository.enqueue()` because that method opens and commits a separate Session. The standalone Background Job repository remains unchanged for non-recovery jobs.

- [ ] **Step 6: Verify repository and migration**

Run from `apps/backend`: `uv run pytest tests/test_recovery_migration.py tests/test_postgresql_recovery_repository.py -q`

Expected: PASS when test PostgreSQL is available; otherwise report the environment dependency, do not add skips.

- [x] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/memory apps/backend/src/super_ai/recovery apps/backend/alembic/versions/202608230001_add_production_recovery.py apps/backend/tests/test_recovery_migration.py apps/backend/tests/test_postgresql_recovery_repository.py
git commit -m "feat: persist recovery intent state machine"
```

---

### Task 5: 从真实诊断事实创建 Intent 并执行确定性策略门

**Files:**
- Create: `apps/backend/src/super_ai/recovery/proposal_adapter.py`
- Create: `apps/backend/src/super_ai/recovery/policy.py`
- Create: `apps/backend/src/super_ai/recovery/intent_service.py`
- Test: `apps/backend/tests/test_recovery_proposal_adapter.py`
- Test: `apps/backend/tests/test_recovery_policy.py`
- Test: `apps/backend/tests/test_recovery_intent_service.py`

**Interfaces:**
- Produces: `RecoveryProposalAdapter.resolve(validated_decision, evidence, settings) -> RecoveryProposal | None`; `RecoveryPolicy.evaluate_creation(facts, settings) -> RecoveryPolicyDecision`; `RecoveryPolicy.evaluate_execution(fresh_facts, intent, settings, approval) -> RecoveryPolicyDecision`; `RecoveryIntentService.create(owner_user_id, diagnostic_task_id, note) -> RecoveryIntentRecord`.
- Consumes: owner-scoped Diagnostic task/report/evidence, Incident linked by `diagnostic_task_id`, validator/policy fields from persisted result, configuration target registry, repository Task 4.

- [ ] **Step 1: Write policy table tests**

Parameterize every hard gate: diagnostic incomplete, report missing, Incident missing/resolved, insufficient evidence, deterministic validator failed, proposal/action mismatch, target not configured, global disabled, Compose auto disabled, and PostgreSQL high-risk. Assert only exact low-risk Compose facts produce `queued`; PostgreSQL always produces `awaiting_approval`; unsafe identity produces `denied`/safe code rather than approval bypass. Add a second table for `evaluate_execution`: Incident becomes resolved, global switch turns off, target disappears, auto flag turns off, report/evidence/fingerprint changes, approval expires or fingerprint changes. Every row must deny before the side-effect claim and record zero executor calls.

- [ ] **Step 2: Write injection and immutability tests**

Call create with only `{diagnostic_task_id, note}`. Add malicious action/path/PID fields to the original diagnostic input and assert the service ignores them, deriving `action`, `target_key`, canonical arguments and evidence IDs only from persisted report/evidence plus allowlist mapping.

- [ ] **Step 3: Implement a deterministic proposal adapter instead of trusting recovery prose**

Read the persisted validated Decision `component` and `mechanism`, deterministic-validator origin/status, and immutable evidence fact keys. Match them to exactly one server-configured diagnostic selector. The selector chooses the closed action and `target_key`; LLM `RecoveryPlan.action`, `target`, command-like prose and client fields are advisory only and MUST NOT select an executor.

```python
proposal = adapter.resolve(validated_decision, evidence, settings)
assert proposal.action in {"restart_compose_service", "terminate_postgres_blocker"}
assert proposal.target_key in settings.all_target_keys
```

Add contract fixtures built from the persisted report shapes of `APY-LIVE-ORDER-POOL-LEAK-001` and `APY-LIVE-PG-LOCK-001`; both must resolve to the intended action/target without reading scenario ID, run ID, ground truth or injected PID. A free-text-only plan, unsupported mechanism, missing required fact, or ambiguous selector returns `None` and cannot create an executable Intent.

- [ ] **Step 4: Implement stable creation**

Compute the fingerprint from owner, Incident, diagnostic, report, action, target, canonical safe arguments and sorted evidence IDs. Store trusted details privately; return only `RecoveryIntentRecord.public_payload()`.

- [ ] **Step 5: Verify**

Run from `apps/backend`: `uv run pytest tests/test_recovery_proposal_adapter.py tests/test_recovery_policy.py tests/test_recovery_intent_service.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/recovery apps/backend/tests/test_recovery_policy.py apps/backend/tests/test_recovery_intent_service.py
git commit -m "feat: derive recovery intents from grounded diagnostics"
```

---

### Task 6: 实现 Compose 单次重启执行器与独立验证器

**Files:**
- Create: `apps/backend/src/super_ai/recovery/compose.py`
- Test: `apps/backend/tests/test_compose_recovery_executor.py`
- Test: `apps/backend/tests/test_compose_recovery_verifier.py`

**Interfaces:**
- Produces: `ComposeRecoveryExecutor.preflight`, `execute_once`, `verify`; typed trusted fact/result objects.
- Consumes: server-resolved `ComposeRecoveryTarget`, an injected argv runner, injected HTTP health/business probes, injected Incident status reader.

- [ ] **Step 1: Write argv and target-boundary tests**

Assert the only subprocess argv is:

```python
["docker", "compose", "-f", str(target.compose_file), "restart", target.service]
```

Assert `shell=False`, fixed timeout, no user flags, and public results contain no resolved path/stdout/stderr.

- [ ] **Step 2: Write preflight and unknown-outcome tests**

Fresh `docker compose ps --format json` facts must match configured project/service and produce a container identity snapshot. A timeout after process start returns `outcome_known=False`; invalid/missing identity fails before side effect with `outcome_known=True`.

- [ ] **Step 3: Implement injectable subprocess runner**

Use `asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)` and `asyncio.wait_for`. On timeout terminate the process tree using the existing cross-platform process helper if one exists; otherwise add a focused helper in this file for Windows/POSIX without invoking a shell. Return only exit category and duration.

- [ ] **Step 4: Write verification matrix tests**

Require all checks: container identity/start time changed, health endpoint success, business probe success, and Incident status resolved. One failure yields `verification_failed`; no verifier calls a second restart.

- [ ] **Step 5: Verify**

Run from `apps/backend`: `uv run pytest tests/test_compose_recovery_executor.py tests/test_compose_recovery_verifier.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/recovery/compose.py apps/backend/tests/test_compose_recovery_*.py
git commit -m "feat: execute allowlisted compose recovery"
```

---

### Task 7: 实现 PostgreSQL blocker fresh probe、终止与验证

**Files:**
- Create: `apps/backend/src/super_ai/recovery/postgres.py`
- Test: `apps/backend/tests/test_postgres_recovery_executor.py`
- Test: `apps/backend/tests/test_postgres_recovery_integration.py`

**Interfaces:**
- Produces: `PostgresBlockerRecoveryExecutor.preflight`, `execute_once`, `verify`.
- Consumes: named server-side database target/session factory, approved Intent fingerprint, diagnostic lock relationship fingerprint, Incident status reader.

- [ ] **Step 1: Write safe probe tests**

Use injected query adapter fixtures to assert preflight selects fresh blocker/waiter relations, rejects background/recovery/self/waiter PIDs, rejects zero or multiple targets, rejects changed database/lock fingerprint, and never trusts a PID stored by the model or request.

- [ ] **Step 2: Write approval-binding tests**

Assert execute requires `approved` record for same owner, Incident, Intent and proposal fingerprint with `expires_at > now`; stale or mismatched approval cannot call termination.

- [ ] **Step 3: Implement parameterized execution**

Use a SQLAlchemy `text("SELECT pg_terminate_backend(:pid)")` statement with an integer from the fresh trusted probe. Do not log/render the SQL, PID or DSN. Exactly one boolean result is accepted; connection loss after dispatch is unknown outcome.

- [ ] **Step 4: Write real PostgreSQL integration test**

Create two dedicated fixture transactions, induce a lock wait, approve the exact Intent, run preflight/execute/verify, and assert blocker is gone, waiter advances/ends, lock wait clears and no unrelated session is terminated. Run the unknown-result path with a spy and assert invocation count remains one.

- [ ] **Step 5: Verify**

Run from `apps/backend`: `uv run pytest tests/test_postgres_recovery_executor.py tests/test_postgres_recovery_integration.py -q`

Expected: PASS against the isolated test database.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/recovery/postgres.py apps/backend/tests/test_postgres_recovery_*.py
git commit -m "feat: terminate approved postgres blockers safely"
```

---

### Task 8: 编排 Background Job、ExecutionCoordinator 与恢复状态机

**Files:**
- Create: `apps/backend/src/super_ai/recovery/worker.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Test: `apps/backend/tests/test_recovery_worker.py`
- Test: `apps/backend/tests/test_recovery_worker_restart.py`

**Interfaces:**
- Produces: `build_production_recovery_handler(app) -> JobHandler`; registers `production_recovery`.
- Consumes: Task 4 repository, Task 5 policy, Task 6/7 executor registry, `ExecutionCoordinator`, `BackgroundJobContext`.

- [x] **Step 1: Write state progression tests**

Assert exact sequence:

```text
queued -> revalidating -> executing -> verifying -> recovered
```

and append safe audit/job events at every transition. Cancellation is honored before `executing`; after execution claim it returns invalid transition.

- [x] **Step 2: Write at-most-once restart tests**

Simulate duplicate job delivery, concurrent Workers, Worker crash before claim, crash after side-effect claim, operation timeout, and verification failure. Assert executor invocation count is `0` before a failed preflight and at most `1` after claim; an expired `running` side-effect lease with no completed result maps to `manual_intervention`.

Use this explicit restart table:

| Persisted execution / Intent state | Restart behavior |
|---|---|
| no execution claim / `queued` or `revalidating` | reacquire job, rerun fresh authorization and preflight |
| execution `running`, lease expired, outcome unknown | `manual_intervention`, no executor call |
| execution `completed`, Intent still `executing` | reuse stored safe result, transition to `verifying`, no executor call |
| Intent `verifying` | rerun only idempotent verifier probes |
| Intent `recovered` / terminal | return stored state, no executor or verifier call |

Set each executor timeout lower than the execution lease and configure the coordinator lease as `executor_timeout + 30 seconds`; test the invariant so a normal bounded action cannot outlive its claim.

- [x] **Step 3: Implement execution identity**

```python
ExecutionIdentity(
    task_id=intent.id,
    graph_version="production-recovery-v1",
    node_name=f"execute:{intent.action}:{intent.target_key}",
    logical_iteration=0,
    input_payload={"proposalFingerprint": intent.proposal_fingerprint},
    execution_kind="recovery",
    side_effecting=True,
)
```

Call `run_once(..., outcome_known_on_error=False)` once dispatch may have occurred. Clearly pre-dispatch failures bypass `run_once` or use known outcomes; never allow Background Job retry to re-enter an uncertain side effect. A completed execution record is a durable hand-off to verification, not an uncertain result.

- [x] **Step 4: Re-authorize immediately before the side-effect claim**

The handler reloads owner-scoped Incident, report, linked evidence, current configuration and approval, recomputes the proposal fingerprint, then calls `RecoveryPolicy.evaluate_execution()`. Require Incident active, recovery enabled, target still allowlisted, Compose auto flag still enabled when automatic, report/evidence unchanged and approval fresh/bound when required. Any drift appends a safe event and transitions to `manual_intervention` or an explicit denied terminal state with zero executor calls.

- [x] **Step 5: Register the durable handler**

Add `background_runtime.register("production_recovery", build_production_recovery_handler(app))`. Recovery job uses `max_attempts=1`; durable leasing still permits another Worker to pick up only before the side-effect execution claim.

- [x] **Step 6: Verify**

Run from `apps/backend`: `uv run pytest tests/test_recovery_worker.py tests/test_recovery_worker_restart.py -q`

Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/recovery/worker.py apps/backend/src/super_ai/api/app.py apps/backend/tests/test_recovery_worker*.py
git commit -m "feat: orchestrate durable recovery execution"
```

---

### Task 9: 暴露 owner-scoped API、审批、取消和事件读取

**Files:**
- Create: `apps/backend/src/super_ai/recovery/api.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/api/rate_limits.py`
- Modify: `apps/backend/src/super_ai/chat/aiops_bridge.py`
- Modify: `apps/backend/src/super_ai/chat/pending_action_jobs.py`
- Modify: `apps/backend/src/super_ai/memory/chat_runs_sqlalchemy.py`
- Test: `apps/backend/tests/test_recovery_api.py`
- Test: `apps/backend/tests/test_recovery_api_security.py`
- Modify: `apps/backend/tests/test_chat_aiops_bridge.py`
- Modify: `apps/backend/tests/test_pending_chat_actions.py`
- Modify: `apps/backend/tests/test_chat_runs_repository.py`

**Interfaces:**
- Produces: six endpoints from the design and unified response payloads from Task 2.
- Consumes: authenticated user dependency, `RecoveryIntentService`, repository, Background Job repository/runtime, rate limiter action `recovery.execute`.

- [x] **Step 1: Write API happy-path tests**

Cover:

```text
POST /aiops/diagnostics/{task_id}/recovery-intents
GET  /aiops/recovery-intents/{intent_id}
POST /aiops/recovery-intents/{intent_id}:approve
POST /aiops/recovery-intents/{intent_id}:reject
POST /aiops/recovery-intents/{intent_id}:cancel
GET  /aiops/recovery-intents/{intent_id}/events?afterSequence=0
```

Approval body is exactly `{ "incidentIdConfirmation": "<full id>" }`; create body only permits optional bounded `note` and rejects extra fields.

- [x] **Step 2: Write authorization and race tests**

Assert non-owner, wrong Incident confirmation, expired approval, duplicate approve, approve-vs-reject race, cancel-vs-worker race, malformed IDs, path traversal strings and extra action/PID/path fields cannot mutate or enumerate resources. API must not serialize private/trusted fields.

- [x] **Step 3: Implement API router**

Create Intent returns `201` or existing active Intent `200`; auto-eligible queued Intent uses `create_intent_with_job_and_event` and wakes runtime. Approve enforces `recovery.execute`, uses `approve_with_job_and_event`, then returns `202`. Reject/cancel return the converged terminal state. Inject transaction failures in API tests and prove no partial Intent/job/approval/event state escapes.

- [x] **Step 4: Add public event projection**

Map only `sequence`, `type`, `fromStatus`, `toStatus`, `safeReasonCode`, `safeSummary`, `durationMs`, `createdAt`. Validate allowlist recursively so forbidden keys such as `password`, `token`, `dsn`, `sql`, `pid`, `path`, `stdout`, `stderr`, `exception` cannot appear nested.

- [x] **Step 5: Migrate the Chat approval bridge to formal RecoveryIntent creation**

Keep the Chat tool name and pending confirmation UX for compatibility, but after the user confirms, `PendingChatActionJobService` calls `RecoveryIntentService.create(...)` instead of creating a new row in `aiops_recovery_approval_requests`. Chat may create/reuse an Intent and report `awaiting_approval`/`queued`; it may never approve, execute or supply action/target/PID. Existing legacy approval rows remain read-only and are returned with `legacy=true`, `executionPermitted=false`, and a safe instruction to create a current Intent; they are not silently converted into approval authority.

Test old rows, duplicate Chat confirmations, API-and-Chat concurrent creation, owner isolation, and that the Chat execution policy/tool allowlist gains no production write executor.

- [x] **Step 6: Verify**

Run from `apps/backend`: `uv run pytest tests/test_recovery_api.py tests/test_recovery_api_security.py tests/test_chat_aiops_bridge.py tests/test_pending_chat_actions.py tests/test_chat_runs_repository.py -q`

Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/recovery/api.py apps/backend/src/super_ai/api apps/backend/src/super_ai/chat apps/backend/src/super_ai/memory/chat_runs_sqlalchemy.py apps/backend/tests/test_recovery_api*.py apps/backend/tests/test_chat_aiops_bridge.py apps/backend/tests/test_pending_chat_actions.py apps/backend/tests/test_chat_runs_repository.py
git commit -m "feat: expose governed recovery api"
```

---

### Task 10: 用隔离真实故障验证 Compose 与 PostgreSQL 两条闭环

**Files:**
- Modify: `infra/compose.yaml`
- Modify: `config/project.template.json`
- Create: `apps/backend/tests/live/test_production_compose_recovery.py`
- Create: `apps/backend/tests/live/test_production_postgres_recovery.py`
- Modify: the existing Live runbook under `docs/` discovered during implementation.

**Interfaces:**
- Produces: repeatable, explicitly invoked production-recovery acceptance commands.
- Consumes: isolated `live-eval-order-api`, local Alertmanager ingestion, PostgreSQL fixture, real owner auth, production API only; no direct executor invocation and no oracle-derived authorization.

- [ ] **Step 1: Configure only the isolated target in local ignored config**

Document a user-local override with `enabled=true` and `automaticRecoveryEnabled=true` only for `live-eval-order-api`. Keep committed templates disabled/empty. Never enable the main backend, PostgreSQL service or unrelated Compose services for auto restart.

- [ ] **Step 2: Write Compose end-to-end acceptance**

Induce the existing order pool exhaustion, wait for Alertmanager Incident and diagnostic completion, create Intent through HTTP, wait for events, and assert one container restart, health/business recovery, Incident resolved, status `recovered`, and one side-effect execution record.

- [ ] **Step 3: Write PostgreSQL approval acceptance**

Induce an isolated lock wait, wait for diagnosis, create Intent, assert `awaiting_approval`, approve as owner using full Incident ID, then assert exact blocker termination, waiter progress, lock wait clear, Incident resolved, `recovered`, and no unrelated backend termination.

- [ ] **Step 4: Exercise negative closure paths**

Repeat with wrong owner, expired approval, changed blocker identity, disabled auto flag and duplicate client submission. Expected outcomes are safe errors or `manual_intervention`, with side-effect counts `0` or `1` as appropriate.

- [ ] **Step 5: Run the isolated acceptance commands**

Run from `apps/backend` after local Compose services and ignored configuration are ready:

```bash
uv run pytest tests/live/test_production_compose_recovery.py -q -s
uv run pytest tests/live/test_production_postgres_recovery.py -q -s
```

Expected: both PASS; no benchmark ground truth is read and each recovery is visible in persisted Intent/audit/execution records.

- [ ] **Step 6: Commit**

```bash
git add infra/compose.yaml config/project.template.json apps/backend/tests/live/test_production_* docs
git commit -m "test: validate production recovery closure"
```

---

### Task 11: 对齐前端计划、文档、OpenSpec tasks 与最终质量门

**Files:**
- Modify: `docs/superpowers/plans/2026-08-23-aiops-workbench-frontend-redesign.md`
- Modify: `docs/superpowers/specs/2026-08-23-aiops-workbench-frontend-redesign-design.md`
- Modify: `README.md`
- Modify: relevant `docs/` architecture/configuration/runbook pages.
- Modify: `openspec/changes/add-production-recovery-execution/tasks.md`

**Interfaces:**
- Produces: frontend implementation dependency on complete recovery API; migration ordering `202608230001` recovery then `202608230002` Agent config; truthful operator documentation.
- Consumes: all implemented contracts and verified commands from Tasks 1-10.

- [ ] **Step 1: Update the frontend plan**

Replace request-only recovery UI with typed Intent create/detail/approve/reject/cancel/events flow. Add approval countdown, safe gate reasons, execution state, verifier checks and `manual_intervention` state. Change every Agent configuration migration reference from `202608230001` to `202608230002`.

- [ ] **Step 2: Update architecture and operations docs**

Document the boundary `LangGraph -> immutable Intent -> policy/preflight -> executor -> verifier`, safe default-off configuration, only two supported actions, owner approval TTL, unknown-result semantics and the isolated enablement procedure. Explicitly state that the Agent and frontend cannot call recovery write tools directly.

- [ ] **Step 3: Run static and focused regression gates**

From repository root:

```bash
openspec validate --all
npm run contracts:typecheck
npm --workspace packages/api-contracts run test
```

From `apps/backend`:

```bash
uv run ruff check src/super_ai/recovery tests/test_recovery_*.py tests/test_postgres_recovery_*.py tests/test_compose_recovery_*.py
uv run pyright
uv run pytest tests/test_recovery_contracts.py tests/test_recovery_config.py tests/test_recovery_proposal_adapter.py tests/test_recovery_policy.py tests/test_recovery_intent_service.py tests/test_recovery_worker.py tests/test_recovery_worker_restart.py tests/test_recovery_api.py tests/test_recovery_api_security.py tests/test_chat_aiops_bridge.py tests/test_pending_chat_actions.py tests/test_chat_runs_repository.py -q
```

Expected: all commands PASS. Run PostgreSQL and Live suites separately as documented in Tasks 4, 7 and 10; do not hide missing infrastructure with skips.

- [ ] **Step 4: Check security invariants mechanically**

Run:

```bash
rg -n "create_subprocess_shell|shell=True|\.env|ground_truth|ReadGroundTruth|docker compose .*\{" apps/backend/src/super_ai/recovery
```

Expected: no matches. Review every public serializer and audit event for nested sensitive keys.

- [ ] **Step 5: Complete, sync and validate OpenSpec**

Mark only actually verified tasks complete. Use the repository `wiki-sync` workflow, build docs if navigation changed, and leave the change active until all acceptance checks pass; archive only after implementation is genuinely complete.

- [ ] **Step 6: Commit**

```bash
git add docs README.md openspec/changes/add-production-recovery-execution
git commit -m "docs: complete production recovery rollout"
```

## Acceptance Summary

- A client supplies only a diagnostic ID and optional note; action, target and trusted execution parameters are server-derived.
- Executable proposals are mapped deterministically from validated component/mechanism plus required evidence facts to one configured target; LLM free text cannot select an executor.
- Global disabled/default-empty configuration produces no side effects.
- Only explicitly allowlisted `live-eval-order-api` can be auto-restarted in the initial local acceptance setup.
- PostgreSQL blocker termination always requires current owner approval bound to the exact proposal and no older than 600 seconds.
- Duplicate requests, concurrent Workers, network loss and process restart never cause more than one side-effect invocation.
- Immediately before claim, execution reloads Incident/config/report/evidence/approval and denies drift with zero side effects.
- Intent/job/event creation and approval/queued/job/event transitions are each one PostgreSQL transaction with rollback tests.
- Unknown outcome, identity drift, cancellation race and failed verification converge to safe terminal/manual states without an automatic second action.
- A persisted completed side effect resumes at verification after Worker restart; it is not re-executed or incorrectly treated as unknown.
- `recovered` requires action postcondition, health/business or lock recovery, and Alertmanager resolved.
- The Chat recovery-approval tool creates/reuses the same formal Intent but cannot approve or execute it; legacy request-only rows remain non-executable.
- Persisted audit can trace Incident -> Diagnostic -> Evidence -> Report -> Intent -> Approval -> Execution -> Verification without sensitive data leakage.
