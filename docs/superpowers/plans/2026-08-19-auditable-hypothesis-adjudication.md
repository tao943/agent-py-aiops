# Auditable Hypothesis Adjudication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 AIOps 诊断升级为可审计的四态假设裁决、条件式 LLM Validator 和 PostgreSQL 可恢复执行，使典型单场景模型调用降至 4～5 次、硬上限 8 次，并支持网络或 Worker 中断后的安全续跑。

**Architecture:** Planner 一次性产生有界取证计划和公开 evidence rules；Executor 将工具结果转换为公共 facts，确定性 reducer 负责 `supported / refuted / causally_inactive / unresolved` 裁决，批量 LLM Adjudicator 只处理无法由规则解释的歧义。LangGraph 使用 PostgreSQL checkpointer 保存图状态，同时以稳定 execution/tool/recovery key 保证节点、工具和副作用幂等；Deterministic Validator 始终执行，独立 LLM Validator 仅由确定性风险路由触发。

**Tech Stack:** Python 3.10+、Pydantic、LangGraph 1.2.8、SQLAlchemy Async、Alembic、PostgreSQL 16、FastAPI BackgroundJobRuntime、pytest、Ruff、Pyright、OpenSpec。

## Global Constraints

- 新执行图固定 `graphVersion=aiops-diagnostic-v2`，新 Artifact 固定 `workflowVersion=evidence-driven-v4`。
- 四态 disposition 是安全判断真源；旧 `status` 仅是兼容投影，不能进入新门禁。
- `causally_inactive` 必须引用至少一条公开 Evidence；另一原因已能解释故障本身不构成不活跃证据。
- 恰好一个 `supported` 且不存在 `unresolved` 活跃竞争根因，才允许形成单一根因结论。
- 所有路径执行 Deterministic Validator；条件式 LLM Validator 由代码计算，模型文本不能修改路由。
- 单场景模型调用硬上限 8；任何格式纠正和网络重试都计入预算。
- 整体诊断沿用首次启动时持久化的 5 分钟软截止时间和 8 分钟硬截止时间；重启不得重置 deadline，Replanner 最多一次。
- PostgreSQL 唯一约束是幂等最终保障；本轮不增加 Redis 分布式锁或新第三方依赖。
- Planner 只能实例化项目代码中受信任的 evidence-rule template；模型自定义 fact/disposition 因果映射不得进入 deterministic reducer。
- 初始告警 Evidence、诊断 Step/Evidence、工具审计和报告链接均使用稳定业务 ID 冲突安全写入，避免副作用提交后 checkpoint 前崩溃产生重复记录。
- 不读取或泄露 Ground Truth、Oracle、Prompt、模型原始响应、私有推理、凭据或未脱敏 CLS 日志。
- 不降低 Benchmark 阈值、评分权重、恢复授权或 required evidence 门禁。
- 实施、调试和验证均由当前主 Agent 单独完成，不分派实现子任务。

## File Structure

- `super_ai/aiops/adjudication.py`：四态模型、evidence rule DSL、确定性 reducer 和 sufficiency。
- `super_ai/aiops/facts.py`：公共 Observation 到 typed facts 的规范化适配。
- `super_ai/aiops/model_budget.py`：角色级超时、调用预算、重试与安全审计。
- `super_ai/aiops/validator_routing.py`：条件式 LLM Validator 的纯函数路由。
- `super_ai/aiops/execution.py`：稳定 key、claim/reuse/wait/uncertain 协调器。
- `super_ai/aiops/checkpointing.py`：LangGraph `BaseCheckpointSaver` 的 PostgreSQL 异步适配器。
- `super_ai/memory/aiops_execution_sqlalchemy.py`：执行记录、图 checkpoint/write 的 SQLAlchemy 仓储。
- `diagnostics.py`：只负责编排节点、状态与 SSE，不继续承载 reducer 和 checkpoint 细节。

---

### Task 1: 固化 OpenSpec 行为合同

**Files:**
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/.openspec.yaml`
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/proposal.md`
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/design.md`
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/tasks.md`
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/specs/aiops-diagnosis-tasks/spec.md`
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/specs/background-job-runtime/spec.md`
- Create: `openspec/changes/add-auditable-hypothesis-adjudication/specs/agentpy-sre-benchmark/spec.md`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-08-18-auditable-hypothesis-adjudication-design.md`.
- Produces: disposition、条件 Validator、幂等续跑和 v4 Artifact 的可验证 SHALL/MUST 合同。

- [ ] **Step 1: 写 OpenSpec delta**

`aiops-diagnosis-tasks` 至少声明：

```markdown
### Requirement: Hypothesis disposition is evidence-audited
Workflow SHALL represent every public hypothesis as supported, refuted,
causally_inactive, or unresolved. A refuted or causally_inactive disposition
MUST cite current-task public Evidence.

#### Scenario: A complete cause does not dismiss a competitor
- **WHEN** one supported cause explains the incident but no evidence addresses another competitor
- **THEN** the competitor MUST remain unresolved
```

`background-job-runtime` 声明同 task/graph version 从最后完成 checkpoint 恢复，未知副作用禁止重放；`agentpy-sre-benchmark` 声明 v4/v2/v3 兼容和 8 次调用上限。

- [ ] **Step 2: 运行 strict 验证**

Run from repository root:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-auditable-hypothesis-adjudication --strict
```

Expected: change valid，退出 0，无缺失 capability 或 scenario。

- [ ] **Step 3: 提交合同**

```powershell
git add openspec/changes/add-auditable-hypothesis-adjudication
git commit -m "spec: define auditable hypothesis adjudication"
```

---

### Task 2: 建立四态假设领域模型与确定性 reducer

**Files:**
- Create: `apps/backend/src/super_ai/aiops/adjudication.py`
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Create: `apps/backend/tests/test_aiops_hypothesis_adjudication.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `HypothesisAssessment`、`HypothesisEvidenceRule`、`reduce_hypotheses()`、`assess_sufficiency()`。
- Consumes: 当前任务公开 hypothesis IDs、公共 Evidence facts 和 Planner 声明的规则。

- [ ] **Step 1: 写四态与证据约束 RED 测试**

```python
def test_complete_cause_does_not_close_unaddressed_competitor() -> None:
    result = reduce_hypotheses(
        assessments=initial_assessments(("process_down", "port_mismatch")),
        facts=(fact("container.status", "exited", "evidence-process"),),
        rules=(rule("process_down", "container.status", "eq", "exited", "supported"),),
    )
    assert by_id(result, "process_down").disposition == "supported"
    assert by_id(result, "port_mismatch").disposition == "unresolved"

def test_causally_inactive_requires_cited_public_evidence() -> None:
    with pytest.raises(ValueError, match="public evidence"):
        HypothesisAssessment(
            hypothesis_id="port_mismatch",
            disposition="causally_inactive",
            evidence_ids=(),
            reason_code="inactive_for_failure_path",
            assessment_source="deterministic",
        )
```

再覆盖：同证据重复输入、直接反证、两个 supported、强证据冲突、未知 ID、非公开 Evidence、状态转移审计。

- [ ] **Step 2: 运行目标测试确认 RED**

```powershell
cd apps/backend
uv run pytest tests/test_aiops_hypothesis_adjudication.py -q -p no:cacheprovider
```

Expected: import 或类型缺失失败。

- [ ] **Step 3: 实现领域类型和 reducer**

核心接口固定为：

```python
Disposition = Literal["supported", "refuted", "causally_inactive", "unresolved"]
AssessmentSource = Literal["deterministic", "llm_adjudicated"]

@dataclass(frozen=True, slots=True)
class HypothesisAssessment:
    hypothesis_id: str
    disposition: Disposition
    evidence_ids: tuple[str, ...]
    reason_code: str
    assessment_source: AssessmentSource
    has_high_quality_conflict: bool = False

def reduce_hypotheses(
    *,
    assessments: Sequence[HypothesisAssessment],
    facts: Sequence[DiagnosticFact],
    rules: Sequence[HypothesisEvidenceRule],
) -> tuple[HypothesisAssessment, ...]:
    """Apply the declared transition table and return ID-sorted assessments."""
    return _reduce_with_public_evidence(assessments, facts, rules)

def assess_sufficiency(
    assessments: Sequence[HypothesisAssessment],
) -> EvidenceSufficiencyDecision:
    """Require one supported cause and no unresolved active competitor."""
    return _decision_from_dispositions(assessments)
```

Reducer 按稳定 hypothesis ID 排序，按 Evidence ID 去重；`supported` 与 `refuted` 强证据冲突时回到 `unresolved` 并置 `has_high_quality_conflict=True`。不保留浮点阈值作为状态转移依据。

- [ ] **Step 4: 更新兼容类型但不切换生产链路**

`reasoning.HypothesisState` 暂时保留旧字段供 v2/v3 读取；新增从 `HypothesisAssessment` 到旧 `status/confidence` 的只读 projection。`causally_inactive` 兼容投影为 `status=refuted`，并明确该 projection 不被新 Validator 调用。

- [ ] **Step 5: 运行测试与静态检查**

```powershell
uv run pytest tests/test_aiops_hypothesis_adjudication.py tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops/adjudication.py src/super_ai/aiops/reasoning.py tests/test_aiops_hypothesis_adjudication.py
uv run pyright
```

Expected: 全部通过。

- [ ] **Step 6: 提交领域模型**

```powershell
git add apps/backend/src/super_ai/aiops/adjudication.py apps/backend/src/super_ai/aiops/reasoning.py apps/backend/tests/test_aiops_hypothesis_adjudication.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: add auditable hypothesis dispositions"
```

---

### Task 3: 增加 typed facts 与 Planner evidence-rule DSL

**Files:**
- Create: `apps/backend/src/super_ai/aiops/facts.py`
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Create: `apps/backend/tests/test_aiops_fact_adapters.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `extract_public_facts()`、受限 predicate operators、带 `evidenceRules` 的 `DiagnosticPlanStep`。
- Consumes: 已脱敏工具 output、tool name、Evidence ID；不消费 scenario ID 或 Ground Truth。

- [ ] **Step 1: 写 APY-003 跨证据关系 RED 测试**

```python
def test_public_facts_can_compare_nginx_port_with_container_config() -> None:
    facts = extract_public_facts(
        observations=(
            observation("InspectContainer", "e1", {"status": "exited", "configuredPorts": [8080]}),
            observation("InspectNginx", "e2", {"upstreamPort": 8080, "resolvedAddresses": ["172.30.0.12"]}),
        )
    )
    assert evaluate_predicate(
        facts,
        EvidencePredicate("InspectNginx.upstreamPort", "in", "InspectContainer.configuredPorts"),
    ) is True
```

覆盖 scalar/list/object 深度限制、字段数量限制、Secret key 丢弃、键顺序规范化、未知字段只生成 facts 不产生 disposition。

- [ ] **Step 2: 实现公共 fact 提取**

```python
@dataclass(frozen=True, slots=True)
class DiagnosticFact:
    key: str
    value: JsonScalar | tuple[JsonScalar, ...]
    evidence_id: str
    source_tool: str
    quality: Literal["direct", "context"]

ALLOWED_OPERATORS = frozenset({"eq", "ne", "in", "contains", "exists", "empty", "truthy"})

def extract_public_facts(
    observations: Sequence[PublicToolObservation],
) -> tuple[DiagnosticFact, ...]:
    """Flatten bounded, secret-filtered public observation fields."""
    return tuple(_bounded_public_facts(observations))
```

只展开深度不超过 3、每条 Observation 最多 64 个 scalar facts；`secret/token/password/apiKey/authorization` 大小写无关过滤。

- [ ] **Step 3: 扩展 plan parser**

`DiagnosticPlanStep` 增加：

```python
evidence_rules: tuple[HypothesisEvidenceRule, ...] = ()
```

`parse_plan()` 只接受 `templateId + bounded parameters`，并验证模板来自代码内受信任 allowlist、hypothesis ID 公开、参数属于当前 step tool 的 fact namespace，以及 reason code 由模板固定提供。Planner 不能直接指定任意 `whenTrue/disposition` 或任意左右 fact 组合；未命中受信模板的规则按空规则处理，后续统一进入一次批量 Adjudicator，而不是被标成 deterministic 或逐 Observation 调模型。

- [ ] **Step 4: 更新 Planner prompt 与 fallback plan**

要求 Planner 对能预先表达的检查引用受信任 `evidenceRules` 模板，示例形状：

```json
{
  "templateId": "nginx_upstream_port_matches_container_port",
  "hypothesisId": "upstream_port_mismatch",
  "parameters": {
    "nginxFact": "InspectNginx.upstreamPort",
    "containerFact": "InspectContainer.configuredPorts"
  }
}
```

Prompt 明确规则是公开验证合同而非答案；不得引用场景 ID、Oracle 或未发现工具。增加反例测试：即使公共证据存在，只要 template 与 hypothesis 的因果语义不匹配，也不得关闭该假设。

- [ ] **Step 5: 验证**

```powershell
uv run pytest tests/test_aiops_fact_adapters.py tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops/facts.py src/super_ai/aiops/reasoning.py src/super_ai/aiops/diagnostics.py tests/test_aiops_fact_adapters.py
uv run pyright
```

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/facts.py apps/backend/src/super_ai/aiops/reasoning.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_fact_adapters.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: derive hypothesis facts from public observations"
```

---

### Task 4: 压缩 LangGraph 推理链并加入批量 Adjudicator

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/src/super_ai/aiops/causal_intents.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Produces: `fact_adapter`、`hypothesis_adjudicator`、确定性 `sufficiency_gate` 和确定性 `decision` 节点。
- Consumes: Tasks 2–3 的 assessments、facts 和 rules。

- [ ] **Step 1: 写模型调用数量 RED 测试**

构造四步取证场景，断言纯确定性路径只调用 Planner、Recovery Planner、Report；未解析规则路径额外只调用一次 Adjudicator，而不是每条证据调用 Evidence Evaluator/Sufficiency。

```python
assert provider.calls_by_role == {
    "planner": 1,
    "adjudicator": 1,
    "recovery_planner": 1,
    "report": 1,
}
```

- [ ] **Step 2: 重构图节点**

将图改为：

```text
START → planner → executor → fact_adapter → sufficiency_gate
sufficiency_gate → executor | replanner | hypothesis_adjudicator | decision
hypothesis_adjudicator → sufficiency_gate
decision → deterministic_validator
```

删除生产路径中的逐证据 LLM `_evidence_evaluator()` 和 LLM `_sufficiency_gate()` 调用；保留旧 payload parser 仅供 v2/v3 Artifact 读取。

- [ ] **Step 3: 实现一次批量 Adjudicator**

Adjudicator 输入只包含公开 hypotheses、全部公共 facts、Evidence IDs 和尚 unresolved IDs；输出每个 unresolved hypothesis 的 disposition、evidenceIds 和 reasonCode。解析后仍通过 `reduce_hypotheses()`，无证据的关闭状态被拒绝。`adjudication_count` 最大为 1。

- [ ] **Step 4: 将 Decision 改为确定性组装**

复用 `build_grounded_fallback_decision()` 作为主路径，使用唯一 supported hypothesis 的 public decision label、supporting Observation summaries 和因果角色组装 component/mechanism/trigger/causalChain；不再调用主模型生成 Decision。没有唯一结论时保存 `insufficient_evidence`。

- [ ] **Step 5: 更新 Deterministic Validator**

把 `no_open_competitor` 替换为：

```python
no_unresolved_active_competitor = not unresolved_ids
closed_alternatives_are_grounded = all(
    item.evidence_ids
    for item in assessments
    if item.disposition in {"refuted", "causally_inactive"}
)
```

Validator 只读取 disposition，不读取兼容 `status` 或 confidence。

- [ ] **Step 6: 验证与提交**

```powershell
uv run pytest tests/test_aiops_hypothesis_adjudication.py tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py tests/test_aiops_causal_intents.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py
uv run pyright
git add apps/backend/src/super_ai/aiops apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_decision_validation.py apps/backend/tests/test_aiops_causal_intents.py
git commit -m "refactor: make evidence adjudication deterministic"
```

---

### Task 5: 条件式 Validator、角色级超时与调用预算

**Files:**
- Create: `apps/backend/src/super_ai/aiops/validator_routing.py`
- Create: `apps/backend/src/super_ai/aiops/model_budget.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Create: `apps/backend/tests/test_aiops_validator_routing.py`
- Create: `apps/backend/tests/test_aiops_model_budget.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `requires_llm_validation()`、`ModelCallBudget`、安全的 call audit。
- Consumes: assessment source、恢复计划、风险等级、因果组件和冲突标记。

- [ ] **Step 1: 写六项触发规则 RED 测试**

```python
@pytest.mark.parametrize(
    "override",
    [
        {"used_llm_adjudication": True},
        {"execution_requested": True},
        {"max_risk_tier": "L2"},
        {"compound_root_cause": True},
        {"causal_components": ("nginx", "checkout")},
        {"has_high_quality_conflict": True},
    ],
)
def test_each_risk_condition_requires_llm_validator(override: dict[str, object]) -> None:
    assert requires_llm_validation(replace(BASE_CONTEXT, **override)).required
```

再覆盖纯确定性路径返回 `required=False, reasonCodes=[]`。Deterministic Validator 失败优先 fail closed；此时不调用 LLM Validator，即使风险上下文为真。

- [ ] **Step 2: 调整 Validator 图顺序**

```text
decision → deterministic_validator
deterministic_validator invalid → 未使用且仍合格的 targeted replanner；否则生成确定性 manual-review plan → policy_gate
deterministic_validator valid → recovery_planner
recovery_planner → validator_router
validator_router required → llm_validator → policy_gate
validator_router skipped → policy_gate
```

确定性校验失败后不得再消耗 Recovery Planner 模型调用；若 targeted replan 已用尽或不合格，直接生成 `manual_review/executionPermitted=false` 的确定性方案。当前 v4 单根因合同要求恰好一个 `supported`，因此多个 supported cause 会先 fail closed；`compound_root_cause` 仅为未来或显式 compound schema 的 Validator 路由条件，本轮不会绕过单根因门禁。

记录 `validationRequired`、`validationSkipped` 和 allowlisted `validationReasonCodes`。

- [ ] **Step 3: 实现模型预算**

```python
ROLE_TIMEOUT_SECONDS = {
    "planner": 60,
    "replanner": 60,
    "adjudicator": 60,
    "validator": 60,
    "recovery_planner": 90,
    "report": 90,
}

@dataclass(slots=True)
class ModelCallBudget:
    hard_limit: int = 8
    used: int = 0

    def reserve(self, role: ModelRole) -> int:
        if self.used >= self.hard_limit:
            raise ModelCallBudgetExceeded("model_call_budget_exhausted")
        self.used += 1
        return self.used
```

网络错误最多一次重试，Schema correction 最多一次；两者均调用 `reserve()`。`AiopsDiagnosticState` 和 LangGraph checkpoint 必须保存 `model_call_count` 以及仅含 role、attempt、durationMs、cacheHit、safe error code 的模型调用审计；恢复时从持久化计数重建 `ModelCallBudget`，Worker 重启不得重置 8 次硬上限。不保存 Prompt/response。

首次启动同时保存 UTC `started_at`、`soft_deadline_at` 和 `hard_deadline_at`。软截止后禁止新增 Replanner/Adjudicator 调用并转人工复核；硬截止后只允许持久化安全终态和模板报告。恢复严格复用原 deadline；`max_replans=1`，不得沿用当前默认值 2。

- [ ] **Step 4: 增加失败降级测试**

验证 Validator 失败且 deterministic 通过时保留结论但强制 `manual_review/executionPermitted=false`；Report 超时生成模板；Recovery Planner 超时不执行动作；预算耗尽产生人工复核而非无限循环。

- [ ] **Step 5: 验证与提交**

```powershell
uv run pytest tests/test_aiops_validator_routing.py tests/test_aiops_model_budget.py tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops/validator_routing.py src/super_ai/aiops/model_budget.py src/super_ai/aiops/diagnostics.py tests/test_aiops_validator_routing.py tests/test_aiops_model_budget.py
uv run pyright
git add apps/backend/src/super_ai/aiops apps/backend/tests/test_aiops_validator_routing.py apps/backend/tests/test_aiops_model_budget.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_decision_validation.py
git commit -m "feat: conditionally validate risky diagnoses"
```

---

### Task 6: 增加 PostgreSQL 执行与 LangGraph checkpoint schema

**Files:**
- Create: `apps/backend/alembic/versions/202608190001_add_aiops_execution_checkpoints.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Create: `apps/backend/src/super_ai/memory/aiops_execution_sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/tests/test_memory_migrations.py`
- Modify: `apps/backend/tests/test_postgresql_migrations.py`
- Create: `apps/backend/tests/test_aiops_execution_repository.py`

**Interfaces:**
- Produces: `AiopsExecutionRepository`、`LangGraphCheckpointRepository` 和 PostgreSQL 唯一约束。
- Consumes: owner/task scope、稳定 key、LangGraph serialized blobs。

- [ ] **Step 1: 写 migration/repository RED 测试**

要求以下表和约束：

```text
aiops_execution_records
  execution_key PK
  owner_user_id, task_id, graph_version, execution_kind
  node_name, logical_iteration, input_fingerprint
  status, attempt_count, lease_owner, lease_expires_at
  side_effecting, outcome_known, output_payload, safe_error_code

aiops_langgraph_checkpoints
  UNIQUE(thread_id, checkpoint_ns, checkpoint_id)
  owner_user_id, task_id, graph_version, parent_checkpoint_id
  checkpoint_type, checkpoint_blob, metadata_type, metadata_blob

aiops_langgraph_writes
  diagnostic_task_id, write_task_id
  UNIQUE(thread_id, checkpoint_ns, checkpoint_id, write_task_id, task_path, write_index)
  channel, value_type, value_blob
```

其中 `diagnostic_task_id` 是本系统诊断任务作用域，`write_task_id` 是 LangGraph `aput_writes()` 传入的内部 task ID；仓储所有读写除 owner/graph version 外还必须校验 `diagnostic_task_id`，不得用 LangGraph 内部 ID 替代租户任务隔离。

所有 JSON 为 JSONB，blob 为 BYTEA，task 外键 `ON DELETE CASCADE`。

- [ ] **Step 2: 实现 Alembic migration**

Revision 固定 `202608190001`，`down_revision="202608170001"`。status 添加数据库 check：`running/completed/failed/uncertain`；execution kind check：`node/model/tool/recovery`。

- [ ] **Step 3: 定义仓储接口**

```python
class AiopsExecutionRepository(Protocol):
    async def claim(self, request: ExecutionClaim) -> ExecutionClaimResult: raise NotImplementedError
    async def complete(self, *, execution_key: str, lease_owner: str, output: JsonDict) -> ExecutionRecord: raise NotImplementedError
    async def fail(self, *, execution_key: str, lease_owner: str, error_code: str, outcome_known: bool) -> ExecutionRecord: raise NotImplementedError
    async def get(self, execution_key: str) -> ExecutionRecord | None: raise NotImplementedError

class LangGraphCheckpointRepository(Protocol):
    async def get_tuple(self, identity: CheckpointIdentity) -> StoredCheckpointTuple | None: raise NotImplementedError
    async def list_tuples(self, query: CheckpointQuery) -> Sequence[StoredCheckpointTuple]: raise NotImplementedError
    async def put_checkpoint(self, record: StoredCheckpoint) -> None: raise NotImplementedError
    async def put_writes(self, records: Sequence[StoredCheckpointWrite]) -> None: raise NotImplementedError
```

- [ ] **Step 4: 实现冲突安全 SQLAlchemy 仓储**

claim 使用 PostgreSQL `INSERT ... ON CONFLICT DO NOTHING`，随后在新的事务中读取/`FOR UPDATE`；不得捕获 `IntegrityError` 后继续使用 aborted session。过期 lease 递增 attempt 并换 owner；`uncertain + side_effecting` 返回人工复核，不重新 claim。

- [ ] **Step 5: 运行 PostgreSQL 专项测试**

```powershell
uv run pytest tests/test_memory_migrations.py tests/test_postgresql_migrations.py tests/test_aiops_execution_repository.py -q -p no:cacheprovider
uv run ruff check src/super_ai/memory/models.py src/super_ai/memory/repositories.py src/super_ai/memory/aiops_execution_sqlalchemy.py alembic/versions/202608190001_add_aiops_execution_checkpoints.py tests/test_aiops_execution_repository.py
uv run pyright
```

Expected: 两个并发 claim 只有一个取得执行权；另一个得到 wait/reuse，唯一冲突后连接仍可查询。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/alembic/versions/202608190001_add_aiops_execution_checkpoints.py apps/backend/src/super_ai/memory apps/backend/tests/test_memory_migrations.py apps/backend/tests/test_postgresql_migrations.py apps/backend/tests/test_aiops_execution_repository.py
git commit -m "feat: persist idempotent aiops executions"
```

---

### Task 7: 接入 LangGraph checkpointer 与节点执行协调器

**Files:**
- Create: `apps/backend/src/super_ai/aiops/checkpointing.py`
- Create: `apps/backend/src/super_ai/aiops/execution.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Create: `apps/backend/tests/test_aiops_checkpointing.py`
- Create: `apps/backend/tests/test_aiops_execution_coordinator.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`

**Interfaces:**
- Produces: `PostgresDiagnosticCheckpointSaver`、`ExecutionCoordinator.run_once()`、稳定 fingerprint。
- Consumes: Task 6 仓储、LangGraph `BaseCheckpointSaver` async API。

- [ ] **Step 1: 写 saver round-trip RED 测试**

覆盖 `aput/aget_tuple/alist/aput_writes`、parent chain、重复写幂等、tenant/task 隔离。使用 LangGraph serializer 的 typed bytes，不把任意 Python 对象直接 JSON 编码。

- [ ] **Step 2: 实现异步 checkpointer**

```python
class PostgresDiagnosticCheckpointSaver(BaseCheckpointSaver[int]):
    def __init__(self, repository, *, owner_user_id: str, task_id: str, graph_version: str):
        super().__init__()
        self._repository = repository
        self._scope = (owner_user_id, task_id, graph_version)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await _load_checkpoint_tuple(self, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for item in _list_checkpoint_tuples(self, config, filter, before, limit):
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions) -> RunnableConfig:
        return await _store_checkpoint(self, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path="") -> None:
        await _store_checkpoint_writes(self, config, writes, task_id, task_path)
```

同步方法保持未实现；诊断只使用 `astream`。`thread_id` 固定 `aiops:{task_id}:aiops-diagnostic-v2`。

- [ ] **Step 3: 写节点执行幂等 RED 测试**

```python
async def test_same_execution_key_runs_operation_once() -> None:
    first, second = await asyncio.gather(
        coordinator.run_once(identity, operation),
        coordinator.run_once(identity, operation),
    )
    assert operation.await_count == 1
    assert {first.cache_hit, second.cache_hit} == {False, True}
```

覆盖 canonical JSON key 顺序、相同 input fingerprint、租约接管、completed 复用和 uncertain 副作用拒绝。

另覆盖模型预算和 deadline 跨恢复持久化：先消费若干模型调用并保存 checkpoint，使用新 Worker/Service 实例恢复，验证下一次调用从原 `model_call_count` 继续且总计不超过 8；软/硬截止时间与 Replanner 次数均不得因重启重置。

- [ ] **Step 4: 接入 graph.compile(checkpointer=...)**

首次运行向 `graph.astream(initial_state, config=...)` 传入初始状态；检测到同 thread 未完成 checkpoint 时使用 `graph.astream(None, config=...)` 续跑。每个 LLM/tool 节点内部再经 `ExecutionCoordinator`，防止“远端完成但图 checkpoint 尚未保存”的窗口重复追加状态。

- [ ] **Step 5: 将手写审计 checkpoint 稳定化**

现有 `aiops_graph_checkpoints` 继续只作审计事件，`checkpoint_id/event_id` 改为由 `task_id + graph_version + node + logical_iteration + payload_fingerprint` 派生；重复保存返回已有行，不产生第二条证据链事件。初始告警 Evidence、诊断 Step/Evidence、工具审计和报告 Evidence link 同样由对应 execution/tool key 派生稳定 ID，并由仓储 conflict-safe upsert；能与 `execution_record.complete` 同事务提交的记录必须同事务提交，跨事务记录则依靠稳定 ID 收敛。

- [ ] **Step 6: 验证与提交**

```powershell
uv run pytest tests/test_aiops_checkpointing.py tests/test_aiops_execution_coordinator.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops/checkpointing.py src/super_ai/aiops/execution.py src/super_ai/aiops/diagnostics.py tests/test_aiops_checkpointing.py tests/test_aiops_execution_coordinator.py
uv run pyright
git add apps/backend/src/super_ai/aiops apps/backend/src/super_ai/memory apps/backend/tests/test_aiops_checkpointing.py apps/backend/tests/test_aiops_execution_coordinator.py apps/backend/tests/test_aiops_diagnostics.py
git commit -m "feat: resume aiops graphs from postgres checkpoints"
```

---

### Task 8: 工具、恢复动作和后台任务的安全重试

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/jobs/runtime.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/recovery.py`
- Create: `apps/backend/tests/test_aiops_network_resume.py`
- Modify: `apps/backend/tests/test_postgresql_background_jobs.py`
- Modify: `apps/backend/tests/test_live_recovery_policy.py`

**Interfaces:**
- Produces: stable `tool_call_key`、`recovery_intent_id`、网络中断后的 job retry。
- Consumes: ExecutionCoordinator 和 BackgroundJob PostgreSQL lease。

- [ ] **Step 1: 写网络与副作用 RED 测试**

覆盖：SSE 断开不取消 job；Worker 在 checkpoint 后崩溃不重复模型调用；PostgreSQL 短断后从上个 checkpoint 恢复；只读工具可重试；恢复调用返回未知时不重放；容器/Runtime 重启后过期 lease 只被一个 Worker接管；初始告警 Evidence 或诊断副作用已提交、但 execution complete/checkpoint 保存前崩溃时，恢复不得重复创建 Evidence、Step 或 tool audit。

- [ ] **Step 2: 生成稳定 key**

```python
tool_call_key = stable_hash(
    task_id,
    plan_step_id,
    tool_name,
    canonical_json(arguments),
)
recovery_intent_id = stable_hash(
    run_or_task_id,
    action,
    target,
    canonical_json(arguments),
)
```

只读工具 `failed/outcome_known=True` 可按预算重试；副作用请求断网后保存 `uncertain/outcome_known=False`，必须先执行状态探针，不能直接再次调用。

- [ ] **Step 3: 允许后台任务有限重试**

创建 AIOps job 时将 `max_attempts` 从 1 改为 3。`AiopsDiagnosticService.stream()` 对 allowlisted transient infrastructure failure 不写终端 `failed`，而是抛出安全异常交给 BackgroundJobRuntime；确定性合同失败仍写终态，不重试。retry 使用同 task ID 和 graph version。

- [ ] **Step 4: 接入 Live recovery intent**

在 Live runner 调用 `recover()` 前 claim `execution_kind=recovery`；完成后保存 verification 摘要。若 claim 返回 uncertain，生成 `recovery_denied/uncertain_previous_attempt`，执行 cleanup，不再次终止连接或重启服务。

- [ ] **Step 5: 验证与提交**

```powershell
uv run pytest tests/test_aiops_network_resume.py tests/test_postgresql_background_jobs.py tests/test_live_recovery_policy.py tests/test_live_benchmark_cli.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops/diagnostics.py src/super_ai/api/app.py src/super_ai/jobs/runtime.py src/super_ai/evaluation/live/runner.py src/super_ai/evaluation/live/recovery.py tests/test_aiops_network_resume.py
uv run pyright
git add apps/backend/src/super_ai apps/backend/tests/test_aiops_network_resume.py apps/backend/tests/test_postgresql_background_jobs.py apps/backend/tests/test_live_recovery_policy.py apps/backend/tests/test_live_benchmark_cli.py
git commit -m "feat: safely retry interrupted aiops work"
```

---

### Task 9: v4 Artifact、评分兼容与可观测性

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/src/super_ai/evaluation/scoring.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_scoring.py`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`

**Interfaces:**
- Produces: v4 disposition Artifact、v2/v3 兼容 reader、调用耗时与 cache audit。

- [ ] **Step 1: 写 v2/v3/v4 兼容 RED 测试**

v2/v3 `open/supported/refuted` 分别投影为 `unresolved/supported/refuted`；v4 必须存在 disposition、reasonCode、assessmentSource 和关闭状态 Evidence。未知 disposition 使 Artifact 无效，不回退旧 status。为已归档的 v2/v3 fixture 固定升级前基线，断言升级后总分、hard gate、required evidence 以及 Validator/recovery 解释完全不变；v4 门禁只能由 `workflowVersion=evidence-driven-v4` 分派启用。

- [ ] **Step 2: 更新 Artifact 类型与评分输入**

```python
@dataclass(frozen=True, slots=True)
class ArtifactHypothesisAssessment:
    id: str
    disposition: Disposition
    evidence_ids: tuple[str, ...]
    reason_code: str | None
    assessment_source: AssessmentSource | None
```

Differential diagnosis 将 `refuted` 与有证据的 `causally_inactive` 视为已关闭替代项；没有 Evidence 的 v4 closed state 触发 hard gate，不能获得分数。

- [ ] **Step 3: 输出兼容 API 与安全观测**

证据链 API 同时输出 `status` 和 `disposition`，并新增 allowlisted：`graphVersion`、`workflowVersion`、`modelCallCount`、每角色 attempts/duration/cacheHit、Validator trigger/skip reason、resume count。不得输出 execution blob、Prompt 或模型响应。

- [ ] **Step 4: 验证与提交**

```powershell
uv run pytest tests/test_evaluation_artifacts.py tests/test_evaluation_scoring.py tests/test_live_evaluation_scoring.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider
uv run ruff check src/super_ai/evaluation src/super_ai/api/app.py tests/test_evaluation_artifacts.py tests/test_evaluation_scoring.py tests/test_live_evaluation_scoring.py
uv run pyright
git add apps/backend/src/super_ai/evaluation apps/backend/src/super_ai/api/app.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_evaluation_scoring.py apps/backend/tests/test_live_evaluation_scoring.py apps/backend/tests/test_aiops_diagnostics.py
git commit -m "feat: score auditable v4 diagnosis artifacts"
```

---

### Task 10: 分阶段回归、真实 Benchmark 和交付

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/add-auditable-hypothesis-adjudication/tasks.md`
- Create: `docs/aiops/auditable-adjudication-acceptance-2026-08-19.md`

**Interfaces:**
- Consumes: Tasks 1–9、既有 30 卡 RAG、真实模型配置、10 Snapshot、4 Live。
- Produces: 可复核差分结果和可合并分支。

- [ ] **Step 1: 运行离线专项回归**

```powershell
cd apps/backend
$taskPytestTemp = Join-Path $env:TEMP ('agentpy-adjudication-' + [guid]::NewGuid().ToString('N'))
uv run pytest tests/test_aiops_hypothesis_adjudication.py tests/test_aiops_fact_adapters.py tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py tests/test_aiops_validator_routing.py tests/test_aiops_model_budget.py tests/test_aiops_execution_repository.py tests/test_aiops_checkpointing.py tests/test_aiops_execution_coordinator.py tests/test_aiops_network_resume.py tests/test_evaluation_artifacts.py tests/test_evaluation_scoring.py tests/test_live_evaluation_scoring.py tests/test_snapshot_benchmark_runner.py tests/test_snapshot_evaluation_tools.py tests/test_evaluation_scenarios.py tests/test_live_evaluation_scenarios.py tests/test_tool_argument_contracts.py -q -p no:cacheprovider --basetemp $taskPytestTemp
uv run ruff check .
uv run pyright
```

Expected: 全部通过；本任务不要求先跑全量 pytest。

隔离专项必须覆盖 `--scenario ../APY-003` 路径穿越、嵌套 `oracle/primary_cause`、Agent 调用 `ReadGroundTruth`、Prompt/RAG/public context 不含 Ground Truth，以及 checkpoint blob/v4 API 不输出敏感字段。

- [ ] **Step 2: 验证 migration 与 OpenSpec**

```powershell
uv run alembic upgrade head
uv run alembic current
cd ../..
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-auditable-hypothesis-adjudication --strict
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate --all
```

Expected: revision `202608190001` 为唯一 head，OpenSpec 全部有效。

- [ ] **Step 3: 运行 APY-003 单场景验收**

沿用现有正式 Snapshot 命令和独立新 run ID。要求：唯一 supported 根因正确；port/DNS alternative 被公开 Evidence 关闭；无 unresolved competitor；模型调用不超过 8；结果同时保存 Archive/PostgreSQL。

- [ ] **Step 4: 运行 APY-002 性能验收**

要求根因和 required evidence 不回退、模型调用不超过 8；记录总耗时、每角色耗时和 cache hit。180～300 秒是目标，不因供应商波动直接修改功能或评分。

- [ ] **Step 5: 顺序运行剩余 8 个 Snapshot 与 4 个 Live**

按场景 ID 逐个运行，Snapshot 命令固定为：

```powershell
uv run python scripts/run_snapshot_benchmark.py --scenario $scenarioId --suite-version v1 --runs 1 --adapter application --rag-mode on --owner-user-id $benchmarkOwnerId --knowledge-base-id $benchmarkKnowledgeBaseId --config $sharedProjectConfig --campaign-id $acceptanceCampaignId --output ("var/benchmarks/auditable-v4-{0}.json" -f $scenarioId)
```

Live 命令固定为：

```powershell
$liveRunId = ('auditable-v4-{0}-{1}' -f $scenarioId.ToLowerInvariant(), [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
uv run python -m super_ai.evaluation.live.cli run --scenario $scenarioId --run-id $liveRunId --owner-user-id $benchmarkOwnerId --knowledge-base-id $benchmarkKnowledgeBaseId --config $sharedProjectConfig --campaign-id $acceptanceCampaignId --evidence-source cls
```

每个 Live 前后分别运行已有 scoped preflight/verify/cleanup；每个场景使用唯一 run ID。不得批量掩盖首个失败；首个失败立即停止，修复必须先增加目标回归测试。

- [ ] **Step 6: 写安全差分报告**

报告只列 scenario/run ID、Git SHA、graph/workflow version、分数、hard gate、required evidence、模型调用数、耗时、Validator origin/reason、恢复模式、cleanup 和 Archive/PostgreSQL 对账。不得复制 Ground Truth、Prompt、模型原文或原始 CLS 日志。

- [ ] **Step 7: 最终提交**

```powershell
git add docs/aiops/agentpy-domainbench.md docs/aiops/auditable-adjudication-acceptance-2026-08-19.md openspec/changes/add-auditable-hypothesis-adjudication/tasks.md
git commit -m "docs: record auditable adjudication acceptance"
git status --short
```

Expected: 工作区干净；全部失败历史仍保留；未提交本机配置、Archive、数据库或 `var/` 结果。
