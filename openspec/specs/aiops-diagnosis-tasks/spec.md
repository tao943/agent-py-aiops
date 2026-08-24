# aiops-diagnosis-tasks Specification

## Purpose

使用基于证据的、tenant 范围的 AIOps 诊断，通过 LangGraph 计划-执行-重新计划工作流和共享流式协议。
## Requirements
### Requirement: Plan-Execute-Replan diagnostic graph
后端 SHALL 通过具有命名 `Planner`、`Executor`、`Replanner` 和 `Report` 节点的 LangGraph 工作流执行每个经过身份验证的 AIOps 诊断。

#### Scenario: Diagnostic follows the graph lifecycle
- **WHEN** 已认证的 user 启动诊断任务  
- **THEN** 后端 MUST 将任务保存为运行中，并在终端成功或失败前执行 Planner、Executor、Replanner 和 Report 节点。

#### Scenario: Replanner continues only when warranted
- **WHEN** Executor 返回需要其他限定诊断步骤的证据
- **THEN** Replanner MUST 调整计划并返回到 Executor；否则它 MUST 返回到 Report。

### Requirement: SOP-first diagnostic planning
在创建诊断计划之前，Planner SHALL 需要检索 tenant 授权的 SOP 或事件文档证据。

#### Scenario: Matching SOP informs plan
- **WHEN** 知识检索返回一个或多个 SOP 结果  
- **THEN** 计划和报告 MUST 识别已检索到的证据并优先推荐诊断操作。

#### Scenario: No SOP match is explicit
- **WHEN** 知识检索未返回任何结果  
- **THEN** 诊断 SSE 生命周期和最终报告 MUST 明确指出没有 SOP 匹配，并且该计划是通用的。

### Requirement: Evidence-based execution and reporting

AIOps 诊断 SHALL 使用公开告警和可用 MCP 工具形成有界计划，保存每次工具调用的目的、观测以及对已知假设的支持或反驳关系，并 SHALL 只依据可解析的证据记录生成结构化根因决策和最终报告。

#### Scenario: Real MCP evidence is used

- **WHEN** 诊断计划调用 CLS MCP 工具
- **THEN** Executor MUST 调用本地 MCP 服务器，并在其诊断证据中包含实际结果或明确的失败信息。

#### Scenario: 不支持的证据无法得出结论

- **WHEN** 执行没有用于可能结论的支持工具或检索证据
- **THEN** 报告 MUST 将该结论标记为未验证，而不是编造它。

#### Scenario: Multiple hypotheses share one symptom

- **WHEN** 一个告警公开多个可能根因
- **THEN** 工作流 MUST 使用至少两个独立观测区分 primary cause，并 MUST 保存至少一个被证据排除的竞争假设。

#### Scenario: Evidence is insufficient

- **WHEN** 工具失败、观测冲突或没有足够证据支持唯一根因
- **THEN** 结构化根因决策 MUST 为空或明确表示证据不足，报告 MUST NOT 编造确定结论。

### Requirement: Diagnostic persistence and ownership
后端 SHALL 通过 owner 范围的存储库边界保留诊断状态、计划、执行证据、节点 checkpoints、报告和工具审计。

#### Scenario: Diagnostic artifacts are owner scoped
- **WHEN** 一个 user 读取或流式传输诊断任务
- **THEN** 后端 MUST 仅公开该 user 拥有的任务、checkpoint、报告和审计。

#### Scenario: Terminal task is persisted
- **WHEN** 诊断完成或失败  
- **THEN** 任务状态、结果负载、完成时间戳以及任何报告 MUST 在 PostgreSQL 中持久化。

### Requirement: Diagnostic SSE lifecycle
AIOps 诊断 SHALL 由持久化后台任务执行，并将计划、步骤、工具、重规划、报告、完成和错误事件持久化后通过 SSE 订阅返回。

#### Scenario: Diagnostic stream disconnects
- **WHEN** 用户在诊断期间关闭或刷新页面
- **THEN** 诊断 MUST 继续执行，重新打开任务 MUST 能恢复事件和最终证据链。

### Requirement: Diagnostic workflow writes evidence-chain records

LangGraph Planner、Executor、Evidence Evaluator、Replanner、Decision 和 Report 节点 SHALL 通过诊断证据链存储库持续保存计划、工具观测、结构化假设更新、根因决策和报告来源。

#### Scenario: Workflow persists node artifacts

- **WHEN** 诊断在图节点中进行
- **THEN** 节点 MUST 存储有序的步骤记录以及在最终报告发出前生成的任何证据。

#### Scenario: Decision references evidence

- **WHEN** Decision 节点产生根因决策
- **THEN** 其中每个 evidence ID MUST 对应当前诊断任务已经持久化的证据记录。

### Requirement: Alert provenance is retained by a diagnostic task
后端 SHALL 保留从活动警报源中选择的警报的标准化源身份和原始上下文，这些警报来自持久化诊断输入、证据链、流式生命周期和最终报告输入。

#### Scenario: Operator diagnoses an alert from a configured source
- **WHEN** 已认证的操作员从标准化的主动警报中启动 AIOps 诊断
- **THEN** 持久化的诊断输入和初始警报证据 MUST 包括所选的源身份和原始提供者上下文，以及标准化的警报字段。

### Requirement: Structured Chinese alert analysis report
Report 节点 SHALL 基于诊断输入、告警上下文、SOP、执行计划和真实工具证据生成纯 Markdown 中文告警分析报告，并按照活跃告警清单、逐告警根因分析、处理方案执行、整体结论和风险评估的顺序组织内容。

#### Scenario: Evidence supports one or more active alerts
- **WHEN** Report 节点收到一个或多个真实活跃告警及对应工具证据
- **THEN** 最终报告 MUST 包含告警清单表格，并为每个告警分别展示详情、症状、日志证据、根因结论、排查步骤、处理建议和预期效果

#### Scenario: Evidence is missing or a tool fails
- **WHEN** 某个字段、SOP、日志证据或工具结果缺失或失败
- **THEN** 报告 MUST 在对应章节明确写出“未获取”“证据不足”或失败原因，MUST NOT 编造告警、日志、根因或执行结果

#### Scenario: Report model invocation fails
- **WHEN** LLM 报告生成调用失败、返回空内容或返回非 Markdown 结构
- **THEN** Report 节点 MUST 生成并持久化结构化中文 Markdown 回退报告，并如实保留已有证据和失败状态

#### Scenario: Report output remains plain Markdown
- **WHEN** Report 节点完成报告生成
- **THEN** 持久化内容和 SSE `report` 事件 MUST 包含纯 Markdown 文本，MUST NOT 包含 JSON 报告对象或包裹全文的代码围栏

### Requirement: Decision validation failures remain distinct from evidence gaps

后端 SHALL 先根据当前任务的公开 Hypothesis、Decision Vocabulary、持久化 Evidence 和
Evidence-linked Observation 验证根因候选，再调用 LLM Validator，并 SHALL 分开保存业务拒绝、
模型调用失败和模型格式失败。

#### Scenario: Validator provider failure after grounded checks pass
- **WHEN** candidate 通过全部公开确定性证据检查且 LLM Validator 调用失败
- **THEN** Workflow MUST 以 `validationOrigin=deterministic_grounded_fallback` 保留 candidate
- **AND** Workflow MUST NOT 进入证据 Replanner
- **AND** Recovery Policy MUST 要求人工审核或外部策略且 MUST NOT 允许执行

#### Scenario: Validator explicitly rejects supported fields
- **WHEN** LLM Validator 返回合法结构化 `invalid` 结果和具体 unsupported fields 或 missing evidence
- **THEN** Workflow MUST 将其分类为 `model_rejected`
- **AND** Workflow MAY 只执行一次有界、去重的 gap-targeted Replan

#### Scenario: Deterministic evidence contract fails
- **WHEN** candidate 缺少唯一支持、公开标签、当前任务正证据、独立 Observation 或 grounded causal chain
- **THEN** Workflow MUST fail closed
- **AND** Workflow MUST NOT 使用 deterministic fallback

#### Scenario: Candidate is absent
- **WHEN** Decision 节点没有生成可解析 candidate
- **THEN** Workflow MUST 保存 `candidate_missing`
- **AND** Workflow MUST NOT Replan 或授权恢复

### Requirement: Deterministic fallback uses only positive public evidence

确定性 fallback SHALL 只接受属于唯一 supported hypothesis 的 Observation Evidence IDs，且 SHALL
拒绝 Alert、Knowledge reference、Report、其他任务 Evidence 和未支持该 hypothesis 的 Evidence。

#### Scenario: Available but non-supporting evidence is cited
- **WHEN** candidate 引用了当前任务中存在、但没有被 supporting Observation 绑定的 Evidence ID
- **THEN** 确定性验证 MUST 拒绝 candidate

#### Scenario: Ground Truth is isolated
- **WHEN** Decision Validator 或确定性验证运行
- **THEN** 其输入和可调用工具 MUST NOT 包含 `ground_truth.yaml`、oracle 字段或隐藏评分答案

### Requirement: Resilient validation is auditable and recovery restricted

`evidence-driven-v3` Workflow SHALL 在现有 step/checkpoint JSONB payload 中保存 validation origin、
allowlisted error category、尝试次数、warning 和 deterministic check results，且 SHALL NOT 保存完整
模型输出、Prompt、异常堆栈或凭据。

#### Scenario: Deterministic fallback reaches recovery planning
- **WHEN** v3 Workflow 保留 deterministic fallback candidate
- **THEN** Recovery Plan MUST 使用 `manual_review`
- **AND** Policy Gate MUST 返回 `executionPermitted=false`

### Requirement: Structured-output steering follows model capability

后端 SHALL 从当前 Chat Model 的 capability profile 选择 LangChain structured-output 方法，且 SHALL
以不含凭据、真实日志或隐藏答案的最小 Live readiness 验证该请求形态。

#### Scenario: Model profile selects JSON mode
- **WHEN** 当前模型 capability 声明 `structuredOutputMethod=json_mode`
- **THEN** Decision 与 Validator MUST 使用 `json_mode` 构造 structured invoker
- **AND** Workflow MUST 保留既有 schema 校验、一次格式纠正和安全错误分类

### Requirement: Semantic validation uses an independently configured model

后端 SHALL 允许 Decision Validator 使用与主 Agent 不同的 Chat Model，并 SHALL 复用同一
Provider 的凭据、Base URL、timeout 与 retry 设置。未配置独立 Validator 的历史配置和测试
Provider SHALL 回退主 Chat Model；只有 Decision Validator 节点可使用该独立模型。

#### Scenario: Dedicated Validator is configured
- **WHEN** `validatorModel=qwen3.8-max` 且其 capability 声明 `structuredOutputMethod=json_mode`
- **THEN** Decision Validator MUST 使用 `qwen3.8-max` 和 `json_mode`
- **AND** Planner、Executor、Evidence Evaluation、Sufficiency、Decision、Recovery 与 Report MUST 继续使用主 Agent 模型

#### Scenario: Validator model profile is missing
- **WHEN** `validatorModel` 指向没有 capability profile 的模型
- **THEN** 配置加载 MUST fail closed
- **AND** 系统 MUST NOT 静默切换到未知模型

### Requirement: Validator parse diagnostics are secret-safe and auditable

Workflow SHALL 将 structured parse 失败分类为允许列表中的 `invalid_json`、
`structured_envelope_mismatch`、`missing_required_field`、`invalid_enum`、
`wrong_container_type`、`extra_field`、`unknown_evidence_id` 或
`invalid_json_or_schema`，并 SHALL 在 Step、Checkpoint 与 Run Artifact 中保存安全模型名、
错误码、category、phase 和尝试次数。

#### Scenario: First response needs a format correction
- **WHEN** Validator 第一次响应无法通过 JSON 或 Schema 校验且第二次响应合法
- **THEN** 最终 validation error category MUST 为空
- **AND** 审计 MUST 保留第一次的安全 parse code

#### Scenario: Format correction has insufficient deadline
- **WHEN** Validator 第一次响应无法通过 JSON 或 Schema 校验且剩余 hard deadline 少于一个完整 Validator role timeout 加 5 秒调度余量
- **THEN** Workflow MUST NOT 发起第二次模型调用或消耗第二次模型预算
- **AND** Workflow MUST 保存首次安全 parse code 和 `retry_skipped_insufficient_deadline`
- **AND** Recovery Policy MUST 保持 `executionPermitted=false` 并进入人工审核

#### Scenario: Validator receives an exact public output contract
- **WHEN** Workflow 构造 LLM Validator 请求
- **THEN** Prompt MUST 明确五个字段、字段类型、`valid/invalid` 枚举、空数组语义和禁止额外字段
- **AND** Pydantic Schema 与 Evidence ID allowlist MUST 继续作为最终接受条件
- **AND** Prompt MUST NOT 包含 Ground Truth、Oracle、凭据或原始 CLS 日志

#### Scenario: Parse failure is exhausted
- **WHEN** 两次 Validator 响应均无法通过 JSON 或 Schema 校验
- **THEN** Workflow MUST 保存 `retry_exhausted` 和 `structured_parse`
- **AND** Workflow MUST NOT 保存 Prompt、原始响应、异常正文、字段值、凭据、Ground Truth、Oracle 或原始 CLS 日志

#### Scenario: Validation audit contains malformed metadata
- **WHEN** 持久化 validation payload 包含未知枚举值或不符合 `^[A-Za-z0-9._-]{1,120}$` 的模型名
- **THEN** Run Artifact MUST 丢弃该值
- **AND** Benchmark scoring MUST 保持不变

### Requirement: Persisted hypothesis state is authoritative for sufficiency

Sufficiency Gate SHALL 从公开 Hypothesis 全集和持久化 Hypothesis State 确定
supported、refuted 与 unresolved 分类，且 SHALL NOT 允许模型输出覆盖该分类。

#### Scenario: Model prematurely closes an open competitor
- **WHEN** 模型报告 `sufficient`，但一个公开竞争假设在持久化状态中仍为 `open`
- **THEN** Workflow MUST 保存 `status=insufficient` 和该 unresolved hypothesis
- **AND** Workflow MUST 优先执行尚未运行且 `testsHypotheses` 覆盖该 competitor 的 Plan Step

#### Scenario: Persisted state is incomplete or malformed
- **WHEN** 公开 hypothesis 缺少状态，或状态包含重复/非公开 ID
- **THEN** Workflow MUST fail closed 为 insufficient
- **AND** Workflow MUST NOT 把非公开 ID 写入 Sufficiency 审计

### Requirement: Grounded expression normalization preserves strict validation

Workflow MAY 在 Validator 前把 LLM Candidate 的 trigger 和 causal chain 规范化为当前任务
supporting Observation 的精确 summary，但 MUST 保留 component、mechanism、Evidence IDs 和
confidence，并 MUST 在规范化前后运行同一确定性 Validator。

#### Scenario: Only expression checks fail
- **WHEN** Candidate 仅未通过 `trigger_present` 或 `grounded_causal_chain`
- **AND** 唯一 supported hypothesis 没有 open competitor
- **AND** Candidate 引用了规范化所使用的全部 supporting Observation Evidence
- **THEN** Workflow MAY 生成 `llm_grounded_normalization`
- **AND** 规范化结果 MUST 通过全部确定性 checks 才能进入 LLM Validator

#### Scenario: Candidate does not cite a copied observation
- **WHEN** 规范化链需要使用一个 Candidate 未引用 Evidence ID 的 supporting Observation
- **THEN** Workflow MUST NOT 规范化 Candidate
- **AND** 后续验证与恢复 MUST 保持 fail closed

### Requirement: Hypothesis disposition is evidence-audited

Workflow SHALL represent every public hypothesis as `supported`, `refuted`, `causally_inactive`, or
`unresolved`. A `refuted` or `causally_inactive` disposition MUST cite current-task public Evidence.

#### Scenario: A complete cause does not dismiss a competitor
- **WHEN** one supported cause explains the incident but no evidence addresses another competitor
- **THEN** the competitor MUST remain `unresolved`

#### Scenario: Exactly one cause is sufficient
- **WHEN** exactly one hypothesis is supported and every active competitor is grounded as refuted or causally inactive
- **THEN** Workflow MAY form a single root-cause decision

#### Scenario: Multiple causes remain supported
- **WHEN** more than one public hypothesis remains supported under the v4 single-root schema
- **THEN** Workflow MUST fail closed to manual review

### Requirement: Deterministic adjudication uses trusted public rules

Workflow SHALL derive bounded typed facts only from secret-filtered public Observation fields. Planner-proposed
deterministic rules MUST instantiate a code-owned trusted template and MUST NOT define an arbitrary fact-to-disposition mapping.

#### Scenario: Planner proposes an unauthorized causal mapping
- **WHEN** a rule uses valid public facts but its template does not authorize that hypothesis and disposition
- **THEN** Workflow MUST NOT close the hypothesis deterministically
- **AND** Workflow MUST treat the rule as unresolved input for the bounded Adjudicator path

#### Scenario: Observation contains a secret-shaped field
- **WHEN** a tool result contains password, token, authorization, secret, or API-key shaped fields
- **THEN** Fact Adapter MUST omit those fields from facts, checkpoints, prompts, and public API payloads

### Requirement: LLM Adjudicator output is structured and safely diagnosable

Workflow SHALL request the provider-supported structured-output contract for each bounded Adjudicator batch.
It SHALL validate the returned hypothesis IDs, Evidence IDs, complete batch coverage, transition legality, and
causal coverage locally. It SHALL persist only allowlisted rejection codes and MUST NOT persist raw model output,
provider exception text, hidden reasoning, or response fragments.

#### Scenario: Model returns a syntactically valid but ungrounded batch
- **WHEN** an Adjudicator response references an unknown hypothesis or Evidence ID
- **THEN** Workflow MUST reject the complete batch without applying a partial transition
- **AND** it MUST record `unknown_identifier` and `incomplete_batch` without recording model content

#### Scenario: Format correction cannot complete before the deadline
- **WHEN** the first response is rejected and the soft deadline prevents the correction attempt
- **THEN** Workflow MUST record `soft_deadline_exceeded` and fail closed without a root-cause decision

### Requirement: Validation is deterministic first and risk-routed

Workflow SHALL always run Deterministic Validator. It SHALL call LLM Validator only when a code-computed risk
condition requires semantic adjudication, automatic recovery review, L2/L3 review, cross-component causality,
or high-quality conflict review.

#### Scenario: Pure deterministic evidence closes the case
- **WHEN** deterministic evidence produces one supported cause, grounded closed alternatives, and manual-only recovery
- **THEN** Workflow MUST skip LLM Validator and record allowlisted skip reasons

#### Scenario: Deterministic validation fails
- **WHEN** deterministic checks reject a candidate and no unused targeted replan is eligible
- **THEN** Workflow MUST create a deterministic manual-review plan
- **AND** Workflow MUST NOT spend an LLM Recovery Planner or Validator call

### Requirement: Compound trusted patterns remain answer-isolated

Workflow SHALL close multiple hypotheses deterministically only when a code-owned compound pattern matches
current-task public Evidence. The pattern MUST NOT read scenario identity, run identity, Oracle, Ground Truth,
score rules, or fixture values.

#### Scenario: Nginx timeout facts close differentiated alternatives
- **WHEN** one request has HTTP 504, upstream connect success, read deadline elapsed, independently healthy upstream and gateway probes, and an incident-scoped upstream-timeout event
- **THEN** Workflow SHALL support upstream response timeout
- **AND** every closed competitor MUST cite the direct current-task Evidence that closes it

#### Scenario: PostgreSQL row-lock facts form a cross-tool causal chain
- **WHEN** current-task public Evidence confirms a PostgreSQL Lock wait, a blocker-to-waiter edge, database reachability, a timed-out business probe, and matching contention and timeout log events
- **THEN** Workflow SHALL support PostgreSQL row-lock blocking
- **AND** it SHALL refute connectivity failure and an unlocked slow query using their direct counter-evidence
- **AND** it SHALL project separate trigger, mechanism, and impact observations

#### Scenario: One required fact is absent or belongs to another task
- **WHEN** any required fact is absent, contradicted, or its Evidence ID is not owned by the current task
- **THEN** Workflow MUST leave the affected hypothesis unresolved or conflicting
- **AND** it MUST NOT infer the benchmark answer from scenario identity

### Requirement: Replanner model calls require a provably useful search space

Before calling the Replanner model, Workflow SHALL determine from discovered tool schemas, trusted argument
contracts, execution-owned bindings, and causal capabilities whether at least one unexecuted contract-valid step
can still address the current evidence gap. If no such step can exist under bounded trusted arguments, Workflow
MUST persist `no_useful_step` without spending a Replanner model call.

#### Scenario: Bounded diagnostic calls are exhausted
- **WHEN** every gap-relevant tool has only empty, constant, or execution-owned arguments and every canonical call has already completed
- **THEN** Workflow MUST skip the Replanner model and record an allowlisted skip reason

#### Scenario: A free legal parameter space remains
- **WHEN** a gap-relevant tool accepts a legal parameter that code cannot prove exhausted
- **THEN** Workflow MAY call the bounded Replanner model once

### Requirement: Model and time budgets survive restart

Workflow SHALL limit all model requests, including retries and format corrections, to eight calls. It SHALL persist
model-call count, role audit, one-Replanner limit, five-minute soft deadline, and eight-minute hard deadline in graph state.

#### Scenario: Worker resumes a partially used budget
- **WHEN** a new Worker resumes the same task and graph version from PostgreSQL
- **THEN** remaining call and time budgets MUST be calculated from the persisted original values
- **AND** restart MUST NOT reset model count, Replanner count, or deadlines

#### Scenario: Hard deadline expires
- **WHEN** the persisted hard deadline has passed
- **THEN** Workflow MUST stop new model and tool work and persist only a safe terminal state and template report

### Requirement: Diagnostic side effects are idempotent

Workflow SHALL use stable IDs and conflict-safe persistence for initial alert Evidence, diagnostic Steps and Evidence,
tool audits, report links, model executions, tool executions, recovery intents, and audit events.

#### Scenario: Crash follows an evidence commit
- **WHEN** Evidence is committed but execution completion or graph checkpoint persistence fails before acknowledgement
- **THEN** resumed execution MUST reuse the same stable Evidence identity
- **AND** the evidence chain MUST NOT contain a duplicate logical record

### Requirement: Investigation strategy is deterministic and auditable

Workflow SHALL select `deterministic_fast_path`, `single_agent`, or `multi_agent` from current-task
public state, versioned policy, capability availability, remaining time, and remaining model budget.
It MUST NOT read scenario identity, run identity, Oracle, Ground Truth, score rules, or fixture values.

#### Scenario: Complexity is below the multi-agent threshold
- **WHEN** no hard gate requires stopping and the public route score is between 0 and 3
- **THEN** Workflow SHALL select `single_agent`
- **AND** it SHALL persist the policy version, score, and stable reason codes

#### Scenario: Deterministic evidence is already sufficient
- **WHEN** a current-task trusted pattern has closed the active hypotheses or `decisionReady` is true
- **THEN** Workflow SHALL select `deterministic_fast_path`
- **AND** it MUST NOT start another Executor or Investigator Dispatch
- **AND** it SHALL continue through Sufficiency, Decision, Validator, Recovery, Policy, and Report

#### Scenario: Multiple independent public sources are required
- **WHEN** the score is at least 5 and at least two unfinished trusted source Dispatches pass all time and budget gates
- **THEN** Workflow MAY select `multi_agent`
- **AND** selected Investigator types SHALL have a stable order

#### Scenario: Auto routing reaches the Multi threshold during shadow release
- **WHEN** service-owned `auto` reaches the Multi threshold and all hard gates pass
- **THEN** Workflow SHALL persist the suggested Multi route and matched score features
- **AND** it SHALL execute Single until automatic Multi receives separate approval

### Requirement: Parallel investigators have isolated capabilities

Runtime and Log Investigators SHALL receive only source-scoped read-only tools and MUST return a
schema-valid EvidencePacket without mutating shared Fact, Hypothesis, or Observation state.

#### Scenario: Runtime and Log steps are independent
- **WHEN** a Multi route selects trusted unfinished Runtime and Log Dispatches
- **THEN** Workflow MAY execute them concurrently
- **AND** each branch SHALL write only its own Packet, Evidence, audit, checkpoint, and progress event

#### Scenario: An Investigator attempts a recovery action
- **WHEN** a plan step names a recovery, proposal-only, external-policy, or otherwise writable tool
- **THEN** the capability registry MUST reject the step
- **AND** the tool MUST NOT be dispatched

#### Scenario: A Specialist performs bounded local reasoning
- **WHEN** Runtime or Log is dispatched as a Specialist
- **THEN** it SHALL receive isolated local state and an immutable parent assignment
- **AND** it SHALL use at most one Local Planning call, three tool steps, and one Evidence Analysis call
- **AND** it MUST NOT read another Specialist's local plan, raw observations, or private reasoning

#### Scenario: A Log Specialist proposes wider query arguments
- **WHEN** its Local Plan changes any prepared CLS scope argument
- **THEN** the runtime MUST reject the proposal before the tool call
- **AND** only the code-owned exact argument binding MAY reach the CLS MCP server

### Requirement: Multi-agent tools require explicit read-only trust

Discovery alone MUST NOT classify an MCP tool as safe. Only a code-owned capability descriptor with
`read_only=true` MAY expose a discovered tool to an Investigator; unknown tools SHALL fail closed.

#### Scenario: A user MCP server exposes an unknown tool
- **WHEN** discovery returns a tool without a matching trusted capability descriptor
- **THEN** Workflow MUST NOT classify it as Runtime or Log
- **AND** Multi-Agent routing MUST NOT dispatch it

#### Scenario: Model output forges a source domain
- **WHEN** a Planner step reports a source domain that differs from the trusted tool registry
- **THEN** Workflow SHALL replace or reject the reported domain using the registry
- **AND** the model-provided domain MUST NOT expand tool access

### Requirement: Evidence aggregation is deterministic and single-writer

Workflow SHALL validate Packet ownership, completed tool audit, Evidence quality, temporal scope,
deduplication, and stable ordering before one Aggregator writes shared diagnostic state.

#### Scenario: Parallel completion order changes
- **WHEN** the same Runtime and Log Packets arrive in different coroutine completion orders
- **THEN** Aggregator SHALL produce identical Facts, Observations, Hypothesis states, and Evidence order

#### Scenario: One Evidence is cited by multiple Packets
- **WHEN** multiple claims cite the same current-task Evidence
- **THEN** Aggregator MUST NOT count it as independent duplicate evidence

#### Scenario: Different Evidence IDs share one source fingerprint
- **WHEN** claims cite different Evidence IDs produced by the same underlying scoped source query
- **THEN** Aggregator SHALL retain auditable IDs but count one independent source group
- **AND** repeated source groups MUST add zero independent-evidence credit

#### Scenario: Incident state differs from current health
- **WHEN** one direct claim describes an incident-window failure and another describes current health
- **THEN** Aggregator MUST preserve both time scopes
- **AND** it MUST NOT automatically classify them as conflicting

### Requirement: Multi-agent dispatch is resumable and fail-closed

Completed Dispatches SHALL be reused by stable dispatch key. Timeout, partial failure, late result,
and all-failed paths MUST NOT be interpreted as negative evidence.

#### Scenario: Worker restarts after one branch completes
- **WHEN** one Dispatch is completed and another is unfinished at restart
- **THEN** Workflow SHALL reuse the completed Packet
- **AND** it SHALL resume only the unfinished logical Dispatch without duplicating tool calls or Evidence

#### Scenario: One Investigator times out
- **WHEN** one Packet completes and another times out
- **THEN** Workflow SHALL aggregate the completed Packet
- **AND** timeout SHALL be recorded as a limitation rather than a refuting claim

#### Scenario: Every Investigator fails
- **WHEN** all selected Dispatches fail during a Benchmark-forced Multi run
- **THEN** Workflow SHALL record `multi_investigation_failed`
- **AND** it MUST NOT execute Single to replace the terminal Multi result

#### Scenario: Parallel branches consume one model budget
- **WHEN** Runtime and Log role calls execute concurrently
- **THEN** the parent SHALL reserve their maximum optional budget before dispatch
- **AND** fan-in SHALL settle only unique persisted successful logical role calls
- **AND** replay or partial failure MUST NOT double-charge the run budget

#### Scenario: A result arrives after decision readiness
- **WHEN** an already-issued Dispatch completes after the task has become decision-ready
- **THEN** Workflow SHALL record `late_result_ignored`
- **AND** it MUST NOT modify Decision, Report, recovery authorization, or scoreable Artifact state

### Requirement: Graph topology versions isolate resumable state

The new investigation topology SHALL use graph version `aiops-diagnostic-v3`. A v2 checkpoint MUST
only resume with the legacy topology and MUST NOT be injected into v3 channels.

#### Scenario: An unfinished v2 task is resumed after deployment
- **WHEN** a task owns an `aiops-diagnostic-v2` checkpoint
- **THEN** Workflow SHALL select the legacy graph and v2 thread namespace
- **AND** it MUST NOT query or write the v3 checkpoint namespace for that execution

#### Scenario: A new task starts on the investigation topology
- **WHEN** no historical graph version is persisted for a newly accepted task
- **THEN** Workflow SHALL persist `aiops-diagnostic-v3` before graph execution
- **AND** all node, model, tool, checkpoint, and Artifact identities SHALL use v3

### Requirement: Cross-source investigation preserves evidence boundaries

Runtime and Log Investigators SHALL emit bounded EvidencePackets and SHALL NOT write shared hypotheses,
decisions, recovery authorization, or evaluator-private state.

#### Scenario: Runtime and Log evidence are each incomplete
- **WHEN** only Runtime evidence or only order-api lifecycle logs are available
- **THEN** the diagnosis SHALL record the supported partial facts and limitations
- **AND** it MUST NOT claim a complete exception-path connection-leak causal chain

#### Scenario: The Aggregator receives both sources
- **WHEN** scoped Runtime and CLS packets agree on run, incident, time window, and generation
- **THEN** the Aggregator MAY construct the checkout-without-checkin to pool-saturation causal chain
- **AND** every causal claim SHALL cite the supporting packet identifiers

### Requirement: Order-pool diagnostic tools are read-only and answer-isolated

The trusted order-pool Runtime server SHALL expose only scoped pool state, scoped database sessions,
database reachability, and business-probe outcome.

#### Scenario: A tool output is persisted or shown to the Agent
- **WHEN** the order-pool Runtime client returns an observation
- **THEN** it MUST NOT contain credentials, raw SQL, PIDs, fault tokens, Oracle fields, Ground Truth,
  `primary_cause`, or a connection-leak answer label

#### Scenario: A strategy is forced by the internal Benchmark CLI
- **WHEN** either `single` or `multi` is requested
- **THEN** both strategies SHALL receive the same discovered tool catalog and trusted argument bindings
- **AND** ordinary diagnosis API behavior SHALL remain service-owned `auto`

### Requirement: Order-pool Single diagnosis has a trusted deterministic closure

The Workflow SHALL support `order_connection_lifecycle_failure` only when current-task trusted
evidence establishes checkout, failed update, acquire timeout, missing checkin, saturated pool,
zero free connections, an observed waiter, current scoped PostgreSQL sessions, database reachability,
absence of lock wait, and a timed-out business acquisition probe.

#### Scenario: The complete compound pattern is present
- **WHEN** every required lifecycle, Runtime, database, rule-out, and impact fact is present and consistent
- **THEN** Workflow SHALL support `order_connection_lifecycle_failure`
- **AND** it SHALL refute database-unreachable and database-lock-wait alternatives
- **AND** trigger, mechanism, and impact SHALL cite independent trusted source groups

#### Scenario: A required fact is missing, conflicting, foreign, or duplicated
- **WHEN** any required fact is absent or contradictory, belongs to another owner or task, or reuses an existing source fingerprint under another Evidence ID
- **THEN** the compound resolver MUST fail closed
- **AND** it MUST NOT create a supported lifecycle hypothesis or grounded recovery authorization

### Requirement: Order-pool Specialists preserve immutable scope and bounded behavior

Runtime and Log Specialists SHALL receive an immutable public assignment, SHALL have isolated local
state, and SHALL run at most one Local Planning role, three read-only tool steps, and one Evidence
Analysis role. Each structured role MAY make one format-correction retry under the same logical call.

#### Scenario: A Log Local Plan changes its prepared query scope
- **WHEN** model output changes Region, TopicId, time window, run, incident, owner, Query, or Limit
- **THEN** Workflow SHALL reject the arguments before MCP invocation
- **AND** it SHALL retain the exact code-owned trusted argument binding

#### Scenario: A Specialist completes model analysis
- **WHEN** a Specialist produces a schema-valid result
- **THEN** only public facts, Evidence IDs, tested hypotheses, limitations, counters, and stable checksums SHALL be persisted
- **AND** raw prompts, raw model responses, credentials, and private reasoning MUST NOT be persisted

### Requirement: Order-pool Multi aggregation is deterministic and non-decisional

The Aggregator SHALL validate owner, task, role, tool audit, Evidence provenance, source fingerprint,
temporal scope, budget use, and stable result checksum without calling an LLM or voting on a cause.

#### Scenario: Specialist completion order changes
- **WHEN** identical Runtime and Log results arrive in either completion order
- **THEN** aggregation checksum, normalized facts, source groups, conflicts, and missing domains SHALL be identical
- **AND** the Aggregator MUST NOT create a RootCauseDecision or authorize recovery

#### Scenario: One Specialist fails or times out
- **WHEN** one role has completed Evidence and the other terminates unsuccessfully
- **THEN** Workflow SHALL preserve the completed Evidence and record the missing domain
- **AND** incomplete closure MUST prevent unsafe automatic recovery

### Requirement: Order-pool Multi release is benchmark-forced then shadow-only

The first release SHALL permit effective Multi execution only for the internal Order Pool Benchmark.
Ordinary `auto` requests SHALL persist the candidate score and reasons but SHALL execute Single.

#### Scenario: Forced Multi fails
- **WHEN** the internal Benchmark requested Multi and either Specialist or aggregation fails
- **THEN** the terminal result SHALL remain a Multi failure
- **AND** Workflow MUST NOT silently execute Single to replace or overwrite that result

#### Scenario: Auto identifies a Multi candidate
- **WHEN** the public route score is at least 5 and all capability, budget, and deadline gates pass
- **THEN** Workflow SHALL persist a shadow Multi candidate
- **AND** the effective production strategy SHALL remain Single until separately approved

### Requirement: Specialist replay is PostgreSQL-authoritative

Completed role, tool, Evidence, and aggregation identities SHALL be recoverable from PostgreSQL and
SHALL be charged or applied at most once per logical identity.

#### Scenario: A worker restarts after a role or tool completed
- **WHEN** the same task, graph version, specialist role, logical role or step, and input fingerprint are replayed
- **THEN** Workflow SHALL reuse the persisted completed result
- **AND** it MUST NOT repeat the model call, tool call, Evidence append, or model-budget charge
