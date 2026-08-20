# Single/Multi-Agent 数据源路由实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. 每个任务严格执行 RED → 最小实现 → GREEN → commit；不得跳过 RED 证据。

**Goal:** 在不替换现有 AIOps 单 Agent 主链、不增加第三方依赖的前提下，为 `evidence-driven-v4` 增加可解释的 Strategy Router、受控的数据源 Investigator 并行取证、确定性 Evidence Aggregator，以及只供 Benchmark 使用的 Single/Multi/Auto A/B 能力。

**Architecture:** 复用现有 LangGraph `StateGraph`、PostgreSQL checkpointer、`ExecutionCoordinator`、Fact Adapter、Sufficiency Gate、工具审计和 Artifact。Knowledge Investigator 从现有 Planner 中抽取但仍只执行一次 RAG；Planner 之后由纯函数 Router 选择 deterministic fast path、现有 single-agent executor loop 或 runtime/log fan-out。并行分支只产出独立 `EvidencePacket`，由唯一 Aggregator 校验、排序、去重并写回共享事实状态。普通 API 固定 `auto` 且只有配置允许时才能升级；强制 `single|multi` 只从内部 Benchmark CLI 注入。

**Tech Stack:** Python 3.10、LangGraph 1.2.8、Pydantic 2、SQLAlchemy Async、PostgreSQL、Milvus、Redis Retrieval Cache、腾讯云 CLS、pytest、Ruff、Pyright、OpenSpec。

## 全局约束

- 不新增 AutoGen、OpenAI Agents SDK、CrewAI 或其他 Agent runtime；不新增数据库表或 migration。
- 新图使用 `graph_version="aiops-diagnostic-v3"` 和 thread ID `aiops:{task_id}:aiops-diagnostic-v3`；已有 `aiops-diagnostic-v2` checkpoint 只能由旧拓扑恢复，绝不送入新图。
- 不接入 GitHub PR、Issue、Commit、Actions 或 Deployment API；Change Investigator 固定不可用，原因码为 `deployment_change_source_not_configured`。
- Multi-Agent v1 仅包含 Knowledge、Runtime、Log；Knowledge 先于 Planner 完成，通常 fan-out 只有 Runtime 与 Log。
- Investigator 只获得各自白名单内的只读诊断工具；不得获得任何恢复工具、恢复提案工具或策略绕过能力。
- Router、Investigator、RAG、Aggregator、Report 均不得读取 Scenario ID、Run ID、Ground Truth、Oracle、评分规则或 fixture 常量。
- 并行分支不得写 `diagnostic_facts`、`hypothesis_assessments`、`observation_decisions`；Aggregator 是 fan-in 后唯一共享状态写入者。
- PostgreSQL 唯一约束和现有 ExecutionCoordinator 是幂等真源；Redis 不作为正确性锁。
- 不降低 Snapshot/Live 评分阈值、required evidence、Validator 标准或恢复授权门禁。
- 默认不启用 Multi-Agent 生产路由；只有 A/B 达到能力和性能门槛后才能另行决策是否默认开启。
- 不运行全量 pytest；每个任务只运行列出的目标测试，最后执行相关测试集、Ruff 和 Pyright。
- 不提交 `config/project.json`、`config/user.project.json`、`var/`、Archive、API Key、CLS 凭据或原始敏感日志。
- 实现与验证由主 Agent完成；计划审查只允许一个只读子 Agent，且仅一轮。

## Reuse Assessment

| 候选 | 许可/状态 | 决策 | 理由 |
| --- | --- | --- | --- |
| 项目现有 LangGraph `StateGraph`、动态 `Send`、fan-in、reducer、checkpointer | MIT；项目已锁定 `langgraph>=1.2.8` | Direct adoption | 与当前图、状态和 PostgreSQL checkpoint 原生兼容，无新增依赖 |
| 项目现有 `ExecutionCoordinator`、稳定 ID、Fact Adapter、Sufficiency、Tool Audit、Artifact | 项目自有 | Direct adoption | 已验证网络续跑、幂等副作用和 Evidence 所有权 |
| Microsoft AutoGen team/state pattern | MIT；仓库已进入 maintenance mode | Reference only | 会引入第二套运行时、状态与恢复语义，兼容成本高 |
| OpenAI Agents SDK orchestrator / agent-as-tool | MIT；活跃维护 | Reference only | handoff 更偏控制权转移，一对多并行仍需外部编排，且当前使用 Qwen + LangGraph |
| Strategy Router、capability registry、`EvidencePacket`、Aggregator | 项目尚无等价实现 | Custom project-owned | 规则与本项目证据安全、工具合同、审计和评分强绑定 |

结论：不复制候选代码、不新增依赖；采用现有 LangGraph 能力的 wrapped adoption，并实现项目拥有的受控路由与证据合同。

## 文件结构

- `apps/backend/src/super_ai/aiops/investigation.py`：Strategy 类型、版本化 Router policy、capability registry、Dispatch、`EvidencePacket`、纯函数路由和安全校验。
- `apps/backend/src/super_ai/aiops/investigation_runtime.py`：single/multi 共用的单工具执行原语、runtime/log Dispatch、并发预算、`ExecutionCoordinator` 复用和 Packet 生成。
- `apps/backend/src/super_ai/aiops/evidence_aggregation.py`：Packet 验证、稳定排序、claim 去重、时空冲突检测和 Fact/Observation 投影。
- `apps/backend/src/super_ai/aiops/diagnostics.py`：Knowledge Investigator、Strategy Router、LangGraph fan-out/fan-in、single fallback、动态复评和审计接线。
- `apps/backend/src/super_ai/evaluation/artifacts.py`：路由、Dispatch、Packet、降级和并发观测投影。
- `apps/backend/src/super_ai/evaluation/history.py`：terminal envelope 的安全 A/B metrics/metadata allowlist。
- `apps/backend/src/super_ai/evaluation/recording.py`：把可重建的诊断策略指标写入持久化终态结果。
- `apps/backend/src/super_ai/evaluation/live/cli.py`：内部 `--strategy auto|single|multi` 参数。
- `apps/backend/src/super_ai/evaluation/live/diagnostics.py`：只把 Benchmark strategy 注入诊断 service；普通 API 不开放该入口。
- `apps/backend/src/super_ai/evaluation/summary.py`：同场景 A/B 的能力、Evidence、耗时和模型调用差分。
- `apps/backend/tests/test_aiops_investigation_router.py`：Router、registry、预算和答案隔离。
- `apps/backend/tests/test_aiops_evidence_packets.py`：Packet schema、安全、去重和冲突。
- `apps/backend/tests/test_aiops_multi_agent_runtime.py`：并发、超时、部分失败、幂等与续跑。
- `apps/backend/tests/test_aiops_v4_workflow.py`：图接线、single 兼容、dynamic escalation 和 fallback。
- `apps/backend/tests/test_aiops_sse_delivery.py`：稳定事件顺序与重放。
- `apps/backend/tests/test_evaluation_artifacts.py`：审计投影及历史兼容。
- `apps/backend/tests/test_evaluation_history.py`：A/B 安全指标 round-trip、checksum 和敏感字段拒绝。
- `apps/backend/tests/test_evaluation_persistence.py`：进程重启后从 terminal envelope/diagnostic task 重建 A/B 输入。
- `apps/backend/tests/test_live_cli_contract.py`：Benchmark CLI 强制策略边界。
- `apps/backend/tests/test_live_diagnostic_adapter.py`：Live adapter strategy 注入和普通路径隔离。
- `openspec/changes/add-single-multi-agent-source-routing/`：本功能的 proposal、design、tasks 和 delta specs。
- `docs/aiops/agentpy-domainbench.md`：A/B 验收记录；只有真实执行后才填写结果。

---

### Task 1: 固化 OpenSpec 合同

**Files:**
- Create: `openspec/changes/add-single-multi-agent-source-routing/.openspec.yaml`
- Create: `openspec/changes/add-single-multi-agent-source-routing/proposal.md`
- Create: `openspec/changes/add-single-multi-agent-source-routing/design.md`
- Create: `openspec/changes/add-single-multi-agent-source-routing/tasks.md`
- Create: `openspec/changes/add-single-multi-agent-source-routing/specs/aiops-diagnosis-tasks/spec.md`
- Create: `openspec/changes/add-single-multi-agent-source-routing/specs/agentpy-sre-benchmark/spec.md`

**Contract:** 新增受控 Strategy Router、数据源 Investigator、EvidencePacket/Aggregator、Benchmark-only strategy override、幂等/失败降级 SHALL/MUST；明确普通 API 不接受强制策略。

- [x] **Step 1: 记录 OpenSpec RED**

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-single-multi-agent-source-routing --strict
```

Expected: change 不存在，命令非 0；保存这一失败作为 RED 证据。

- [x] **Step 2: 创建 proposal、design 和 delta specs**

`aiops-diagnosis-tasks/spec.md` 必须包含以下 requirements 及场景：

```markdown
### Requirement: Investigation strategy is deterministic and auditable
Workflow SHALL select deterministic_fast_path, single_agent, or multi_agent from
current-task public state, versioned policy, capability availability, time and model
budget. It MUST NOT read benchmark identity or evaluator-private data.

### Requirement: Parallel investigators have isolated capabilities
Runtime and Log investigators SHALL receive only source-scoped read-only tools and
MUST return schema-valid EvidencePacket values without mutating shared hypothesis state.

### Requirement: Evidence aggregation is deterministic and single-writer
Workflow SHALL validate ownership, audit completion, evidence quality, temporal scope,
deduplication and stable ordering before one Aggregator writes shared diagnostic state.

### Requirement: Multi-agent dispatch is resumable and fail-closed
Completed dispatches SHALL be reused by stable dispatch key. Timeout, partial failure,
late result and all-failed paths MUST NOT be interpreted as negative evidence.

### Requirement: Graph topology versions isolate resumable state
The new investigation topology SHALL use aiops-diagnostic-v3. A v2 checkpoint MUST
only resume with the legacy topology and MUST NOT be injected into v3 channels.

### Requirement: Multi-agent tools require explicit read-only trust
Discovery alone MUST NOT classify an MCP tool as safe. Only a code-owned, read-only
capability descriptor MAY expose a tool to an Investigator; unknown tools fail closed.
```

`agentpy-sre-benchmark/spec.md` 必须规定：CLI 可以强制 `auto|single|multi`；普通 API 不可强制；A/B 固定场景、知识库、模型、CLS 窗口、工具白名单和评分器；terminal envelope 持久化定长安全聚合指标且不得保存 Oracle 标签/required Evidence；进程重启后必须可重建 A/B 输入；未达门槛不得默认启用。

- [x] **Step 3: 创建可勾选 tasks 并同步已确认设计**

`design.md` 引用 `docs/superpowers/specs/2026-08-20-single-multi-agent-source-routing-design.md`，逐项保留阈值、硬门禁、并发2、Change unavailable、最多两轮、最多一个可选 Investigator LLM、无新表、无 GitHub 集成。

- [x] **Step 4: 运行 OpenSpec GREEN**

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-single-multi-agent-source-routing --strict
```

Expected: `add-single-multi-agent-source-routing` valid，退出码 0。

- [x] **Step 5: 提交规范**

```powershell
git add openspec/changes/add-single-multi-agent-source-routing
git commit -m "spec: define investigation strategy routing"
```

---

### Task 2: 建立 capability registry 与 sourceDomain

**Files:**
- Create: `apps/backend/src/super_ai/aiops/investigation.py`
- Create: `apps/backend/tests/test_aiops_investigation_router.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`

**Interfaces:**

```python
InvestigatorType = Literal["knowledge", "runtime", "log", "change"]
InvestigationStrategy = Literal["deterministic_fast_path", "single_agent", "multi_agent"]
StrategyMode = Literal["auto", "single", "multi"]

@dataclass(frozen=True, slots=True)
class InvestigatorCapability:
    investigator_type: InvestigatorType
    available: bool
    allowed_tools: frozenset[str]
    reason_code: str | None = None

@dataclass(frozen=True, slots=True)
class TrustedToolCapability:
    tool_name: str
    source_domain: Literal["runtime", "log"]
    read_only: Literal[True]
    maximum_calls_per_dispatch: int

def build_investigator_capabilities(
    *,
    discovered_tools: Sequence[McpToolDefinition],
    trusted_tool_capabilities: Mapping[str, TrustedToolCapability],
    tool_policies: Mapping[str, str],
    retrieval_available: bool,
    cls_available: bool,
) -> Mapping[InvestigatorType, InvestigatorCapability]: ...

def source_domain_for_tool(
    tool_name: str, capabilities: Mapping[InvestigatorType, InvestigatorCapability]
) -> InvestigatorType | None: ...

def normalize_plan_source_domains(
    plan: Sequence[Mapping[str, object]],
    capabilities: Mapping[InvestigatorType, InvestigatorCapability],
) -> list[dict[str, object]]: ...
```

- [x] **Step 1: 写 registry/sourceDomain RED 测试**

覆盖：`knowledge_retrieval→knowledge`；只有代码拥有的 `TrustedToolCapability` 才能把 `SearchLog→log` 或已枚举诊断工具→runtime；仅 discover 到但没有可信 descriptor 的用户 MCP 工具返回 `None`；descriptor 的 `read_only` 必须为 `True`；恢复工具永不进入任何 capability；Change 固定 unavailable/reason code；模型提供的非法 `sourceDomain` 被 registry 覆盖。

- [x] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_aiops_investigation_router.py -q -p no:cacheprovider
```

Expected: `super_ai.aiops.investigation` 不存在或接口缺失导致 FAIL。

- [x] **Step 3: 最小实现 registry 与 normalize**

工具归属必须由显式 `trusted_tool_capabilities` 决定，不能从描述、schema、server 名或“非恢复工具”反推只读属性。`build_investigator_capabilities()` 显式接收 discovered definitions 与 `tool_policies`；不在 trusted map、未 discover、read_only 非真，或 policy 为 `proposal_only`、`external_policy_required`、`execute` 的工具一律 fail closed。`normalize_plan_source_domains()` 复制 step，不修改调用者输入；未知映射保留给 single path，并写 `sourceDomainStatus="unmapped"`，但永不进入 Multi dispatch。

- [x] **Step 4: 在 Planner 输出后调用 normalize**

只修改 plan 的公开结构，不改变现有工具参数、causal intent 或 evidence rules。Knowledge step 的结果在 Task 5 抽取前仍保持现有行为。

- [x] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_investigation_router.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider
```

Expected: 目标测试全部 PASS。

- [x] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/investigation.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_investigation_router.py
git commit -m "feat: register investigation capabilities"
```

---

### Task 3: 实现纯函数 Strategy Router

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/investigation.py`
- Modify: `apps/backend/tests/test_aiops_investigation_router.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class InvestigationRouterPolicy:
    version: str = "investigation-router-v1"
    escalation_watch_threshold: int = 4
    multi_agent_threshold: int = 6
    single_agent_max_initial_steps: int = 2
    maximum_investigation_waves: int = 2
    aggregation_reserve_ms: int = 5_000

@dataclass(frozen=True, slots=True)
class InvestigationRoutingInput:
    required_domains: frozenset[InvestigatorType]
    unresolved_hypothesis_count: int
    causal_component_count: int
    missing_causal_roles: frozenset[str]
    high_quality_conflict: bool
    severity: str
    trusted_pattern_matched: bool
    decision_ready: bool
    valid_tool_calls_without_gain: int
    knowledge_hit: bool
    remaining_time_ms: int
    remaining_model_calls: int
    completed_dispatch_keys: frozenset[str]
    evidence_snapshot_hash: str
    wave: int

@dataclass(frozen=True, slots=True)
class InvestigationRoute:
    strategy: InvestigationStrategy
    score: int
    escalation_watch: bool
    selected_investigators: tuple[InvestigatorType, ...]
    rejected_investigators: Mapping[InvestigatorType, str]
    reason_codes: tuple[str, ...]
    policy_version: str

def route_investigation(
    routing_input: InvestigationRoutingInput,
    *, capabilities: Mapping[InvestigatorType, InvestigatorCapability],
    policy: InvestigationRouterPolicy,
    mode: StrategyMode = "auto",
) -> InvestigationRoute: ...
```

- [x] **Step 1: 写 Router RED**

覆盖 0..3 single、4..5 watch、>=6 multi；required domain 2类 +1、3类 +3（互斥）；跨组件 +2、3个 unresolved +1、冲突 +3、P0/P1/critical +2、缺2类 causal role +2、两工具无增益 +3、无知识 +1；recent change 始终 0。

再覆盖硬门禁优先：trusted pattern、decision ready、少于两个未完成可并行 domain、deadline 不足、模型预算不足、wave>=2、能力不可用、相同 dispatch snapshot 已完成、multi disabled。`mode="multi"` 也不能绕过安全/预算硬门禁，只能绕过分数阈值。

- [x] **Step 2: 写答案隔离 RED**

使用递归 shaped input 包含 `scenarioId`、`runId`、`ground_truth`、`oracle`、`primary_cause`、`scoreRules`，断言构造 `InvestigationRoutingInput` 时拒绝未知 evaluator-private 字段；对相同允许字段生成完全一致 route/reason code。

- [x] **Step 3: 运行 RED**

```powershell
uv run pytest tests/test_aiops_investigation_router.py -q -p no:cacheprovider
```

Expected: route 类型和函数缺失导致 FAIL。

- [x] **Step 4: 最小实现打分、硬门禁和稳定 reason 顺序**

Router 不调用 LLM、不读取全局文件或环境变量。reason code 按固定枚举顺序输出；selected investigators 固定 `runtime, log` 顺序；Knowledge 已完成不进入 fan-out；Change 总是 rejected。

- [x] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_investigation_router.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [x] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/investigation.py apps/backend/tests/test_aiops_investigation_router.py
git commit -m "feat: route investigation strategies"
```

---

### Task 4: 定义 EvidencePacket 与确定性 Aggregator

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/investigation.py`
- Create: `apps/backend/src/super_ai/aiops/evidence_aggregation.py`
- Create: `apps/backend/tests/test_aiops_evidence_packets.py`

**Interfaces:**

```python
PacketStatus = Literal["completed", "inconclusive", "failed", "timeout"]
EvidenceQuality = Literal["direct", "context", "reference"]
TimeScope = Literal["incident_window", "current", "historical"]

@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    claim_id: str
    value: JsonValue
    quality: EvidenceQuality
    causal_role: str | None
    supports: tuple[str, ...]
    refutes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    target_component: str
    observed_at: datetime | None
    time_scope: TimeScope

@dataclass(frozen=True, slots=True)
class EvidencePacket:
    task_id: str
    owner_user_id: str
    dispatch_id: str
    investigator_type: InvestigatorType
    status: PacketStatus
    claims: tuple[EvidenceClaim, ...]
    limitations: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    model_calls_used: int

@dataclass(frozen=True, slots=True)
class AggregationResult:
    accepted_packets: tuple[EvidencePacket, ...]
    rejected_dispatches: Mapping[str, str]
    claims: tuple[EvidenceClaim, ...]
    conflicts: tuple[dict[str, object], ...]

def aggregate_evidence_packets(
    packets: Sequence[EvidencePacket], *, context: AggregationContext
) -> AggregationResult: ...
```

- [x] **Step 1: 写 schema、安全和去重 RED**

覆盖跨 owner/task、未知 Evidence ID、未完成 audit、非白名单工具、reference 伪装 direct、缺少 time scope、恢复动作字段、secret/private reasoning/prompt/model raw response 字段、非法 causal role。失败或 timeout 空 claims 合法，但不能生成 refutes。

- [x] **Step 2: 写顺序无关与冲突 RED**

不同协程到达顺序必须输出相同 `(investigator_type, dispatch_id, claim fingerprint)` 排序；同 Evidence 多次引用只计一次；claim/value/source/component/timeScope/evidence hash 相同去重；同组件和同时间范围的相反值形成 conflict；incident abnormal/current healthy 不形成 conflict。

- [x] **Step 3: 运行 RED**

```powershell
uv run pytest tests/test_aiops_evidence_packets.py -q -p no:cacheprovider
```

Expected: packet/Aggregator 模块接口缺失导致 FAIL。

- [x] **Step 4: 最小实现 dataclass 边界和 Aggregator**

Aggregator 只做结构校验、所有权验证、稳定排序、去重和冲突标记，不调用 LLM、不自行改变 hypothesis disposition。claim fingerprint 精确使用：`claim_id + canonical_value + investigator_type + target_component + time_scope + sorted evidence_ids hash`。

- [x] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_evidence_packets.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [x] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/investigation.py apps/backend/src/super_ai/aiops/evidence_aggregation.py apps/backend/tests/test_aiops_evidence_packets.py
git commit -m "feat: aggregate investigator evidence packets"
```

---

### Task 5: 抽取 Knowledge Investigator，确保 RAG 只运行一次

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify: `apps/backend/tests/test_aiops_checkpointing.py`
- Modify: `apps/backend/tests/test_aiops_network_resume.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**

```python
async def AiopsDiagnosticService._knowledge_investigator(
    self, state: AiopsDiagnosticState
) -> dict[str, object]: ...
```

State 新增：`knowledge_context`、`knowledge_evidence_ids`、`knowledge_completed`、`investigation_strategy_mode`。Planner 只消费 `knowledge_context`，不再直接调用 retrieval tool。

- [ ] **Step 1: 写图版本隔离与 RAG 单次调用 RED**

断言新拓扑固定使用 `graph_version="aiops-diagnostic-v3"` 与 `aiops:{task_id}:aiops-diagnostic-v3`；同一 task 的 v2 未完成 checkpoint 不会被 v3 repository/checkpointer 查询或恢复；旧任务明确继续由旧 v2 graph/version 处理，不能把旧 channel state 注入新节点。Artifact 对新执行投影 v3，对历史 v4 Artifact 仍保留已持久化/推导的 v2。

构造计数 RetrievalTool：新图运行后调用恰好1次；Planner 重试或 v3 checkpoint 恢复不重复调用；Citation/reference event 与现有内容一致；空召回仍允许 Planner；Knowledge claim 质量只能是 reference，不能直接支持根因。

- [ ] **Step 2: 运行 RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_checkpointing.py tests/test_aiops_network_resume.py tests/test_evaluation_artifacts.py tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider -k "knowledge or retrieval or graph_version or checkpoint or legacy"
```

Expected: `_knowledge_investigator`/v3 graph version 缺失、v2 checkpoint 被复用，或 retrieval 仍在 Planner 导致 FAIL。

- [ ] **Step 3: 抽取现有 Planner 的 retrieval 代码**

保留现有 owner/knowledge-base 隔离、Milvus/BM25L/RRF/rerank、Redis cache、Citation、Tool Audit、Evidence 持久化和安全过滤。使用稳定 execution identity：`node_name="knowledge_investigator"`，`logical_iteration=0`，input fingerprint 只包含公开 query 和允许 knowledge base IDs。

- [ ] **Step 4: 调整 v4 图起点并隔离 checkpoint namespace**

`START -> knowledge_investigator -> planner`；旧 workflow 图不改。为新拓扑定义常量 `AIOPS_GRAPH_VERSION = "aiops-diagnostic-v3"`，Execution repository、Coordinator、checkpointer、thread ID、模型/工具 execution identity 和 Artifact 全部使用该常量。恢复时按任务 input 中已持久化的 graph/workflow version 选择旧 v2 或新 v3；缺失版本的历史任务保持 v2，绝不自动迁移未完成 checkpoint。Planner 只读取同 v3 checkpoint 恢复的 knowledge state；不得在知识节点添加默认 LLM 调用。

- [ ] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_checkpointing.py tests/test_aiops_network_resume.py tests/test_evaluation_artifacts.py tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider -k "knowledge or retrieval or graph or checkpoint or resume or legacy"
```

Expected: 目标测试 PASS，RAG 调用计数为1。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_aiops_checkpointing.py apps/backend/tests/test_aiops_network_resume.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "refactor: isolate knowledge investigation"
```

---

### Task 6: 实现 runtime/log Dispatch 与执行幂等

**Files:**
- Create: `apps/backend/src/super_ai/aiops/investigation_runtime.py`
- Create: `apps/backend/tests/test_aiops_multi_agent_runtime.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class InvestigationDispatch:
    task_id: str
    owner_user_id: str
    dispatch_id: str
    dispatch_key: str
    investigator_type: Literal["runtime", "log"]
    objective: str
    tests_hypotheses: tuple[str, ...]
    missing_causal_roles: tuple[str, ...]
    steps: tuple[dict[str, object], ...]
    allowed_tools: frozenset[str]
    existing_evidence_ids: tuple[str, ...]
    deadline_ms: int
    model_call_budget: int

def build_investigation_dispatches(...) -> tuple[InvestigationDispatch, ...]: ...

class InvestigatorExecutor:
    async def execute(self, dispatch: InvestigationDispatch) -> EvidencePacket: ...

@dataclass(frozen=True, slots=True)
class DiagnosticToolExecutionRequest:
    owner_user_id: str
    task_id: str
    graph_version: str
    plan_step: Mapping[str, object]
    logical_iteration: int
    allowed_tools: frozenset[str]

@dataclass(frozen=True, slots=True)
class DiagnosticToolExecutionResult:
    status: Literal["completed", "failed"]
    evidence_id: str
    tool_call_id: str
    safe_output: object
    safe_summary: str
    events: tuple[dict[str, object], ...]

async def execute_diagnostic_tool(
    request: DiagnosticToolExecutionRequest, *, runtime: DiagnosticToolRuntime
) -> DiagnosticToolExecutionResult: ...
```

- [ ] **Step 1: 写共享工具原语、Dispatch 与幂等 RED**

先用相同 step 分别经过 single `_executor()` 和 `InvestigatorExecutor`，断言两者调用同一个 `execute_diagnostic_tool()`，产生相同 canonical arguments fingerprint、stable tool/evidence ID、Tool Audit、safe output、checkpoint execution identity 和异常分类，禁止复制两套执行逻辑。

再覆盖稳定 dispatch key：`task_id + policy_version + investigator_type + objective_hash + evidence_snapshot_hash`；相同 key 完成后直接复用 Packet；失败/timeout 重试 attempt 增加但逻辑 key 不变；工具继续复用共享原语的 canonical arguments fingerprint；不同 owner/task 不复用。

- [ ] **Step 2: 写权限和执行 RED**

Runtime 不能调用 SearchLog/knowledge/recovery；Log 只能调用启用的 SearchLog/日志工具；Planner step 即使伪造 allowedTools 也被 registry 拒绝。每个 Dispatch 最多 runtime 3步、log 2 query、可选 LLM 1次；第一版默认模型调用0。

- [ ] **Step 3: 运行 RED**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py -q -p no:cacheprovider
```

Expected: runtime 模块和 Dispatch 接口缺失导致 FAIL。

- [ ] **Step 4: 先抽取共享单工具执行原语，再实现 Dispatch 执行器**

从现有 `AiopsDiagnosticService._executor()` 抽取 `execute_diagnostic_tool()`，由 `DiagnosticToolRuntime` 显式携带 repositories、MCP client、retrieval tool、ExecutionCoordinator 和安全回调。现有 `_executor()` 改为薄状态适配器；InvestigatorExecutor 调用同一原语，按 dispatch 内依赖顺序执行。Packet 只引用已持久化 Evidence 和 completed audit。异常映射为 allowlisted safe error code，禁止保存原始异常/日志。

- [ ] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_execution_coordinator.py tests/test_aiops_network_resume.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/investigation_runtime.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_multi_agent_runtime.py
git commit -m "feat: execute source scoped investigators"
```

---

### Task 7: 接入 LangGraph fan-out/fan-in 与 Aggregator 单写

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/aiops/evidence_aggregation.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify: `apps/backend/tests/test_aiops_multi_agent_runtime.py`

**Graph interfaces:**

```python
def AiopsDiagnosticService._route_after_strategy(
    self, state: AiopsDiagnosticState
) -> str | list[Send]: ...

async def AiopsDiagnosticService._strategy_router(
    self, state: AiopsDiagnosticState
) -> dict[str, object]: ...

async def AiopsDiagnosticService._investigator_dispatch(
    self, state: AiopsDiagnosticState
) -> dict[str, object]: ...

async def AiopsDiagnosticService._evidence_aggregator(
    self, state: AiopsDiagnosticState
) -> dict[str, object]: ...
```

State 新增 append-only `investigation_packets` reducer；共享 fact/hypothesis/observation 字段不设并行 reducer。

- [ ] **Step 1: 写图结构 RED**

断言 v3 graph topology（workflow 仍为 `evidence-driven-v4`）包含 `knowledge_investigator`、`strategy_router`、`investigator_dispatch`、`evidence_aggregator`；single 路由进入现有 executor；multi 通过两个 `Send` 启动 runtime/log；两个 Packet 都完成后只运行一次 Aggregator，再进入 Fact Adapter/Sufficiency。

单独覆盖 `deterministic_fast_path`：当动态复评时 `trusted_pattern_matched` 或 `decision_ready` 已由当前任务公开状态证明，route update 持久化 `strategy="deterministic_fast_path"`，图不得再进入 executor 或 investigator dispatch，而是直接进入 `sufficiency_gate`；随后必须正常到达 decision/validator/recovery/policy/report，不能绕过任何验证或恢复权限节点。初始 Planner 后尚无可决事实时不得伪造 fast path。

- [ ] **Step 2: 写单写和顺序无关 RED**

人为让 Log 先完成/Runtime 先完成，两次最终 facts、assessments、observations、evidence IDs、decision 必须相同；branch update 若包含共享字段必须被测试拒绝。

- [ ] **Step 3: 运行 RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_multi_agent_runtime.py -q -p no:cacheprovider -k "strategy or multi_agent or fan or aggregator"
```

Expected: 图节点/Send/fast-path 接线缺失导致 FAIL。

- [ ] **Step 4: 最小实现图接线**

从 `langgraph.types import Send` 直接复用动态 fan-out。Strategy Router 持久化 route step；single 返回 `executor`，multi 返回稳定排序的 Send，deterministic fast path 返回 `sufficiency_gate`。每个 branch 只追加 Packet/event；Aggregator 验证 Packet 后复用现有 `extract_public_facts()`、trusted pattern 和 hypothesis reducer，一次性更新共享状态。所有三条路径最终汇入原有 Sufficiency/Decision/Validator/Recovery/Policy/Report 安全链。

- [ ] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_evidence_packets.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/aiops/evidence_aggregation.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_aiops_multi_agent_runtime.py
git commit -m "feat: fan out investigation sources"
```

---

### Task 8: 增加动态升级、deadline、部分失败和 Single fallback

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/aiops/investigation.py`
- Modify: `apps/backend/src/super_ai/aiops/investigation_runtime.py`
- Modify: `apps/backend/src/super_ai/aiops/model_budget.py`
- Modify: `apps/backend/tests/test_aiops_multi_agent_runtime.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify: `apps/backend/tests/test_aiops_model_budget.py`

- [ ] **Step 1: 写动态路由 RED**

覆盖初始 single；两个有效工具后 hypothesis/causal coverage 无增益且存在 runtime/log 未执行步骤时升级 multi；已有 trusted pattern、decisionReady、没有新搜索空间、soft deadline 临近或第二 wave 后不再升级。

- [ ] **Step 2: 写失败/取消 RED**

一个 Packet timeout、另一个 completed：保留完成 Evidence，timeout 不作为反证；两个都失败且预算足够：`fallback_to_single_agent` 并从剩余 plan 继续；预算不足：manual review；decisionReady 后未启动 dispatch 取消，迟到 Packet 记录 `late_result_ignored` 且不修改 decision/report/artifact score。

- [ ] **Step 3: 写并发预算 RED**

使用 async barriers 证明 collector 最多4、LLM Investigator 最多2；首版 runtime/log 两个工具可重叠；单 dispatch 内存在数据依赖的步骤保持串行；Multi 启动条件满足 `remaining_time >= max(deadline)+aggregation_reserve` 和 `remaining_model_calls >= optional+mandatory`。扩展 `ModelRole` 为 `Literal[..., "investigator"]`，固定 `ROLE_TIMEOUT_SECONDS["investigator"] = 45`，并断言所有 Investigator 共享现有 hard limit 8、每个 Dispatch 最多保留1次、总体额外调用最多2次；并发 semaphore 不能绕过原子 budget reserve。

- [ ] **Step 4: 运行 RED**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_v4_workflow.py -q -p no:cacheprovider -k "escalat or timeout or partial or fallback or concurrency or late"
```

Expected: 动态升级/降级行为缺失导致 FAIL。

- [ ] **Step 5: 最小实现 wave state、semaphore 和 fallback**

最多2 wave；Collector semaphore=4、LLM semaphore=2；在 `model_budget.py` 增加明确的 `investigator` role 和45秒 timeout，所有可选调用继续走同一个 `ModelCallBudget`，每 Investigator 最多1、每个任务额外最多2；失败原因使用 allowlist。`fallback_to_single_agent` 只能执行尚未完成且合同有效的 plan step。

- [ ] **Step 6: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_v4_workflow.py tests/test_aiops_model_budget.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/aiops/investigation.py apps/backend/src/super_ai/aiops/investigation_runtime.py apps/backend/src/super_ai/aiops/model_budget.py apps/backend/tests/test_aiops_multi_agent_runtime.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_aiops_model_budget.py
git commit -m "feat: degrade multi agent investigations safely"
```

---

### Task 9: 稳定 SSE 顺序并投影 Artifact/审计

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Modify: `apps/backend/src/super_ai/evaluation/recording.py`
- Modify: `apps/backend/tests/test_aiops_sse_delivery.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_history.py`
- Modify: `apps/backend/tests/test_evaluation_persistence.py`

**Artifact additions:**

```python
@dataclass(frozen=True, slots=True)
class InvestigationAudit:
    strategy: str
    score: int
    reason_codes: tuple[str, ...]
    policy_version: str
    selected_investigators: tuple[str, ...]
    dispatch_count: int
    packet_statuses: tuple[str, ...]
    fallback_reason: str | None

@dataclass(frozen=True, slots=True)
class InvestigationBenchmarkMetrics:
    strategy: str
    policy_version: str
    root_cause_top1_correct: bool
    evidence_recall_basis_points: int
    duration_ms: int
    model_call_count: int
    duplicate_evidence_basis_points: int
    fallback_reason: str | None
    security_hard_gate_passed: bool
```

- [ ] **Step 1: 写 SSE RED**

并行完成顺序不同，持久化 task events 的 sequence 唯一且递增；实时 dispatch progress 可携带 dispatch ID，但 fan-in、decision、report、complete 稳定有序；PostgreSQL 重放不重复、不按协程完成时间重排终态。

- [ ] **Step 2: 写 Artifact RED**

Artifact 从持久化 steps 提取 Router、Dispatch、Packet、fallback 和模型调用，不读取 report prose；不保存 objective 原文、tool raw output、prompt、exception、secret；旧 v2/v3/v4 没有 routing steps 时投影为 `None`，历史 checksum/评分语义不变。

再写 terminal persistence RED：Live evaluator 在私有 Oracle 边界只把聚合后的 `rootCauseTop1Correct`、`evidenceRecallBasisPoints` 和 security hard gate 写入 safe metrics；Artifact 提供 strategy、policy、duration、model call、duplicate Evidence 和 fallback。`terminal_envelope()` allowlist 接受这些定长标量，拒绝 expected cause、required evidence IDs、Oracle、Prompt、原始日志和任意嵌套扩展。保存后销毁运行期 `RunArtifact`，从 PostgreSQL terminal envelope 重新读取仍能完整构造 `InvestigationBenchmarkMetrics`，checksum round-trip 稳定。

- [ ] **Step 3: 运行 RED**

```powershell
uv run pytest tests/test_aiops_sse_delivery.py tests/test_evaluation_artifacts.py tests/test_evaluation_history.py tests/test_evaluation_persistence.py -q -p no:cacheprovider -k "investigation or dispatch or replay or sequence or legacy or benchmark_metrics"
```

Expected: 新审计投影、稳定序列或进程重启后的 metrics 重建断言 FAIL。

- [ ] **Step 4: 最小实现稳定 sequence 与安全投影**

Coordinator 在 dispatch 创建时分配稳定序号；Aggregator 按 dispatch/type/evidence 排序生成公共事件；Artifact 只保留 allowlisted route metadata 和 safe status。Evaluation Recorder 在 evaluator 边界构造定长 `InvestigationBenchmarkMetrics` 并写入 terminal envelope metrics；历史读取只使用 persisted envelope 和 `diagnostic_task_id`，不得依赖内存中的 RunArtifact。

- [ ] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_sse_delivery.py tests/test_evaluation_artifacts.py tests/test_evaluation_history.py tests/test_evaluation_persistence.py tests/test_evaluation_archive.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/src/super_ai/evaluation/history.py apps/backend/src/super_ai/evaluation/recording.py apps/backend/tests/test_aiops_sse_delivery.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_evaluation_history.py apps/backend/tests/test_evaluation_persistence.py
git commit -m "feat: audit investigation routing"
```

---

### Task 10: 增加 Benchmark-only strategy CLI

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Create: `apps/backend/tests/test_live_cli_contract.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**

```text
python -m super_ai.evaluation.live.cli run ... --strategy auto|single|multi
```

`ApplicationLiveDiagnosticAdapter.__init__(..., investigation_strategy: StrategyMode = "auto")`；普通 FastAPI request schema 和 `AiopsDiagnosticService.start()` 不新增客户端字段。

- [ ] **Step 1: 写 CLI/API 隔离 RED**

断言 run parser 接受三值，缺省 auto，非法值退出2；verify/cleanup/report 不接受 strategy；adapter 将值传给内部 service state；FastAPI 诊断 request 即使包含 `strategy="multi"` 也不能覆盖服务策略（按当前 unknown-field 行为拒绝或忽略，但必须测试固定一种行为）。

- [ ] **Step 2: 运行 RED**

```powershell
uv run pytest tests/test_live_cli_contract.py tests/test_live_diagnostic_adapter.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider -k "strategy"
```

Expected: CLI 参数/adapter 接口缺失导致 FAIL。

- [ ] **Step 3: 最小实现 CLI 注入**

只在 `_run_live_command()` 创建 adapter 时传值；safe output 和持久化 evaluation envelope 记录 strategy，但不记录私有配置。普通 API 固定 auto，并受服务端 `multi_agent_enabled` 开关约束。

- [ ] **Step 4: 运行 GREEN**

```powershell
uv run pytest tests/test_live_cli_contract.py tests/test_live_diagnostic_adapter.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider -k "strategy"
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/tests/test_live_cli_contract.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "feat: expose benchmark investigation strategy"
```

---

### Task 11: 补齐并发恢复、答案隔离与安全回归

**Files:**
- Modify: `apps/backend/tests/test_aiops_multi_agent_runtime.py`
- Modify: `apps/backend/tests/test_aiops_network_resume.py`
- Modify: `apps/backend/tests/test_aiops_execution_coordinator.py`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_live_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_knowledge_candidate_safety.py`
- Modify: `apps/backend/tests/test_snapshot_evaluation_tools.py`

- [ ] **Step 1: 写完整失败路径 RED**

增加：相同 dispatch key 并发创建、重复 Packet/Aggregator 保存、PostgreSQL 唯一冲突后安全读取、Worker 在一个 branch 完成后重启、两个 branch 完成但 fan-in 未提交时重启、CLI 同 run-id 重试。断言工具调用/Evidence/Packet/共享 observations 不重复。

- [ ] **Step 2: 写答案隔离 RED**

增加：`--scenario ../APY-003`、symlink/path traversal、Packet/claim 嵌套字段伪装 `oracle`/`primary_cause`、Router 输入伪装 `ground_truth.yaml`、Investigator 尝试 `ReadGroundTruth`、RAG 文档携带 evaluator-private shaped 字段。全部必须在进入 Prompt、Packet、checkpoint、Artifact 前拒绝或过滤。

- [ ] **Step 3: 运行 RED**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_network_resume.py tests/test_aiops_execution_coordinator.py tests/test_evaluation_scenarios.py tests/test_live_evaluation_scenarios.py tests/test_knowledge_candidate_safety.py tests/test_snapshot_evaluation_tools.py -q -p no:cacheprovider
```

Expected: 新恢复/隔离用例至少一个 FAIL，且失败原因对应缺失保护而不是 fixture 错误。

- [ ] **Step 4: 最小补强生产代码**

只修复测试暴露的幂等或过滤缺口；不新增 Redis lock；不放宽 Packet schema；状态不确定的任何可写动作继续 manual review。

- [ ] **Step 5: 运行 GREEN**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py tests/test_aiops_network_resume.py tests/test_aiops_execution_coordinator.py tests/test_evaluation_scenarios.py tests/test_live_evaluation_scenarios.py tests/test_knowledge_candidate_safety.py tests/test_snapshot_evaluation_tools.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add apps/backend/src/super_ai apps/backend/tests/test_aiops_multi_agent_runtime.py apps/backend/tests/test_aiops_network_resume.py apps/backend/tests/test_aiops_execution_coordinator.py apps/backend/tests/test_evaluation_scenarios.py apps/backend/tests/test_live_evaluation_scenarios.py apps/backend/tests/test_knowledge_candidate_safety.py apps/backend/tests/test_snapshot_evaluation_tools.py
git commit -m "test: harden multi agent investigation recovery"
```

---

### Task 12: 执行 Single/Multi A/B 与发布门禁验收

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/summary.py`
- Modify: `apps/backend/tests/test_evaluation_summary.py`
- Modify: `apps/backend/tests/test_evaluation_persistence.py`
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/add-single-multi-agent-source-routing/tasks.md`

**Acceptance metrics:**

- Single 兼容：同场景分数/根因/恢复授权不下降；P95 增幅 ≤5%。
- Multi 性能：P95 ≤ Single 的1.5倍；额外模型调用 ≤2；重复 Evidence ≤10%。
- Multi 能力：Evidence Recall 提升 ≥10个百分点，或 Root Cause Top-1 提升 ≥5个百分点。
- 安全：0 Ground Truth 泄漏、0 跨租户 Evidence、0 重复恢复、0 未授权工具。
- 未达到能力与性能门槛：功能保留 Benchmark/显式内部模式，生产 auto 仍降为 single。

- [ ] **Step 1: 写 summary gate RED**

为纯函数 `compare_investigation_strategies(single_runs, multi_runs)` 写测试，输入只能是从 PostgreSQL 重新读取的 terminal envelopes 所投影的 `InvestigationBenchmarkMetrics`：按 safe scenario/campaign key 配对；计算 score/root cause/evidence recall/duration/model calls/duplicate evidence；任一安全 hard gate 失败则 rejected；仅性能达标但能力无增益仍 rejected；满足能力和性能才 eligible。删除内存 RunArtifact 后重建结果必须相同。

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_evaluation_summary.py -q -p no:cacheprovider -k "investigation_strategy"
```

Expected: comparison 接口缺失导致 FAIL。

- [ ] **Step 3: 最小实现 A/B summary**

只读取 Task 9 已持久化的 terminal envelope metrics/metadata；若需要诊断详情，只能用 envelope 的 `diagnostic_task_id` 从 owner-scoped repositories 重建安全 Artifact，不能依赖运行期对象。不得读取 report prose 或 Ground Truth 原文件；结果保存 campaign/run IDs、知识库版本、模型名、graph/policy version、safe metrics 和 eligibility，不保存 expected cause、required Evidence ID、凭据或原始日志。

- [ ] **Step 4: 运行离线相关回归**

```powershell
uv run pytest tests/test_aiops_investigation_router.py tests/test_aiops_evidence_packets.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_v4_workflow.py tests/test_aiops_checkpointing.py tests/test_aiops_network_resume.py tests/test_aiops_sse_delivery.py tests/test_evaluation_artifacts.py tests/test_evaluation_history.py tests/test_evaluation_persistence.py tests/test_evaluation_summary.py tests/test_live_cli_contract.py tests/test_live_diagnostic_adapter.py tests/test_evaluation_scenarios.py tests/test_live_evaluation_scenarios.py tests/test_knowledge_candidate_safety.py tests/test_snapshot_evaluation_tools.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops src/super_ai/evaluation tests/test_aiops_investigation_router.py tests/test_aiops_evidence_packets.py tests/test_aiops_multi_agent_runtime.py tests/test_live_cli_contract.py
uv run pyright src/super_ai/aiops src/super_ai/evaluation tests/test_aiops_investigation_router.py tests/test_aiops_evidence_packets.py tests/test_aiops_multi_agent_runtime.py tests/test_live_cli_contract.py
```

Expected: pytest 全部 PASS；Ruff 0 error；Pyright 0 error。不运行全量 pytest。

- [ ] **Step 5: 运行固定场景 A/B（需真实服务就绪后逐场景执行）**

对至少2个同时包含 runtime/log 有效搜索空间、且 single 不命中 deterministic fast path 的 Live 场景，各运行3次 single 和3次 multi；每次先 verify 环境，失败立即 cleanup 并停止。命令模板：

```powershell
cd apps/backend
uv run python -m super_ai.evaluation.live.cli run --scenario <SAFE_SCENARIO_ID> --run-id <UNIQUE_RUN_ID> --owner-user-id <TEST_OWNER_ID> --knowledge-base-id <ACTIVE_30_CARD_KB_ID> --evidence-source cls --strategy single --config <LOCAL_CONFIG_PATH>
uv run python -m super_ai.evaluation.live.cli run --scenario <SAFE_SCENARIO_ID> --run-id <UNIQUE_RUN_ID> --owner-user-id <TEST_OWNER_ID> --knowledge-base-id <ACTIVE_30_CARD_KB_ID> --evidence-source cls --strategy multi --config <LOCAL_CONFIG_PATH>
```

Expected: 每个 run 都持久化 terminal envelope、Artifact checksum、route audit、模型调用和耗时；不能复用 run ID。真实 ID 和本地配置路径仅在执行时从安全环境取得，不写入计划或仓库。

- [ ] **Step 6: 生成差分并执行发布判定**

使用持久化结果生成 paired A/B summary。若门槛通过，在文档写 `eligible_for_default_review`，仍需用户做“是否生产默认启用”的重大决策；若不通过，写 `benchmark_only` 并列出具体差距，不能调整评分阈值掩盖失败。

- [ ] **Step 7: 更新 OpenSpec task 与验收文档**

只勾选有测试或真实 Run 证据的条目；记录 run IDs、checksum、分数、Evidence Recall、Root Cause Top-1、P50/P95、模型调用、fallback 和安全 hard gates。不得填写估算或虚构数据。

- [ ] **Step 8: 提交**

```powershell
git add apps/backend/src/super_ai/evaluation/summary.py apps/backend/tests/test_evaluation_summary.py apps/backend/tests/test_evaluation_persistence.py docs/aiops/agentpy-domainbench.md openspec/changes/add-single-multi-agent-source-routing/tasks.md
git commit -m "test: evaluate investigation strategy routing"
```

## 最终验收清单

- [ ] OpenSpec strict validation 通过。
- [ ] Router 纯函数、reason code 稳定、硬门禁优先、Change 分值固定0。
- [ ] Knowledge Retrieval 每次任务最多一次，checkpoint 续跑不重复。
- [ ] Runtime/Log 并行且权限隔离；Multi 不拥有恢复工具。
- [ ] Aggregator 单写、结果顺序无关、Evidence 去重、时空冲突正确。
- [ ] 部分失败、全部失败、timeout、late result、Worker 重启全部 fail closed。
- [ ] Benchmark CLI 可强制策略，普通 API 不可强制。
- [ ] Artifact、SSE、checkpoint 和审计不含私有推理、凭据、原始敏感日志或 evaluator-private 内容。
- [ ] v2/v3/v4 历史 Artifact 与当前 single 路径保持兼容。
- [ ] 目标 pytest、Ruff、Pyright 通过；未运行全量 pytest。
- [ ] A/B 未达到门槛时生产 auto 不默认升级 Multi。

## 计划自查

**Spec coverage:** 已覆盖三态路由、全部评分特征与硬门禁、Knowledge/Runtime/Log/Change、EvidencePacket、Aggregator、并发/预算/两轮限制、Checkpoint/幂等、SSE/Artifact、安全隔离、Benchmark-only override、A/B 门槛和生产默认启用决策。

**Placeholder audit:** 生产代码接口、测试文件、命令、阈值和预期结果均已明确。Task 12 的 `<SAFE_SCENARIO_ID>`、`<UNIQUE_RUN_ID>`、`<TEST_OWNER_ID>`、`<ACTIVE_30_CARD_KB_ID>`、`<LOCAL_CONFIG_PATH>` 是故意不写入仓库的运行时安全值，不是实现 TBD；执行时必须从现有测试环境解析，不得硬编码或提交。

**Type consistency:** `InvestigatorType`、`InvestigationStrategy`、`StrategyMode`、Packet status/quality/time scope 使用封闭 Literal；Router/Packet/Aggregator 使用 frozen dataclass；公共 LangGraph state 使用 JSON-safe payload；内部 dataclass 在 node 边界显式序列化；旧 Artifact 缺失新审计时为 `None`，不改变既有评分类型。

**Single review amendments:** 唯一一次只读计划审查提出的五项均已吸收：以 `aiops-diagnostic-v3` 隔离旧 checkpoint；以显式 `TrustedToolCapability(read_only=True)` 取代未知工具推断；将定长 A/B 聚合指标写入 terminal envelope；抽取 single/multi 共用 `execute_diagnostic_tool()`；为 `deterministic_fast_path` 增加明确图目标、RED 和完整安全链验收。未启动第二轮 reviewer。
