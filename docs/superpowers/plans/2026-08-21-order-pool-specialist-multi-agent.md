# Order Pool Specialist Multi-Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先用公开、可信的 Order Pool 复合证据修复 Single 确定性闭环，再把现有 Runtime/Log fan-out 深化为有界 Local Planning、Evidence Analysis、确定性聚合和可恢复持久化的 Specialist Multi-Agent，并完成真实 3×3 A/B。

**Architecture:** 保留现有 LangGraph `Send`、`AiopsDiagnosticService`、PostgreSQL checkpoint/execution/evidence repository、统一工具执行原语和中央 Decision/Validator/Recovery 链。新增代码只负责 Order Pool trusted compound pattern、通用 Specialist 合同与执行器、确定性 AggregatedInvestigation，以及 forced/shadow 发布控制；Specialist 共享不可变公开上下文和 PostgreSQL Evidence，不共享消息历史、局部可变状态或 Chain-of-Thought。

**Tech Stack:** Python 3.10、FastAPI、Pydantic v2、LangGraph、SQLAlchemy/PostgreSQL、pytest、Ruff、strict Pyright、现有 OpenAI-compatible Qwen provider、Docker Compose、腾讯云 CLS。

## Global Constraints

- 实施顺序固定为：Single deterministic repair → 唯一真实 Single canary → Specialist contracts/runtime → forced Multi → 真实 3×3 A/B → shadow；本计划不启用生产 auto Multi。
- Multi v1 只注册 `APY-LIVE-ORDER-POOL-LEAK-001`，但 Specialist 合同不得包含 scenario ID 或答案标签。
- Runtime 与 Log 最多并行 2 个；每个 Specialist 最多 3 个工具步骤、1 次 Local Planning、1 次 Evidence Analysis；每个结构化角色调用最多 1 次格式纠正重试。
- Specialist soft/hard timeout 为 120/180 秒；Global soft/hard deadline 为 240/360 秒。
- Planner、Specialist、Adjudicator、Decision 使用 `qwen3.7-plus`；Validator 使用 `qwen3.8-max`；Rerank 使用 `qwen3-vl-rerank`；Embedding 使用 `qwen3.7-text-embedding`。
- 与等价 Single 相比，Multi 平均成功模型调用数不得超过 `Single + 4`。
- PostgreSQL 是 checkpoint、Evidence、执行幂等与聚合结果的权威存储；Redis 只允许缓存或发布进度事件。
- Forced Multi 失败必须保存为 Multi 失败，不得静默以 Single 重跑覆盖结果。
- 不改变评分阈值、Ground Truth、恢复授权、安全门禁或 Oracle 隔离来使场景通过。
- 不保存原始 Prompt、原始模型响应、凭据、原始 CLS 日志或私有 Chain-of-Thought。

---

## Reuse Assessment

### Requirements and constraints

项目已经依赖 LangGraph，并已有 `investigator_dispatch`、`evidence_aggregator`、`PostgresDiagnosticCheckpointSaver`、`ExecutionCoordinator`、只读工具 capability registry、forced strategy 和 Live Archive。新实现必须保持 Python 3.10、现有依赖锁、owner/task scope、PostgreSQL 恢复语义和中央安全链，不能引入第二套 Agent runtime。

### Search scope

GitHub 查询覆盖 `langgraph multi agent supervisor handoff persistence`、`langgraph swarm state handoff`、`python multi agent router orchestration`。核验候选的 README、许可证、归档状态、最近 push 和 release：

| Candidate | License / activity | Fit | Decision |
| --- | --- | --- | --- |
| `langchain-ai/langgraph-supervisor-py` | MIT；未归档；2026-07-15 push；0.0.31 | 提供 supervisor、handoff、history mode 和 checkpointer 接口，但以 LLM handoff/共享消息为中心 | Reference only |
| `langchain-ai/langgraph-swarm-py` | MIT；未归档；2026-07-15 push；0.1.0 | 支持 active-agent handoff 和独立 state key，但自由 swarm 与固定 Runtime/Log fan-out 不匹配 | Reference only |
| `2FastLabs/agent-squad` | Apache-2.0；未归档；2026-08-15 push；1.1.4 | 独立多语言编排框架，路由思想可参考，替换成本和依赖重量过高 | Reference only |
| `microsoft/autogen` | GitHub 标示 CC-BY-4.0；未归档；0.7.5 | 完整独立 runtime，许可证、依赖和状态模型均不适合嵌入当前生产链 | Reject |

### Decision

不新增依赖。直接复用项目内 LangGraph `Send`、现有 capability registry、工具执行原语、Diagnostic Evidence、ExecutionCoordinator 和 PostgreSQL checkpointer；参考 supervisor/swarm 的 parent-child state transform，但自定义项目特有的 `SharedRunContext`、`SpecialistAssignment`、`SpecialistState`、`SpecialistResult` 和确定性 Aggregator。这样不产生许可证变更、原生二进制、外部服务或额外供应链审批。

## File Structure

- Modify `openspec/changes/add-order-pool-leak-live-scenario/specs/aiops-diagnosis-tasks/spec.md`: 增补 Single compound pattern、Specialist、聚合和发布场景。
- Modify `openspec/changes/add-order-pool-leak-live-scenario/tasks.md`: 追加本计划的可跟踪任务，不改写已完成证据。
- Modify `openspec/changes/add-single-multi-agent-source-routing/specs/aiops-diagnosis-tasks/spec.md`: 收紧 Specialist checkpoint、预算、partial failure 与 shadow 语义。
- Modify `apps/backend/src/super_ai/aiops/trusted_patterns.py`: 只保存 code-owned deterministic compound resolver。
- Modify `apps/backend/src/super_ai/aiops/adjudication.py`: 为 trusted resolver 提供最小、公开的 Evidence provenance 类型，不把来源信息塞进 Fact 文本。
- Create `apps/backend/src/super_ai/aiops/specialists.py`: Specialist 的公开类型、状态转换、结构化 schema、稳定 identity 和私有字段拒绝。
- Modify `apps/backend/src/super_ai/aiops/investigation_runtime.py`: Local Planner、工具串行执行、Evidence Analysis、角色预算和重放。
- Modify `apps/backend/src/super_ai/aiops/evidence_aggregation.py`: SpecialistResult 验证、source fingerprint 去重、missing domain、budget 和稳定 checksum。
- Modify `apps/backend/src/super_ai/aiops/investigation.py`: 新阈值、forced/shadow release mode 和路由审计。
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: parent/child state transform、并行 Specialist、checkpoint、中央链回接。
- Modify `apps/backend/src/super_ai/evaluation/live/diagnostics.py`: Benchmark-only forced Multi 注册和相同工具目录保证。
- Modify `apps/backend/src/super_ai/evaluation/artifacts.py`: 投影安全、定长 Specialist/Aggregator 指标。
- Modify `docs/aiops/agentpy-domainbench.md`: 记录 Single gate、A/B 结果与 auto 保持关闭的结论。
- Tests stay beside existing AIOps and Live tests; no parallel test package is created.

### Task 1: Update OpenSpec contracts before behavior changes

**Files:**
- Modify: `openspec/changes/add-order-pool-leak-live-scenario/specs/aiops-diagnosis-tasks/spec.md`
- Modify: `openspec/changes/add-order-pool-leak-live-scenario/tasks.md`
- Modify: `openspec/changes/add-single-multi-agent-source-routing/specs/aiops-diagnosis-tasks/spec.md`

**Interfaces:**
- Consumes: confirmed design `docs/superpowers/specs/2026-08-21-order-pool-specialist-multi-agent-design.md`.
- Produces: executable requirements for Tasks 2–10.

- [ ] **Step 1: Add exact delta requirements**

Append requirements stating: the Order Pool compound pattern consumes only trusted current-task facts and fails closed for every missing/conflicting trigger/mechanism/impact fact; Specialist assignments are immutable and source-scoped; Local Planning and Evidence Analysis are separately budgeted; Aggregator is deterministic and cannot write decisions; forced Multi never falls back; auto is shadow-only; PostgreSQL replay returns the same completed role result.

- [ ] **Step 2: Add verifiable scenarios to the active task list**

Append unchecked tasks `5.1` through `5.8` corresponding to Tasks 2–9 below and preserve existing checked entries, especially unfinished A/B task `4.4`.

- [ ] **Step 3: Validate specifications**

Run: `openspec validate --all`

Expected: exit 0 with both active changes valid.

- [ ] **Step 4: Commit**

```bash
git add openspec/changes/add-order-pool-leak-live-scenario openspec/changes/add-single-multi-agent-source-routing
git commit -m "docs: specify order pool specialist workflow"
```

### Task 2: Close the Order Pool Single path with a trusted compound pattern

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/trusted_patterns.py`
- Modify: `apps/backend/src/super_ai/aiops/adjudication.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_trusted_patterns.py`
- Modify: `apps/backend/tests/test_aiops_fact_adapters.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`

**Interfaces:**
- Consumes: existing `DiagnosticFact`/`HypothesisAssessment` plus owner/task-scoped persisted Diagnostic Evidence provenance.
- Produces: `_match_order_pool_lifecycle(facts) -> _OrderPoolMatch | None`; matched pattern ID `order_connection_checkout_without_checkin`.

- [ ] **Step 1: Write the positive and exhaustive negative tests**

Create `_order_pool_facts()` with separate Evidence IDs for lifecycle log, pool state, sessions, reachability and business probe. Assert exactly `order_connection_lifecycle_failure` becomes `supported`; unreachable and lock-wait become `refuted`; traffic pressure and slow statement remain non-supported; trigger/mechanism/impact each cite independent evidence. Parameterize removal or contradiction of checkout, failed update, acquire timeout, missing checkin, capacity, free count, waiter, sessions, reachability, no lock wait and business timeout; every case must return no match. Add cross-owner, cross-task, duplicate-source-with-different-Evidence-ID and scenario/oracle-shaped negative tests through the production Fact Adapter path.

- [ ] **Step 2: Run the new tests and verify the current resolver fails**

Run: `uv run pytest tests/test_aiops_trusted_patterns.py -k order_pool -q`

Expected: FAIL because `order_connection_checkout_without_checkin` is not implemented.

- [ ] **Step 3: Implement the minimal code-owned matcher**

Add a frozen `TrustedEvidenceProvenance(evidence_id, owner_user_id, task_id, source_fingerprint, source_domain, tool_name)` and extend the resolver signature to `resolve_trusted_patterns(..., evidence_provenance: Mapping[str, TrustedEvidenceProvenance])`. In `diagnostics.py`, build this map only from the current owner/task's persisted `DiagnosticEvidenceRecord.payload["sourceFingerprint"]` plus its completed tool audit; never trust a model-supplied fingerprint. Add a frozen `_OrderPoolMatch` containing selected facts by causal role. Dispatch both Nginx and Order Pool resolvers from `resolve_trusted_patterns`; select exactly one trusted fact per required key, require lifecycle ordering, reject `connection_checkin`, cross-scope provenance and repeated source fingerprints, and construct deterministic assessments/observation decisions without identity fields. Reuse `_one_fact`, `_one_numeric_fact`, `_one_contains_fact`; do not inspect benchmark scenario IDs.

- [ ] **Step 4: Run focused regression**

Run: `uv run pytest tests/test_aiops_trusted_patterns.py tests/test_aiops_fact_adapters.py tests/test_aiops_hypothesis_adjudication.py -q`

Expected: PASS; Nginx pattern remains unchanged and all Order Pool negative cases fail closed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/aiops/trusted_patterns.py apps/backend/src/super_ai/aiops/adjudication.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_trusted_patterns.py apps/backend/tests/test_aiops_fact_adapters.py apps/backend/tests/test_aiops_v4_workflow.py
git commit -m "fix: ground order pool lifecycle diagnosis"
```

### Task 3: Enforce the one-run Single canary gate

**Files:**
- Modify: `apps/backend/tests/test_live_order_pool_contracts.py`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: persisted Live CLI report and diagnostic task Evidence/steps.
- Produces: one immutable Single canary record proving a grounded decision before Multi code begins.

- [ ] **Step 1: Add an offline canary-result contract**

Add a test that feeds the real canary-shaped public outputs from all four tools through the production Fact Adapter and trusted resolver, then asserts one supported lifecycle hypothesis, three causal roles, at least three independent source groups, a non-null grounded decision and `executionPermitted=false` unless recovery policy separately authorizes it.

- [ ] **Step 2: Run Docker and target checks before spending one model run**

Run from repository root:

```powershell
docker compose -f infra/compose.yaml config
Set-Location apps/backend
uv run pytest tests/test_live_order_pool_contracts.py tests/test_live_order_pool_docker.py tests/test_aiops_trusted_patterns.py -q
```

Expected: Compose config exit 0 and tests PASS.

- [ ] **Step 3: Run exactly one new persisted Single canary**

```powershell
uv run python -m super_ai.evaluation.live.cli run --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id order-pool-specialist-single-gate-20260821 --owner-user-id eval-user --knowledge-base-id kb-30-cards --evidence-source cls --strategy single --config ..\..\config\user.project.json
```

Expected: terminal report is persisted; `rootCauseDecision` is non-null; `matchedTrustedPatternIds` contains `order_connection_checkout_without_checkin`; cleanup succeeds. If it fails, stop this plan and diagnose the Single failure without starting Task 4.

- [ ] **Step 4: Record only safe evidence**

Append run ID, score dimensions, decision origin, model-call count, elapsed time, cleanup result and archive path to `docs/aiops/agentpy-domainbench.md`; do not copy raw model/CLS content or secrets.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/tests/test_live_order_pool_contracts.py docs/aiops/agentpy-domainbench.md
git commit -m "test: gate order pool specialist work on single canary"
```

### Task 4: Introduce immutable Specialist contracts and stable identities

**Files:**
- Create: `apps/backend/src/super_ai/aiops/specialists.py`
- Create: `apps/backend/tests/test_aiops_specialist_contracts.py`

**Interfaces:**
- Consumes: `InvestigatorType`, `JsonValue`, public hypotheses and owner/task scope.
- Produces: `SharedRunContext`, `SpecialistAssignment`, `SpecialistPlanStep`, `SpecialistState`, `SpecialistResult`, `specialist_execution_key()`, `specialist_result_checksum()`.

- [ ] **Step 1: Write schema, immutability and answer-isolation tests**

Test that nested `ground_truth`, `oracle`, `primary_cause`, `prompt`, `raw_response`, credentials and recovery tool names are rejected; mappings/sets are frozen; Runtime cannot receive CLS tools; Log cannot receive Runtime tools; max steps is 3; role model-call budget is 2; stable keys ignore dictionary order and change for role, step, arguments, task or graph version.

- [ ] **Step 2: Run the tests and verify import failure**

Run: `uv run pytest tests/test_aiops_specialist_contracts.py -q`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the contracts**

Use frozen slotted dataclasses and Pydantic output models. The public contracts must expose these exact fields:

```python
@dataclass(frozen=True, slots=True)
class SharedRunContext:
    owner_user_id: str
    task_id: str
    graph_version: str
    public_incident_input: Mapping[str, JsonValue]
    public_hypotheses: tuple[str, ...]
    decision_vocabulary: Mapping[str, JsonValue]
    allowed_tools_by_specialist: Mapping[SpecialistRole, frozenset[str]]
    trusted_arguments_by_specialist: Mapping[SpecialistRole, Mapping[str, Mapping[str, JsonValue]]]
    global_soft_deadline_at: datetime
    global_hard_deadline_at: datetime
    global_model_budget: int

@dataclass(frozen=True, slots=True)
class SpecialistAssignment:
    role: SpecialistRole
    objective: str
    hypotheses_to_test: tuple[str, ...]
    required_causal_roles: tuple[CausalRole, ...]
    allowed_tools: frozenset[str]
    trusted_arguments_by_tool: Mapping[str, Mapping[str, JsonValue]]
    maximum_tool_steps: int
    model_call_budget: int
    soft_deadline_at: datetime
    hard_deadline_at: datetime

@dataclass(frozen=True, slots=True)
class SpecialistResult:
    role: SpecialistRole
    terminal_status: SpecialistTerminalStatus
    tested_hypotheses: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    fact_candidates: tuple[EvidenceClaim, ...]
    proposed_assessments: tuple[PublicAssessmentSignal, ...]
    unresolved_questions: tuple[str, ...]
    completed_steps: tuple[str, ...]
    model_call_count: int
    duration_ms: int
    result_checksum: str
```

Canonicalize JSON with sorted keys and SHA-256. `trusted_arguments_by_tool` is code-owned and immutable; every Log assignment must contain the exact prepared `Region`, `TopicId`, `From`, `To`, `Query` and `Limit` binding. Never include model reasoning in a dataclass or serialized payload.

- [ ] **Step 4: Run contract tests and static checks**

Run: `uv run pytest tests/test_aiops_specialist_contracts.py -q && uv run ruff check src/super_ai/aiops/specialists.py tests/test_aiops_specialist_contracts.py && uv run pyright src/super_ai/aiops/specialists.py tests/test_aiops_specialist_contracts.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/aiops/specialists.py apps/backend/tests/test_aiops_specialist_contracts.py
git commit -m "feat: add bounded specialist contracts"
```

### Task 5: Add Local Planning and Evidence Analysis to the existing Investigator runtime

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/investigation_runtime.py`
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/tests/test_aiops_multi_agent_runtime.py`
- Create: `apps/backend/tests/test_aiops_specialist_model_roles.py`

**Interfaces:**
- Consumes: Task 4 contracts, existing `execute_diagnostic_tool`, model provider, global `ModelCallBudget` and `ExecutionCoordinator`.
- Produces: `SpecialistExecutor.execute(context, assignment) -> SpecialistResult` and reusable `invoke_bounded_structured_role(...)`.

- [ ] **Step 1: Write red tests for the two role calls**

Cover valid Local Plan, one invalid response followed by one corrected response, retry exhaustion, allowlist rejection, no plan after soft deadline, serial dependent tool steps, max 3 tools, Evidence Analysis consuming only its own completed Evidence, failure retaining completed Evidence, and total role calls never exceeding 2. For Log, attempt to alter `Region`, `TopicId`, `From`, `To`, `Query`, `Limit`, run/incident identifiers and owner scope; assert rejection before the MCP call. Assert prompts and persisted audits contain public summaries only.

- [ ] **Step 2: Write replay and timeout tests**

Using the existing fake execution repository, assert identical `task_id + graph_version + role + role_name + input_fingerprint` reuses a completed model result; a worker restart resumes the first incomplete role; soft timeout aggregates completed evidence; hard timeout cancels the branch; uncertain non-side-effecting model/tool reads retry under the same logical key with a new attempt.

- [ ] **Step 3: Run the tests and verify failure**

Run: `uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_specialist_model_roles.py -q`

Expected: FAIL because the current `InvestigatorExecutor` only executes Main Planner steps and uses zero/one optional model call.

- [ ] **Step 4: Implement a generic bounded structured-role helper**

Extract the already-proven structured parsing/failure classification from `decision_validation.py` into `invoke_bounded_structured_role(model, schema, prompt, correction_prompt, maximum_attempts=2)`. Return typed value plus safe audit metadata (`role`, `attempt`, `durationMs`, `errorCategory`); never return or store raw response. Preserve the existing Decision/Validator structured envelope, two-attempt correction and provider failure classifications unchanged.

- [ ] **Step 5: Implement SpecialistExecutor without replacing the shared tool primitive**

Build a role-specific prompt from immutable context and assignment, validate every planned tool against `allowed_tools`, then replace all model-supplied arguments with the exact code-owned `trusted_arguments_by_tool[tool_name]` binding before `execute_diagnostic_tool`. Reject missing or extra Log bindings rather than merging them. Execute up to three steps serially, then run Evidence Analysis over safe outputs and Evidence IDs. Local Plan and Analysis each consume one pre-reserved branch model unit and use stable `ExecutionIdentity(execution_kind="model")`. Emit failed/inconclusive results instead of synthesizing assessments.

- [ ] **Step 6: Run focused checks**

Run: `uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_specialist_model_roles.py tests/test_aiops_execution_coordinator.py tests/test_aiops_model_budget.py tests/test_aiops_decision_validation.py tests/test_aiops_validator_routing.py -q`

Expected: PASS, including duplicate execution and restart cases.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/aiops/investigation_runtime.py apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/tests/test_aiops_multi_agent_runtime.py apps/backend/tests/test_aiops_specialist_model_roles.py
git commit -m "feat: run bounded specialist model roles"
```

### Task 6: Deepen deterministic aggregation and PostgreSQL authority

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/evidence_aggregation.py`
- Modify: `apps/backend/tests/test_aiops_evidence_packets.py`
- Modify: `apps/backend/tests/test_aiops_network_resume.py`

**Interfaces:**
- Consumes: `SpecialistResult`, persisted `DiagnosticEvidenceRecord`, completed tool audits and expected assignments.
- Produces: `AggregatedInvestigation` with stable checksum, source groups, conflicts, missing domains and budget usage.

- [ ] **Step 1: Write red aggregation tests**

Assert result order independence; duplicate Evidence ID counts once; different Evidence IDs with the same `sourceFingerprint` count as one source; conflict is recorded and never majority-voted; cross-owner/task/role/tool evidence is rejected; one timeout preserves the other role; both failures yield `multi_investigation_failed`; proposed assessments remain untrusted; aggregation cannot contain root cause or recovery fields.

- [ ] **Step 2: Write PostgreSQL replay tests**

Persist tool Evidence first, then SpecialistResult checkpoint, then aggregation checkpoint. Simulate a crash after each boundary and assert replay produces the same result and aggregation checksum without duplicate Evidence or model calls. Assert conflicting completed result under the same identity fails closed.

- [ ] **Step 3: Run and observe failure**

Run: `uv run pytest tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py -q`

Expected: FAIL because current AggregationResult lacks source groups, missing domains, budget usage and stable checksum.

- [ ] **Step 4: Implement AggregatedInvestigation**

Extend deterministic aggregation with these exact fields: `specialist_statuses`, `evidence`, `normalized_facts`, `hypothesis_signals`, `conflicts`, `source_groups`, `missing_domains`, `budget_usage`, `aggregation_checksum`. Derive source groups only from persisted Evidence payload `sourceFingerprint`; hash `task_id + graph_version + sorted(result_checksum)`; keep all mappings sorted and immutable.

- [ ] **Step 5: Reuse existing persistence instead of adding a table**

Store completed tool observations in `aiops_diagnostic_evidence`, role/aggregation outputs in existing PostgreSQL LangGraph checkpoints, and stable role/model/tool executions in `aiops_execution_records`. Do not add an Alembic migration unless an implementation test proves these existing repositories cannot enforce the required unique identity; if that occurs, stop and request approval before changing schema.

- [ ] **Step 6: Run focused checks and commit**

Run: `uv run pytest tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py tests/test_aiops_checkpointing.py tests/test_aiops_execution_repository.py -q`

Expected: PASS.

```bash
git add apps/backend/src/super_ai/aiops/evidence_aggregation.py apps/backend/tests/test_aiops_evidence_packets.py apps/backend/tests/test_aiops_network_resume.py
git commit -m "feat: aggregate specialist evidence deterministically"
```

### Task 7: Wire Specialist child state into the existing LangGraph

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_aiops_sse_delivery.py`

**Interfaces:**
- Consumes: Tasks 4–6 and existing `strategy_router -> investigator_dispatch -> evidence_aggregator` topology.
- Produces: isolated Runtime/Log child execution whose only parent-state merge value is a serialized `SpecialistResult`.

- [ ] **Step 1: Write graph and isolation tests**

Assert Multi produces exactly two `Send` branches in stable Runtime/Log order; both run concurrently; each receives an immutable context plus only its own assignment; neither receives the other role's local plan/output; branch completion order does not change aggregation; Aggregator executes once; Single graph path is byte-for-byte equivalent outside the trusted pattern fix. Before dispatch the parent atomically reserves four optional model calls (two per branch); insufficient reserve prevents Multi. Branches never merge a scalar `model_call_count` directly.

- [ ] **Step 2: Write safe audit tests**

Assert checkpoints/SSE expose routing reason, role, terminal status, safe tool names, evidence IDs, model counts, durations, conflict/missing-domain summaries and replay events, but not prompts, raw model responses, raw CLS records, credentials or private reasoning.

- [ ] **Step 3: Run and verify current dispatch fails the new semantics**

Run: `uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_reasoning_trace.py tests/test_aiops_sse_delivery.py -q`

Expected: FAIL because `_investigator_dispatch` currently replays global plan steps and returns `EvidencePacket` with `model_calls_used=0`.

- [ ] **Step 4: Replace only Multi branch internals**

Keep graph node names and central route intact. Convert `investigation_dispatches` to `SpecialistAssignment`; call `SpecialistExecutor` inside `_investigator_dispatch`; serialize only SpecialistResult; read persisted Evidence in `_evidence_aggregator`; map accepted normalized facts into the existing `aggregated_facts` key; continue through current Fact Adapter, trusted resolver, sufficiency, Adjudicator only when needed, Decision, Validator and Recovery Policy.

- [ ] **Step 5: Enforce deadline and failure semantics**

At soft deadline do not start a new role call; at hard deadline cancel unfinished branch and preserve completed Evidence. At fan-in, settle the reservation exactly once as `parent_count_before_dispatch + sum(unique persisted successful role calls)`, releasing unused units; replay, retry and partial failure cannot charge a logical role twice. The updated parent count is the starting count for Shared Adjudicator/Decision/Validator and must remain within the run hard limit. One failed role results in partial aggregation and prevents unsafe recovery; two failed roles terminate as `multi_investigation_failed`. Remove current `fallback_to_single_agent` behavior for forced Multi.

- [ ] **Step 6: Run checks and commit**

Run: `uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_reasoning_trace.py tests/test_aiops_sse_delivery.py tests/test_aiops_validator_routing.py tests/test_live_recovery_policy.py -q`

Expected: PASS.

```bash
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_sse_delivery.py
git commit -m "feat: wire specialist branches into aiops graph"
```

### Task 8: Implement threshold 5, forced Multi registry and shadow-only auto

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/investigation.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_investigation_router.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Consumes: existing public routing features and Benchmark-only `--strategy` input.
- Produces: requested/effective strategy, score, matched/rejected features, release mode and downgrade reason.

- [ ] **Step 1: Write routing matrix tests**

Assert each confirmed score feature independently and in combination: Runtime+CLS `+3`; at least three public candidates `+2`; candidates spanning component/mechanism domains `+2`; cross-source temporal causal chain required `+2`; deterministic evidence already decision-ready `-3`; one evidence domain sufficient `-3`; insufficient deadline `-4`; insufficient model budget `-4`. Assert matched/rejected feature names are persisted, score 5 is Multi candidate, Order Pool may run forced Multi only through Benchmark CLI, ordinary API `auto` records `shadow_multi_candidate` but executes Single, deterministic-ready remains fast path, non-Order-Pool forced Multi is rejected, and forced Multi runtime failure remains effective Multi failure.

- [ ] **Step 2: Run and verify threshold/release failures**

Run: `uv run pytest tests/test_aiops_investigation_router.py tests/test_live_diagnostic_adapter.py -q`

Expected: FAIL because current threshold is 6, optional role budget is 1, and auto can directly select Multi when enabled.

- [ ] **Step 3: Implement the release contract**

Replace the legacy scoring terms (`severity`, stagnation and knowledge miss) with the confirmed eight-feature matrix and exact weights above; do not retain hidden double-counting. Set `multi_agent_threshold=5`, `maximum_optional_model_calls_per_investigator=2`, Specialist timeout values 120/180 seconds and global 240/360 seconds. Add `release_mode: Literal["forced_benchmark", "shadow", "single"]`; in `auto`, persist candidate score but force effective Single. Register only Order Pool for `forced_benchmark` and preserve identical discovered tool catalogs and exact trusted argument bindings across Single/Multi.

- [ ] **Step 4: Run checks and commit**

Run: `uv run pytest tests/test_aiops_investigation_router.py tests/test_live_diagnostic_adapter.py tests/test_aiops_model_budget.py -q`

Expected: PASS.

```bash
git add apps/backend/src/super_ai/aiops/investigation.py apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/tests/test_aiops_investigation_router.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "feat: gate order pool specialist routing"
```

### Task 9: Persist safe Specialist metrics and complete offline forced-Multi acceptance

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`
- Modify: `apps/backend/tests/test_live_benchmark_runner.py`

**Interfaces:**
- Consumes: routing, SpecialistResult and AggregatedInvestigation checkpoint payloads.
- Produces: fixed-schema, answer-isolated A/B metrics without changing the existing score.

- [ ] **Step 1: Write artifact and scoring invariance tests**

Assert metrics include requested/effective strategy, role statuses, duration/model/tool counts, evidence contribution, source groups, duplicate count, conflicts, missing domains, aggregation checksum and terminal failure category. Assert scoring is identical for equivalent Single/Multi decisions, strategy labels do not add points, duplicate evidence adds zero, missing/failed role cannot fabricate recall, and raw/private fields are discarded.

- [ ] **Step 2: Run and verify missing metrics**

Run: `uv run pytest tests/test_evaluation_artifacts.py tests/test_live_evaluation_scoring.py tests/test_live_benchmark_runner.py -q`

Expected: FAIL because current artifact projection only understands EvidencePacket-level statuses.

- [ ] **Step 3: Extend projection with bounded fields**

Add only scalar/count/ID-list fields with explicit length limits. Read terminal values from persisted checkpoints rather than in-memory branch objects. Keep `score_live_run` independent of requested strategy and do not change dimension weights.

- [ ] **Step 4: Run offline production-path acceptance**

Run: `uv run pytest tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_evidence_packets.py tests/test_aiops_v4_workflow.py tests/test_live_order_pool_contracts.py tests/test_live_evaluation_scoring.py tests/test_evaluation_artifacts.py -q`

Expected: PASS with no network/model calls.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_live_evaluation_scoring.py apps/backend/tests/test_live_benchmark_runner.py
git commit -m "test: persist specialist benchmark metrics"
```

### Task 10: Run real 3×3 A/B, document the decision, and verify the change

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/add-order-pool-leak-live-scenario/tasks.md`

**Interfaces:**
- Consumes: all prior tasks, live PostgreSQL/order-api/CLS, configured Qwen models and Evaluation Archive.
- Produces: six immutable terminal runs and an explicit keep-disabled or eligible-for-later-auto conclusion.

- [ ] **Step 1: Run preflight verification**

From `apps/backend` run:

```powershell
uv run pytest tests/test_aiops_trusted_patterns.py tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py tests/test_aiops_evidence_packets.py tests/test_aiops_v4_workflow.py tests/test_live_order_pool_contracts.py tests/test_live_order_pool_docker.py -q
uv run ruff check src/super_ai/aiops tests/test_aiops_trusted_patterns.py tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py tests/test_aiops_evidence_packets.py tests/test_aiops_v4_workflow.py
uv run pyright src/super_ai/aiops tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py
```

Expected: all commands exit 0. Do not run the real campaign otherwise.

- [ ] **Step 2: Execute three Single and three Multi runs under one campaign**

Use run IDs `order-pool-specialist-ab-single-01-20260821` through `03` and `order-pool-specialist-ab-multi-01-20260821` through `03`, the same owner `eval-user`, knowledge base `kb-30-cards`, config `../../config/user.project.json`, evidence source `cls`, and a single generated campaign ID. Run sequentially to avoid cross-run fault overlap; every failed terminal run remains archived and is not overwritten.

- [ ] **Step 3: Compare fixed acceptance metrics**

Compute Root Cause Top-1, Evidence Recall, trigger/mechanism/impact completeness, independent source groups, duplicate Evidence count, successful model-call count, mean/P95 elapsed time, Specialist failure rate, safety gate, recovery/verification and cleanup. Multi is only eligible for a future auto design when it does not reduce root-cause accuracy, improves at least one capability metric, adds no unsafe recovery, stays within `Single + 4` average successful calls, stays below 360-second P95, and has safety/cleanup no worse than Single.

- [ ] **Step 4: Keep production auto disabled and record evidence**

Document all six run IDs, archive checksums, failures, aggregate comparison and conclusion in `docs/aiops/agentpy-domainbench.md`. Even if Multi passes, record only “eligible for a separately approved auto-routing design”; do not change production routing in this plan.

- [ ] **Step 5: Mark OpenSpec tasks and run final validation**

Check only tasks backed by passing commands and persisted runs, then run:

```powershell
Set-Location ..\..
openspec validate --all
npm run docs:build
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit final evidence**

```bash
git add docs/aiops/agentpy-domainbench.md openspec/changes/add-order-pool-leak-live-scenario/tasks.md
git commit -m "docs: record order pool specialist ab acceptance"
```

## Final Acceptance Checklist

- [ ] Single trusted pattern passes all missing/conflict/foreign-source negative tests and one real canary.
- [ ] Runtime/Log have separate immutable local state and at most two role calls plus three tool steps each.
- [ ] PostgreSQL replay does not repeat completed model/tool work and produces stable role/aggregation checksums.
- [ ] Aggregator is deterministic, records conflicts and source-group deduplication, and never decides or authorizes recovery.
- [ ] Forced Multi failure is preserved; ordinary auto remains shadow/single.
- [ ] A/B uses identical model/config/tool/evidence conditions and persists all six terminal results.
- [ ] Target pytest, Ruff, Pyright, OpenSpec and docs build pass; no score, safety gate or recovery permission was loosened.
