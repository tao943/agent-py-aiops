# Nginx Live Deterministic Resolution and Routing Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先以可泛化的公开证据模式跑通 Nginx proposal-only Live 闭环，再减少确定性证据已闭合时的 Adjudicator 调用和无法产生新步骤时的 Replanner 模型调用。

**Architecture:** 在现有四态裁决层增加代码拥有的复合 evidence pattern；Fact Adapter 先执行已有单事实规则，再执行复合模式并生成确定性因果 Observation。LangGraph 主图和同步 Report 保持不变；Replanner 在调用模型前执行纯函数预检，只有无法证明工具空间已耗尽时才允许模型规划。

**Tech Stack:** Python 3.10、LangGraph 1.2.8、Pydantic 2、SQLAlchemy Async、PostgreSQL、Milvus、Redis Retrieval Cache、腾讯云 CLS、pytest、Ruff、Pyright、OpenSpec。

## Global Constraints

- 严格按阶段执行：Nginx Live 修复真实验收通过后，才实施通用 Replanner 路由优化。
- Nginx 恢复固定为 `proposal_only`，`executionPermitted=false`，不得修改、重载或覆盖 Nginx 配置。
- 规则不得读取或匹配 Scenario ID、Run ID、Oracle、Ground Truth、评分规则或固定毫秒值。
- 关闭状态必须引用当前任务持久化的公开 Evidence；不能仅因另一个根因已成立而关闭竞争假设。
- 不降低 Snapshot/Live 评分阈值、必要证据、恢复授权或答案隔离门禁。
- 不新增依赖；不复制 GitHub 候选代码；复用现有 LangGraph、四态 reducer、Fact Adapter、Redis Retrieval Cache 和 ExecutionCoordinator。
- 不拆分或并行 Adjudicator，不并行主链 LLM 节点，不新增跨任务 LLM 裁决缓存。
- 不并行诊断工具，不异步化 Report，不改变 API/SSE 完成语义。
- 所有生产代码变更先写目标测试并观察正确 RED；不运行全量 pytest。
- 真实 Live 失败后立即 Cleanup 并停止，不连续消耗模型额度。
- 私有配置、API Key、CLS 凭据、原始日志、Archive 和 `var/` 不得提交。
- 实现和验证由主 Agent 单独完成；唯一子 Agent 只允许在实施前审查本计划，不得编辑文件。

## File Structure

- `apps/backend/src/super_ai/aiops/adjudication.py`：新增复合受信模式类型、确定性状态转换和匹配入口；保持单事实 reducer 行为兼容。
- `apps/backend/src/super_ai/aiops/facts.py`：把 Nginx/CLS 的受控字段标记为可参与确定性裁决的 direct public facts。
- `apps/backend/src/super_ai/aiops/trusted_patterns.py`：保存不依赖场景身份的 Nginx timeout 复合模式和确定性因果 Observation 投影。
- `apps/backend/src/super_ai/aiops/diagnostics.py`：在 Fact Adapter 中接入复合模式；为 Replanner 增加模型调用前的可用步骤预检。
- `apps/backend/src/super_ai/evaluation/live/nginx_timeout.py`：在现有只读健康工具中加入独立 Nginx gateway 探针结果，使 gateway pressure 反证在真实 Live 工具链可达且不增加工具调用次数。
- `apps/backend/tests/test_aiops_trusted_patterns.py`：正向、反事实、冲突、身份无关和 Evidence 归属测试。
- `apps/backend/tests/test_aiops_v4_workflow.py`：Fact Adapter、Sufficiency、Adjudicator/Replanner 路由和模型调用审计测试。
- `apps/backend/tests/test_live_diagnostic_adapter.py`：真实 Live 工具合同进入生产诊断图后的结构化 Artifact 测试。
- `openspec/changes/add-auditable-hypothesis-adjudication/`：同步复合受信模式和无用 Replan 跳过合同。
- `docs/aiops/agentpy-domainbench.md`：记录 Nginx 最终 Run、模型角色、提案边界和耗时差分。

---

### Task 1: 固化 OpenSpec 增量合同

**Files:**
- Modify: `openspec/changes/add-auditable-hypothesis-adjudication/design.md`
- Modify: `openspec/changes/add-auditable-hypothesis-adjudication/tasks.md`
- Modify: `openspec/changes/add-auditable-hypothesis-adjudication/specs/aiops-diagnosis-tasks/spec.md`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-08-20-nginx-live-deterministic-resolution-and-routing-optimization-design.md`.
- Produces: trusted compound pattern、identity isolation 和 no-useful-replan 的 SHALL/MUST 合同。

- [ ] **Step 1: 添加复合受信模式 Requirement**

在 delta spec 增加：

```markdown
### Requirement: Compound trusted patterns remain answer-isolated
Workflow SHALL close multiple hypotheses deterministically only when a code-owned
compound pattern matches current-task public Evidence. The pattern MUST NOT read
scenario identity, run identity, Oracle, Ground Truth, score rules, or fixture values.

#### Scenario: Nginx timeout facts close differentiated alternatives
- **WHEN** one request has HTTP 504, upstream connect success, read deadline elapsed,
  independently healthy upstream and gateway probes, and an incident-scoped
  upstream-timeout event
- **THEN** Workflow SHALL support upstream response timeout
- **AND** every closed competitor MUST cite the direct Evidence that closes it

#### Scenario: One required fact is absent
- **WHEN** any required fact is absent or contradicted
- **THEN** Workflow MUST leave the affected hypothesis unresolved or conflicting
- **AND** it MUST NOT infer the benchmark answer from scenario identity
```

- [ ] **Step 2: 添加 Replanner 预检 Requirement**

```markdown
### Requirement: Replanner model calls require a provably useful search space
Before calling the Replanner model, Workflow SHALL determine whether at least one
unexecuted contract-valid step can still address the current evidence gap. If no such
step can exist under bounded trusted arguments, Workflow MUST persist no_useful_step
without spending a Replanner model call.
```

- [ ] **Step 3: 更新 tasks 与 design 边界**

在 tasks 增加可勾选项：复合模式、反事实测试、真实 Nginx 验收、Replanner 预检、性能审计；在 design
明确 RAG 并发/缓存已存在，Adjudicator 保持一次批量调用。

- [ ] **Step 4: 运行 OpenSpec RED/GREEN 结构验证**

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-auditable-hypothesis-adjudication --strict
```

Expected: `add-auditable-hypothesis-adjudication` valid，退出码 0。

- [ ] **Step 5: 提交规范**

```powershell
git add openspec/changes/add-auditable-hypothesis-adjudication
git commit -m "spec: define trusted nginx resolution"
```

---

### Task 2: 以 TDD 建立复合受信证据模式

**Files:**
- Create: `apps/backend/src/super_ai/aiops/trusted_patterns.py`
- Modify: `apps/backend/src/super_ai/aiops/adjudication.py`
- Modify: `apps/backend/src/super_ai/aiops/facts.py`
- Create: `apps/backend/tests/test_aiops_trusted_patterns.py`

**Interfaces:**
- Consumes: `Sequence[HypothesisAssessment]`、`Sequence[DiagnosticFact]`、由当前任务持久化 Evidence 建立的可信 ID 集。
- Produces: `TrustedPatternResolution(assessments, observations, matched_pattern_ids)`。
- Produces: `apply_deterministic_transition(...) -> HypothesisAssessment`，供复合模式复用 reducer 的冲突语义。

- [ ] **Step 1: 写完整 Nginx 模式的 RED 测试**

测试使用变化后的时长和任意 Evidence ID，不传 Scenario/Run ID：

```python
def test_nginx_timeout_pattern_closes_each_hypothesis_with_direct_evidence() -> None:
    result = resolve_trusted_patterns(
        assessments=initial_nginx_assessments(),
        facts=nginx_timeout_facts(duration_ms=913),
        trusted_evidence_ids=nginx_timeout_evidence_ids(),
    )
    by_id = {item.hypothesis_id: item for item in result.assessments}
    assert by_id["nginx_upstream_response_timeout"].disposition == "supported"
    assert by_id["nginx_route_mismatch"].disposition == "refuted"
    assert by_id["nginx_upstream_unavailable"].disposition == "refuted"
    assert by_id["nginx_gateway_pressure"].disposition == "causally_inactive"
    assert all(item.evidence_ids for item in by_id.values())
    assert [item["causalRole"] for item in result.observations] == [
        "trigger", "mechanism", "impact"
    ]
    assert result.matched_pattern_ids == ("nginx_upstream_read_timeout",)
```

- [ ] **Step 2: 写反事实、Evidence 归属和防过拟合 RED 测试**

分别覆盖：connect failed、upstream unhealthy、gateway 独立探针失败或明显变慢、deadline not elapsed、
缺少 CLS `upstream_timeout`。每个测试断言 timeout 不得成为唯一 supported，且缺失/冲突项仍为
unresolved。将结构正确但 Evidence ID 不属于 `trusted_evidence_ids` 的另一任务 Fact 混入时必须忽略且
不得匹配；再以不同 duration 和无关 `scenarioId/runId` shaped facts 证明结果只由允许的当前任务语义
事实决定。

- [ ] **Step 3: 运行测试确认正确 RED**

```powershell
cd apps/backend
uv run pytest tests/test_aiops_trusted_patterns.py -q -p no:cacheprovider
```

Expected: `ModuleNotFoundError: super_ai.aiops.trusted_patterns` 或缺少接口导致 FAIL。

- [ ] **Step 4: 增加 direct public fact allowlist**

在 `facts.py` 的 `_DIRECT_FACT_KEYS` 增加且只增加：

```python
"InspectNginxRequestTimeline.gatewayStatus",
"InspectNginxRequestTimeline.requestDurationMs",
"InspectNginxRequestTimeline.upstreamConnectSucceeded",
"ReadNginxTimeoutSummary.gatewayTimeoutObserved",
"ReadNginxTimeoutSummary.readDeadlineElapsed",
"ProbeLiveEvalUpstream.status",
"ProbeLiveEvalUpstream.healthy",
"ProbeLiveEvalUpstream.gatewayStatus",
"ProbeLiveEvalUpstream.gatewayHealthy",
"ProbeLiveEvalUpstream.gatewayLatencyMs",
"SearchLog.records.event",
```

`SearchLog.records.event` 仅接受 secret-filtered flatten 产生的字符串 tuple；模式只检查是否包含
`upstream_timeout`，不检查 run/scenario 值。

- [ ] **Step 5: 提取可审计确定性状态转换接口**

在 `adjudication.py` 增加：

```python
def apply_deterministic_transition(
    assessment: HypothesisAssessment,
    *,
    disposition: Disposition,
    evidence_ids: Sequence[str],
    reason_code: str,
) -> HypothesisAssessment:
    """Apply one evidence-cited transition and preserve existing conflict behavior."""
```

接口必须：去重 Evidence ID、拒绝无 Evidence 的关闭状态、追加 `HypothesisTransition`，并保持重复
输入幂等。批量 reducer 必须先归并同一 hypothesis 的全部 proposed outcomes，再一次性转换；一旦
历史 transition 或当前批次存在相反关闭状态，结果必须保持
`unresolved/high_quality_evidence_conflict`，后续同向事实不得重新关闭。增加 A→B、B→A、重复运行和
新增无关 Fact 后冲突仍 unresolved 的顺序无关测试。`reduce_hypotheses()` 改为复用该批量语义，现有
单事实 reducer 测试必须不变。

- [ ] **Step 6: 实现 `trusted_patterns.py`**

定义：

```python
@dataclass(frozen=True, slots=True)
class TrustedPatternResolution:
    assessments: tuple[HypothesisAssessment, ...]
    observations: tuple[dict[str, object], ...]
    matched_pattern_ids: tuple[str, ...]

def resolve_trusted_patterns(
    *,
    assessments: Sequence[HypothesisAssessment],
    facts: Sequence[DiagnosticFact],
    trusted_evidence_ids: AbstractSet[str],
) -> TrustedPatternResolution:
    """Apply code-owned cross-tool patterns without scenario or Oracle input."""
```

实现必须先丢弃 Evidence ID 不在可信 ID 集中的 Fact，再在 HTTP 504、upstream connect success、
read deadline elapsed、upstream 独立健康、gateway 独立健康且低延迟、incident-scoped CLS
`upstream_timeout` 完整时触发；任一健康探针冲突时 fail closed。每个 hypothesis transition 只引用
关闭它所需的 Evidence。Observation 使用 `assessmentSource="deterministic"`、
`causalRoleOrigin="trusted_compound_pattern"`，并分别生成 trigger/mechanism/impact。

- [ ] **Step 7: 运行 GREEN 与相邻 reducer 回归**

```powershell
uv run pytest tests/test_aiops_trusted_patterns.py tests/test_aiops_hypothesis_adjudication.py -q -p no:cacheprovider
```

Expected: PASS，且反事实测试不触发模式。

- [ ] **Step 8: 提交领域能力**

```powershell
git add apps/backend/src/super_ai/aiops/adjudication.py apps/backend/src/super_ai/aiops/facts.py apps/backend/src/super_ai/aiops/trusted_patterns.py apps/backend/tests/test_aiops_trusted_patterns.py
git commit -m "fix: resolve nginx timeout from trusted facts"
```

---

### Task 3: 接入 Fact Adapter 并跑通离线 Nginx 诊断闭环

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/nginx_timeout.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Consumes: `resolve_trusted_patterns()`。
- Produces: Fact Adapter 返回复合模式后的 assessments、states、observations 和审计字段。

- [ ] **Step 1: 写 Fact Adapter 多工具 RED 测试**

依次向同一 state 输入四个真实工具形状；最终断言：

```python
assert sufficiency["status"] == "sufficient"
assert sufficiency_update["next_route"] == "decision"
assert decision["root_cause_decision"]["mechanism"] == (
    "upstream_response_exceeded_proxy_read_timeout"
)
assert decision["root_cause_decision"]["trigger"]
assert len(decision["root_cause_decision"]["causalChain"]) >= 2
```

模型 stub 的 Adjudicator/Replanner 分支直接 `raise AssertionError`，证明闭环不依赖这两个角色。
另加跨任务 Evidence 测试：向 `diagnostic_facts` 混入未出现在当前 state `evidence_ids` 中的外部
Evidence ID，Fact Adapter 必须过滤该 Fact 且复合模式不得据此关闭任何 hypothesis。

- [ ] **Step 2: 运行测试确认 RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py -k "nginx and trusted" -q -p no:cacheprovider
```

Expected: 最终 hypothesis 仍 unresolved 或路由仍为 `hypothesis_adjudicator`。

- [ ] **Step 3: 在 Fact Adapter 中接入复合模式**

执行顺序固定为：

```python
reduced = reduce_hypotheses(...)
trusted_ids = frozenset((*state_evidence_ids, current_persisted_evidence_id))
trusted = resolve_trusted_patterns(
    assessments=reduced,
    facts=all_facts,
    trusted_evidence_ids=trusted_ids,
)
reduced = trusted.assessments
observation_payloads.extend(trusted.observations)
```

随后再做现有 Observation 去重、causal coverage、checkpoint 和审计投影。删除旧
`_derive_nginx_timeout_observations` 对“先有唯一 supported assessment”的循环依赖；其他 PG/Redis
coverage repair 行为保持不变。

`trusted_ids` 只能来自当前任务加载的持久化 `evidence_ids` 与本轮 executor 成功持久化后返回的
`current_evidence_id`，不得从 Fact payload 自报 Evidence ID 推导。

- [ ] **Step 4: 增加真实 gateway 反证工具和 Live Adapter Artifact RED/GREEN 测试**

在 Nginx driver 的故障观察阶段，对 gateway `/health` 做独立状态和延迟探针，并通过现有零参数只读
工具 `ProbeLiveEvalUpstream` 一并暴露 sanitized 的 gateway `status/healthy/latencyMs`；upstream 与
gateway 仍是两次独立 HTTP 探针，但共享一次工具审计，保持“三个 Nginx 只读工具 + CLS”的四步预算。
健康阈值必须是代码拥有的运行合同，不得引用 Benchmark 分值或固定故障答案。测试覆盖 healthy 快速
响应、非 200、超过阈值三种真实输出；后两种必须使 trusted pattern fail closed。

使用真实 `NginxTimeoutEvidenceMcpClient` 的 sanitized 工具响应和内存模型，断言 Artifact：

- 根因 component/mechanism/trigger/causal chain 完整；
- Evidence ID 全部存在；
- 不含 Oracle/ground truth；
- 工具审计包含三个 Nginx 只读工具和 `SearchLog`；
- 不包含 Adjudicator/Replanner model audit；
- proposal tool 仍由后续 Recovery Planner/Policy Gate 负责。

- [ ] **Step 5: 运行阶段一离线目标回归**

```powershell
uv run pytest tests/test_aiops_trusted_patterns.py tests/test_aiops_v4_workflow.py tests/test_live_diagnostic_adapter.py tests/test_live_nginx_timeout_contracts.py -q -p no:cacheprovider
```

Expected: PASS。

- [ ] **Step 6: 提交生产图接入**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/live/nginx_timeout.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_live_diagnostic_adapter.py apps/backend/tests/test_live_nginx_timeout_contracts.py
git commit -m "fix: close nginx live diagnostic evidence"
```

---

### Task 4: 执行阶段一静态验证与真实 Nginx Live 验收

**Files:**
- Modify after pass: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: current private project/user config、30-card active indexed knowledge base、CLS、Docker Live Nginx/Upstream。
- Produces: one persisted formal Nginx Run and acceptance evidence; no committed runtime artifacts.

- [ ] **Step 1: 运行目标静态检查**

```powershell
cd apps/backend
uv run ruff check src/super_ai/aiops/adjudication.py src/super_ai/aiops/facts.py src/super_ai/aiops/trusted_patterns.py src/super_ai/aiops/diagnostics.py tests/test_aiops_trusted_patterns.py tests/test_aiops_v4_workflow.py tests/test_live_diagnostic_adapter.py
uv run pyright
```

Expected: 两条命令退出码 0。不运行全量 pytest。

- [ ] **Step 2: 执行 Live preflight/baseline/inject/confirm**

使用 `scripts/run_live_benchmark.py` 和现有私有配置生成唯一 Run ID；确认 Nginx、upstream、CLS、
PostgreSQL、Milvus、知识库均 ready。任一步失败立即 cleanup。

- [ ] **Step 3: 运行一次正式 LLM+CLS Nginx Run**

```powershell
uv run python scripts/run_live_benchmark.py run --scenario APY-LIVE-NGINX-TIMEOUT-001 --run-id <unique-run-id> --owner-user-id <existing-eval-user> --knowledge-base-id <active-30-card-kb> --evidence-source cls --config <project-config>
```

Expected: `status=passed`、`total=100`、`validity=VALID_PASS`。若失败，保存安全分类，立即 cleanup，
停止阶段二并回到对应 TDD seam。

- [ ] **Step 4: 独立 Verify、Cleanup 与安全核对**

```powershell
uv run python scripts/run_live_benchmark.py verify --scenario APY-LIVE-NGINX-TIMEOUT-001 --run-id <run-id>
uv run python scripts/run_live_benchmark.py cleanup --scenario APY-LIVE-NGINX-TIMEOUT-001 --run-id <run-id>
git diff --exit-code -- ../../infra/live-eval/nginx.conf
```

Expected: verify/cleanup 通过，Nginx 配置无差异。

- [ ] **Step 5: 核对持久化和历史一致性**

确认 diagnostic task=`succeeded`、evaluation run=`passed`、task link 非空、artifact checksum 存在；运行
history audit/summarize，期望 conflict=0、databasePending=0。不得输出私有配置和原始 CLS 日志。

- [ ] **Step 6: 记录阶段一结果并提交**

在 DomainBench 文档记录 Run ID、100 分、proposal-only、模型角色、总耗时、Verify/Cleanup 和配置未变；
不得粘贴原始日志。

```powershell
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record nginx live acceptance"
```

---

### Task 5: 以 TDD 跳过无意义 Replanner 模型调用

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`

**Interfaces:**
- Produces: `_bounded_replan_preflight(state, tool_definitions, trusted_tool_arguments, tool_argument_contracts, causal_capabilities) -> ReplanPreflight`。
- `ReplanPreflight` 返回 deterministic steps、`model_allowed` 和 allowlisted reason。

- [ ] **Step 1: 写“工具空间已耗尽”RED 测试**

构造所有零参数/运行时绑定参数的诊断工具均已执行、没有 deterministic fallback 的 state；模型 stub
调用即失败。断言 `_replanner`：

```python
assert update["termination_reason"] == "no_useful_step"
assert update["replan_count"] == 1
assert provider.calls == 0
assert step.payload["modelCallSkippedReason"] == "bounded_tool_space_exhausted"
```

- [ ] **Step 2: 写“仍存在可用步骤”保护测试**

覆盖两类：

1. `_deterministic_gap_replan_steps` 能生成未执行 Probe；直接加入步骤且不调用 LLM；
2. 工具允许自由但合法的有界参数，代码不能证明空间耗尽；仍允许一次 LLM Replanner。

同时保留真正 unresolved hypothesis 进入一次批量 Adjudicator 的现有测试。

- [ ] **Step 3: 运行测试确认 RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py -k "replan and (preflight or exhausted or useful)" -q -p no:cacheprovider
```

Expected: exhausted case 仍调用模型或缺少 preflight 类型而 FAIL。

- [ ] **Step 4: 实现纯函数预检**

```python
@dataclass(frozen=True, slots=True)
class ReplanPreflight:
    deterministic_steps: tuple[JsonDict, ...]
    model_allowed: bool
    reason: str

def _bounded_replan_preflight(
    state: Mapping[str, object],
    *,
    tool_definitions: Sequence[McpToolDefinition],
    trusted_tool_arguments: Mapping[str, Mapping[str, object]],
    tool_argument_contracts: Mapping[str, ToolArgumentContract],
    causal_capabilities: Mapping[str, AbstractSet[str]],
) -> ReplanPreflight:
    ...
```

调用者必须把实例上的 `_trusted_tool_arguments`、`_tool_argument_contracts` 和按工具计算的 causal
capabilities 显式传入纯函数。规则顺序：先生成并按同一生产合同规范化 deterministic gap steps；若存在
未执行步骤，直接返回这些步骤且 `model_allowed=False`。只有当每个仍可能覆盖当前 gap 的工具都具有
空参数、JSON Schema `const` 参数或 execution-owned 固定参数，并且相同 canonical fingerprint 已执行时，
才返回 `bounded_tool_space_exhausted`。任何自由参数合同或 causal intent 覆盖关系不能被证明时均返回
`model_allowed=True`，不得猜测没有搜索空间。测试分别覆盖空参数、const、trusted runtime-bound、自由
参数及工具 causal intent 不覆盖当前 gap。

- [ ] **Step 5: 在 `_replanner` 调用模型前接入**

```python
preflight = _bounded_replan_preflight(
    state,
    tool_definitions=tool_definitions,
    trusted_tool_arguments=self._trusted_tool_arguments,
    tool_argument_contracts=self._tool_argument_contracts,
    causal_capabilities={
        definition.name: allowed_causal_intents(definition.name)
        for definition in tool_definitions
    },
)
if preflight.deterministic_steps:
    parsed_steps = list(preflight.deterministic_steps)
elif not preflight.model_allowed:
    parsed_steps = []
else:
    parsed_steps = await invoke_replanner_model(...)
```

payload 增加 `modelCallSkippedReason`，但不改变既有 `terminationReason`、Replan 次数、checkpoint 和
Artifact 兼容字段。

- [ ] **Step 6: 运行 GREEN 和相关预算/恢复回归**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_execution_coordinator.py tests/test_aiops_checkpointing.py -q -p no:cacheprovider
```

Expected: PASS；真正可用的 LLM Replanner 路径仍只调用一次。

- [ ] **Step 7: 提交路由优化**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_v4_workflow.py
git commit -m "perf: skip exhausted aiops replans"
```

---

### Task 6: 最终专项回归、规格闭环与交付核对

**Files:**
- Modify: `openspec/changes/add-auditable-hypothesis-adjudication/tasks.md`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: 阶段一正式 Run 和阶段二专项测试结果。
- Produces: 可审计的最终验收记录和干净 Git 变更集。

- [ ] **Step 1: 运行最终专项 pytest**

```powershell
cd apps/backend
uv run pytest tests/test_aiops_trusted_patterns.py tests/test_aiops_hypothesis_adjudication.py tests/test_aiops_v4_workflow.py tests/test_live_diagnostic_adapter.py tests/test_live_nginx_timeout_contracts.py tests/test_live_benchmark_cli.py tests/test_evaluation_scoring.py tests/test_aiops_execution_coordinator.py tests/test_aiops_checkpointing.py -q -p no:cacheprovider
```

Expected: 全部 PASS；不运行全量 pytest。

- [ ] **Step 2: 运行 Ruff、Pyright 与 OpenSpec**

```powershell
uv run ruff check src/super_ai/aiops tests/test_aiops_trusted_patterns.py tests/test_aiops_v4_workflow.py tests/test_live_diagnostic_adapter.py
uv run pyright
cd ../..
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-auditable-hypothesis-adjudication --strict
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate --all
```

Expected: 全部退出 0。

- [ ] **Step 3: 更新任务和性能审计**

只勾选已有代码、测试和真实验收证据支持的 tasks。记录优化前 Nginx：5 次模型调用、Adjudicator
两次、Replanner 一次、约 248 秒；记录优化后实际角色和耗时，不伪造绝对性能结论。

- [ ] **Step 4: 检查敏感文件和工作区**

```powershell
git status --short
git diff --check
git diff --name-only HEAD~6..HEAD
```

Expected: 不包含 `config/project.json`、`config/user.project.json`、`var/`、Archive、原始 CLS 日志、
凭据或临时文件。

- [ ] **Step 5: 提交规格闭环**

```powershell
git add openspec/changes/add-auditable-hypothesis-adjudication/tasks.md docs/aiops/agentpy-domainbench.md
git commit -m "docs: close nginx routing acceptance"
```

- [ ] **Step 6: 最终报告**

向用户报告：正式 Run ID、得分、恢复边界、模型角色差分、总耗时、专项测试数量、静态检查、OpenSpec、
结果持久化状态，以及尚未执行的全量 pytest。不得声称未运行的检查通过。
