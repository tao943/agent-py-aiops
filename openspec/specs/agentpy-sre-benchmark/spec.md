# agentpy-sre-benchmark Specification

## Purpose
TBD - created by archiving change add-agentpy-sre-benchmark. Update Purpose after archive.
## Requirements
### Requirement: Snapshot scenario answers are isolated

系统 SHALL 将 Agent 可见的公开场景与冻结工具观测同 evaluator-only 标准答案分离，且 Agent 运行时 MUST NOT 获得标准答案路径、内容或工具。

#### Scenario: Agent runs a Snapshot case

- **WHEN** runner 启动一个 Snapshot 场景
- **THEN** Agent MUST 只收到公开告警、候选假设和类型化 Snapshot 工具，标准答案 MUST 在 Agent 返回后由 evaluator 单独加载。

#### Scenario: Public scenario contains answer data

- **WHEN** 公开场景文件包含根因、必要证据或答案字段
- **THEN** loader MUST 拒绝该场景且 MUST NOT 启动 Agent。

### Requirement: Paired cases measure differential diagnosis

系统 SHALL 至少提供一对现象相同但 primary mechanism 不同的场景，并 SHALL 按 component、mechanism、trigger 和证据里程碑判定结果。

#### Scenario: Same 502 symptom has different causes

- **WHEN** APY-003 与 APY-006 被加载
- **THEN** 二者 MUST 具有相同 symptom family 和告警名，同时其 primary mechanism MUST 不同。

### Requirement: Snapshot observations are deterministic and typed

Snapshot 工具运行时 SHALL 只公开白名单工具和已登记的参数组合，并 SHALL 对未知工具、额外参数或未登记调用失败关闭。

#### Scenario: Agent requests a registered observation

- **WHEN** Agent 使用完全匹配的类型化参数调用 Snapshot 工具
- **THEN** 运行时 MUST 返回防御性复制的冻结观测并保存有序调用记录。

#### Scenario: Agent attempts oracle access

- **WHEN** Agent 调用未公开工具或尝试读取标准答案
- **THEN** 运行时 MUST 拒绝调用且评测 MUST 能将该安全事件纳入有效性判定。

### Requirement: Benchmark scoring is deterministic and explainable

Evaluator SHALL 根据结构化运行产物计算 outcome、diagnosis、evidence、process、safety 和 efficiency 六个维度，并 SHALL 为每次给分或扣分保存机器可读理由。

#### Scenario: Diagnosis is correct and grounded

- **WHEN** 根因字段与 oracle 一致、必要证据与排除证据均已满足且没有硬门槛
- **THEN** 结果 MUST 能通过且每个得分理由 MUST 引用其判定依据。

#### Scenario: Evidence is fabricated or answer is accessed

- **WHEN** 决策引用不存在的证据或工具审计显示尝试访问标准答案
- **THEN** evaluator MUST 应用硬门槛，并 MUST NOT 仅因报告文本包含正确关键词而判定通过。

### Requirement: Evaluation runs are reproducible records

系统 SHALL 在 PostgreSQL 保存场景、模式、suite 版本、Agent 版本、非敏感模型配置、诊断任务、状态、分数与理由。

#### Scenario: Completed run is inspected

- **WHEN** operator 按 run ID 读取已完成评测
- **THEN** Repository MUST 返回完整版本信息和 scorecard，且记录 MUST NOT 包含 API key、token 或标准答案正文。

### Requirement: Evaluation failures reach safe terminal states

系统 SHALL 将已创建但未完成的评测明确终止为 `agent_failed` 或 `infra_failed`，并 SHALL 只保存 allowlist failure category，不保存异常原文或敏感信息。

#### Scenario: Agent execution fails

- **WHEN** diagnostic adapter 抛出异常或返回 scenario/mode 不匹配的 artifact
- **THEN** run MUST 转为 `agent_failed`，category MUST 分别为 `adapter_error` 或 `artifact_invalid`，且 MUST NOT 保持 `pending`。

#### Scenario: Evaluation infrastructure fails

- **WHEN** oracle、scorer 或 scorecard persistence 失败
- **THEN** run MUST 尽力转为 `infra_failed`，CLI MUST 返回安全错误分类且 MUST NOT 输出异常原文。

#### Scenario: Failure transition is repeated

- **WHEN** 相同 run 以相同终态和 category 重复写入
- **THEN** Repository MUST 幂等返回已有记录；不同终态或 category MUST 被拒绝。

### Requirement: Evaluation persistence is concurrent and atomic

系统 SHALL 对相同 `run_id` 的并发创建提供确定性幂等语义，并 SHALL 在一个 PostgreSQL 事务中完成 run 与 scorecard。

#### Scenario: Identical run identity is created concurrently

- **WHEN** 两个调用以相同身份并发创建相同 `run_id`
- **THEN** 二者 MUST 返回同一记录且 MUST NOT 向调用方暴露原始唯一键异常。

#### Scenario: Scorecard finalization fails

- **WHEN** scorecard flush 或 commit 失败
- **THEN** run MUST NOT 变为 `completed`，且 MUST NOT 留下部分 scorecard。

### Requirement: PostgreSQL and Redis paired Snapshots require differential evidence

系统 SHALL 提供 PostgreSQL 与 Redis 各一对现象相同、primary mechanism 不同的 Snapshot 场景。同一故障族的公开标题、告警和候选假设 MUST 相同；每个 oracle MUST 要求至少两项独立证据里程碑和一个强替代原因排除项。

#### Scenario: PostgreSQL pair is loaded

- **WHEN** APY-002 与 APY-011 被加载
- **THEN** 二者 MUST 暴露相同公共输入，且 primary mechanism MUST 分别表示数据库工作占用连接和应用连接生命周期异常

#### Scenario: Redis pair is loaded

- **WHEN** APY-007 与 APY-012 被加载
- **THEN** 二者 MUST 暴露相同公共输入，且 primary mechanism MUST 分别表示服务端不可用和客户端连接池恢复异常

#### Scenario: New public scenario leaks evaluator values

- **WHEN** 新公开场景包含 oracle mechanism、trigger 或必要 evidence milestone ID
- **THEN** 场景合同测试 MUST 失败，且该输入 MUST NOT 被用于 Agent 运行

### Requirement: Blog-derived scenarios record synthetic provenance

新增场景 SHALL 标记为 `agentpy-original`，SHALL 记录精确参考 URL、访问日期、适用许可证与本项目合成说明，且 MUST NOT 在未转换具体 OpenSRE 场景时声称 OpenSRE-derived。

#### Scenario: Operator audits a new scenario source

- **WHEN** operator 检查 APY-002、APY-007、APY-011 或 APY-012 的 provenance
- **THEN** 文件 MUST 区分公开故障机制参考和 AgentPy 合成的告警、观测、干扰项与答案

### Requirement: Evidence-driven-v3 artifacts require audited validation origin

Evaluator SHALL 只从持久化 Workflow steps 构建 scoreable root-cause decision，并 SHALL NOT 使用报告
文本、Ground Truth 或 Validator 错误消息补全 Agent 结论。

#### Scenario: LLM confirms a v3 decision
- **WHEN** 最新 v3 Decision Validation 为 `status=valid` 且 `validationOrigin=llm_confirmed`
- **THEN** Artifact MUST 导出对应的持久化 root-cause decision

#### Scenario: Strict deterministic fallback validates a v3 decision
- **WHEN** 最新 v3 Decision Validation 为 `status=valid` 且
  `validationOrigin=deterministic_grounded_fallback`
- **THEN** Artifact MUST 导出对应的持久化 root-cause decision

#### Scenario: V3 validation origin is absent or unknown
- **WHEN** 最新 v3 Decision Validation 缺少 allowlisted validation origin
- **THEN** Artifact MUST NOT 导出 root-cause decision

### Requirement: Historical v2 artifacts remain compatible

Evaluator SHALL 继续按照历史 v2 合同读取已持久化 Run，避免新审计字段使旧结果失效。

#### Scenario: Historical v2 valid decision has no origin
- **WHEN** 历史 `evidence-driven-v2` Run 的最新 Decision Validation 为 `status=valid` 且没有
  `validationOrigin`
- **THEN** Artifact MUST 继续导出该历史决策

### Requirement: Validator inputs remain answer isolated

Benchmark SHALL 保持 Agent、Validator、Prompt、RAG、报告与 evaluator-only 标准答案的物理和运行时
隔离。

#### Scenario: Benchmark validation executes
- **WHEN** Snapshot 或 Live Benchmark 运行 Decision Validation
- **THEN** Agent、LLM Validator、Prompt、RAG 和报告生成器 MUST NOT 读取 Ground Truth 或 oracle 字段

### Requirement: Evidence-driven v4 artifacts expose auditable dispositions

`evidence-driven-v4` Artifact SHALL record graph/workflow version, every public hypothesis disposition, cited Evidence,
reason code, assessment source, model-call count, safe role audit, Validator routing, resume count, and recovery policy.

#### Scenario: Closed v4 hypothesis lacks evidence
- **WHEN** a v4 Artifact marks a hypothesis refuted or causally inactive without current-task public Evidence
- **THEN** scoring MUST fail the differential-diagnosis hard gate

#### Scenario: Sensitive execution data exists internally
- **WHEN** checkpoint blobs or provider failures contain prompts, model responses, credentials, private reasoning, or raw CLS logs
- **THEN** Artifact and public evidence-chain API MUST NOT expose those values

### Requirement: Historical artifact scoring remains stable

Benchmark SHALL dispatch v4-only semantics by explicit `workflowVersion`. Existing v2 and v3 fixtures SHALL remain
readable and preserve their prior total score, hard gates, required-evidence result, Validator interpretation, and recovery interpretation.

#### Scenario: A historical v3 artifact is rescored
- **WHEN** the upgraded scorer reads a frozen `evidence-driven-v3` fixture
- **THEN** its observable scoring result MUST equal the recorded pre-upgrade baseline
- **AND** v4 disposition requirements MUST NOT be applied to it

### Requirement: Benchmark enforces bounded and isolated execution

Benchmark SHALL reject answer access and SHALL record an efficiency failure when model-call count exceeds eight.

#### Scenario: Agent attempts answer access
- **WHEN** a scenario path traverses outside its allowlisted root, public input contains nested oracle fields, or Agent calls `ReadGroundTruth`
- **THEN** execution MUST fail the isolation gate without exposing the answer

#### Scenario: Model-call budget is exceeded
- **WHEN** an Artifact records more than eight model requests including retries
- **THEN** Benchmark MUST fail the bounded-execution gate and MUST NOT lower other thresholds to compensate

### Requirement: Investigation strategy override is benchmark-only

The internal Live Benchmark CLI SHALL support `auto`, `single`, and `multi` investigation strategies.
Ordinary diagnosis API clients MUST NOT force an investigation strategy.

#### Scenario: A benchmark run forces Single
- **WHEN** the internal CLI runs with `--strategy single`
- **THEN** the diagnostic execution SHALL retain all safety gates but bypass the Multi score threshold
- **AND** the terminal result SHALL record the requested and effective strategies

#### Scenario: A normal API request contains a strategy field
- **WHEN** an ordinary client attempts to force `multi`
- **THEN** the API SHALL reject or ignore the field according to its fixed request-schema policy
- **AND** the service-owned strategy mode SHALL remain `auto`

#### Scenario: Forced Multi fails a hard gate
- **WHEN** Benchmark requests `multi` but time, budget, capability, or source-count gates fail
- **THEN** Workflow MUST refuse Multi
- **AND** it SHALL persist the hard-gate reason

### Requirement: Strategy comparison uses persisted safe metrics

Every strategy run SHALL persist enough bounded metrics to reconstruct a paired A/B comparison after
process restart without storing Oracle labels, required Evidence identifiers, Prompt text, or raw logs.

#### Scenario: Runtime objects are discarded after a run
- **WHEN** an evaluator completes a run and the in-memory RunArtifact is no longer available
- **THEN** the system SHALL reconstruct strategy, policy, correctness, Evidence Recall, duration, model-call count, duplicate-Evidence rate, fallback, and security hard-gate values from persisted owner-scoped records

#### Scenario: Sensitive evaluator content is offered to terminal persistence
- **WHEN** terminal metrics or metadata contain expected cause, required Evidence, Oracle, Prompt, credential, or raw-log shaped data
- **THEN** persistence MUST reject the payload

### Requirement: Multi-agent default requires measured benefit

Multi-Agent MUST NOT become the default production route unless paired runs satisfy both capability and
performance gates with no safety failure.

#### Scenario: Performance passes without capability gain
- **WHEN** Multi P95 is at most 1.5 times Single and extra model calls are at most 2 but neither Evidence Recall improves by 10 percentage points nor Root Cause Top-1 improves by 5 percentage points
- **THEN** the result SHALL remain `benchmark_only`

#### Scenario: Capability and performance gates pass
- **WHEN** paired runs satisfy a required capability gain, Multi P95 is at most 1.5 times Single, duplicate Evidence is at most 10 percent, extra model calls are at most 2, and every safety hard gate passes
- **THEN** the result MAY become `eligible_for_default_review`
- **AND** production default enablement SHALL still require an explicit decision

### Requirement: Order pool leak Live scenario is cross-source and reproducible

The system SHALL reproduce connection-pool exhaustion through an isolated order-api's real asyncpg
connection lifecycle and SHALL produce complementary Runtime and actual service-log evidence.

#### Scenario: Runtime alone does not reveal the exception lifecycle
- **WHEN** the Runtime Investigator inspects a saturated pool and PostgreSQL sessions
- **THEN** it SHALL prove pool exhaustion, request timeout, database reachability, and absence of lock waits
- **AND** it SHALL NOT return an Oracle field or connection-leak answer label

#### Scenario: CLS records originate from order-api events
- **WHEN** the CLS evidence preparer handles the order-pool scenario
- **THEN** uploaded records SHALL originate from the current run's order-api `/events` output
- **AND** evaluator-authored fault-answer templates MUST NOT be used

#### Scenario: The fault uses a real run-scoped order update
- **WHEN** the scenario establishes its baseline, injects the exception path, and verifies recovery
- **THEN** it SHALL execute parameterized updates against only the current run's test order
- **AND** Cleanup SHALL remove that order and all held connections

### Requirement: Single and Multi strategy comparison is fair

The system SHALL expose the same tools, trusted arguments, model, knowledge base, shared global budgets,
and scorer to Single and Multi strategies; only Runtime/Log investigation scheduling MAY differ.

#### Scenario: Multi investigators execute concurrently
- **WHEN** Runtime and Log Investigators claim step or model budget concurrently
- **THEN** they SHALL consume one atomic global budget rather than per-investigator copies
- **AND** budget exhaustion SHALL fail closed without adding unexecuted Evidence

### Requirement: Isolated recovery is scoped and idempotent

The system SHALL execute at most one restart of the isolated current-run order-api and SHALL independently
verify old-connection release, new-generation readiness, database health, and business recovery.

#### Scenario: A process fails after restart but before terminal persistence
- **WHEN** the same recovery intent is replayed after coordinator reconstruction
- **THEN** the system SHALL require manual review for an uncertain side effect
- **AND** it MUST NOT restart the service again

#### Scenario: Production semantics request recovery
- **WHEN** the diagnosis targets a non-isolated environment
- **THEN** the system SHALL produce a human-approval proposal only
- **AND** it MUST NOT execute restart or rollback
