# AIOps 因果意图路由与结构化 Decision 设计

**日期：** 2026-08-17  
**状态：** 已确认
**范围：** AIOps LangGraph Planner、Evidence Evaluation、Sufficiency Gate、Replanner、Decision

## 背景与问题

真实 `APY-013` 已正确调用 `InspectPostgresErrors`、`InspectPostgresWaitGraph` 和
`InspectTransactionResourceOrder`，三条必要证据全部收集成功，说明 MCP discovery、参数归一化、
Tool Calling 和 Snapshot 精确匹配没有发生故障。但三次 Evidence Evaluation 都把
`causalRole` 标为 `mechanism`。现有 Plan Step 没有声明该步骤要回答 trigger、mechanism、
impact 还是 context，Evidence Evaluator 只能从一次模型输出临时推断角色。

LLM Decision 同时返回 `invalid_model_output`。新的 grounded fallback 要求唯一、证据绑定的
trigger，因而正确地拒绝从普通 mechanism 或 Public Hypothesis description 猜测根因。最终
没有 Root Cause Decision，真实运行安全失败为 `candidate_missing`，没有授权恢复。

## 目标

1. 让每个诊断步骤在执行前拥有公开、可审计的 `causalIntent`。
2. 让工具只声明通用的因果能力，不包含场景答案、Ground Truth 或 evaluator rubric。
3. 在进入 Decision 前确定性检查因果角色覆盖；缺失角色时定向 Replan，而不是继续生成。
4. 让 Decision 使用项目已有的 LangChain/Pydantic structured-output 模式和一次格式修复。
5. 保持现有六步 Executor 总预算、两次 Replan 上限、恢复安全门和 Benchmark 权重不变。

## 非目标

- 不修改 MCP 工具调用参数、Snapshot 注册调用或工具执行实现。
- 不把 APY-013 正确根因、Oracle、`ground_truth.yaml` 或语义评分词表注入 Agent。
- 不为通过 Benchmark 固定 `InspectTransactionResourceOrder` 永远等于 trigger。
- 不增加新的模型、Agent、外部服务、数据库或 Python 依赖。
- 不放宽 grounded fallback、Deterministic Validator 或 Recovery Policy。

## 复用决策

项目已经使用 `langgraph>=1.2.8`、Pydantic v2、LangChain structured output 和 OpenAI SDK。
`decision_validation.py` 已实现 structured invoker、Pydantic envelope 解析、一次格式修复以及
脱敏模型错误分类，应抽取或泛化最小公共适配边界供 Decision 使用，不能复制另一套供应商调用
逻辑。

GitHub Connector 搜索因服务端 HTTP 403 失败；备用 GitHub 开发者索引与 GitHub API 核实了
以下 MIT、未归档且近期活跃的参考：

- `langchain-ai/langgraph`：类型化 StateGraph、ValidationNode、校验失败条件回路；
- `crewAIInc/crewAI`：Plan Step、StepResult、PlannerObserver 和按观察结果 Replan；
- `agentspan-ai/agentspan`：确定性 verifier 输出逐约束缺口，再将缺口传给下一轮生成。

采用“参考实现 + 包装复用现有内部能力”，不直接复制代码、不新增依赖。

## 数据合同

### CausalIntent

沿用公开 `CausalRole` 的四个值：

```python
CausalIntent = Literal["trigger", "mechanism", "impact", "context"]
```

`DiagnosticPlanStep` 增加必填 `causal_intent: CausalIntent`。新生成的 Planner/Replanner payload
持久化为 `causalIntent`。历史持久化 Plan 不参与当前运行恢复；解析缺失或非法值的新模型输出时
必须拒绝该 Plan，并进入现有 generic/fail-closed 路径，不能默认为 context 掩盖合同缺失。

Plan payload 另存 `causalIntentOrigin=model | coverage_repair | generic`，用于说明角色来自模型、
执行前确定性覆盖修复或通用计划。该字段不参与评分，也不改变工具 fingerprint。

### ToolCausalCapability

工具因果能力由项目内部纯函数提供，不修改 MCP 协议：

```python
def allowed_causal_intents(tool_name: str) -> frozenset[CausalIntent]: ...
```

能力描述通用观测语义，而非某个场景答案。首版采用显式允许列表与安全默认值：

- 顺序、配置变更、部署变更、重试策略类工具：`trigger | mechanism`；
- wait graph 类工具：`mechanism`；
- lock graph 类工具：`trigger | mechanism`；
- 错误、状态码和告警结果类工具：`mechanism | impact`；
- 容量、基线和一般指标类工具：`context | mechanism`；
- 只读会话/健康检查工具：`context | mechanism | impact` 中与其通用语义相符的子集；
- 未分类诊断工具：只允许 `context`，并在审计中暴露 `unknown_tool_capability`；
- 知识检索、写操作恢复和恢复提案工具不允许进入诊断 Plan causal intent 合同；只读健康验证
  可以作为 context 或 impact。

`_tool_contracts_payload` 向 Planner/Replanner 公开 `allowedCausalIntents`，不把这些字段写回
MCP server schema。

## LangGraph 数据流

```text
Planner(causalIntent)
  -> Executor
  -> Evidence Evaluator(role validation)
  -> Sufficiency Gate(role coverage)
       -> Executor：当前 Plan 仍有可用步骤
       -> Replanner：支持假设缺少 trigger/mechanism/impact
       -> Decision：因果角色覆盖完整
  -> Decision(structured output)
  -> Deterministic Validator
  -> LLM Validator
  -> Recovery Policy
```

### Planner

Planner Prompt 要求每步包含 `causalIntent`。`parse_plan` 同时验证：

1. 值属于允许列表；
2. intent 属于该工具的 `allowed_causal_intents`；
3. 工具、参数和 hypothesis 继续通过原有校验。

单步验证后必须在调用任何诊断工具之前执行计划级覆盖约束。新增纯函数在现有步骤与各工具允许
角色之间做有界、确定性的分配：优先保留已经形成唯一 trigger、至少一个 mechanism 和至少一个
impact 的模型计划；覆盖不完整但存在合法分配时，只修改最少数量的 intent，并以步骤顺序稳定
选择；不存在合法分配时拒绝模型 Plan，进入 generic/fail-closed 路径。该过程只使用工具能力和
公开 Plan，不读取工具结果、hypothesis 正确性或 Oracle。

例如能力集合为 resource-order=`trigger|mechanism`、wait-graph=`mechanism`、
errors=`mechanism|impact` 时，即使模型输出三个 mechanism，覆盖修复也会在执行前得到
resource-order=trigger、wait-graph=mechanism、errors=impact。它表达的是调查结构，不代表这些
观察最终支持某个 hypothesis；Evidence Evaluation 仍可通过 supports/refutes 拒绝错误假设。

Generic Plan 也必须显式携带 causal intent 并通过同一覆盖约束。通用 `SearchLog` 只能使用
`context` 或由公开工具能力允许的值，不能把日志搜索预设为正确 trigger。若当前 discovered
tools 无法覆盖三类角色，允许执行证据收集，但不得把不完整计划标为可直接进入 Decision。

### Evidence Evaluator

Evaluator Prompt 同时给出 Plan Step 的 `causalIntent` 和允许角色。模型仍负责生成 purpose、
supports、refutes、summary，但 `causalRole` 必须等于已验证的 Plan intent。

解析后进行确定性核对。若模型返回不同角色，不根据内容猜测，也不静默接受：使用 Plan 中已经
验证的 `causalIntent` 作为持久化角色，并记录允许列表审计字段
`causalRoleOrigin=plan_contract`、`reportedCausalRole` 和 `causalRoleCorrected=true`。这些字段不
包含 Prompt、模型原文或 Oracle。Plan intent 本身受工具能力约束，因此该归一化不会把场景
答案注入 Observation。

模型调用或 Observation schema 失败时，现有空 supports/refutes 降级仍保留，但角色使用 Plan
intent，并且该 Observation 不能单独让 hypothesis 变为 supported。

### Sufficiency Gate

新增纯函数计算唯一 supported hypothesis 的公开角色覆盖：

```python
@dataclass(frozen=True, slots=True)
class CausalCoverage:
    trigger_count: int
    mechanism_count: int
    impact_count: int
    missing_roles: tuple[Literal["trigger", "mechanism", "impact"], ...]
    ambiguous_trigger: bool
```

只统计同时满足以下条件的 Observation：

- supports 唯一 supported hypothesis；
- 至少一个 `evidenceIds` 属于该 hypothesis 的支持证据；
- summary 非空；
- causal role 为 trigger、mechanism 或 impact。

进入 Decision 的必要条件为唯一 trigger、至少一个 mechanism、至少一个 impact。若 LLM
Sufficiency 返回 sufficient 但覆盖不完整，确定性覆盖优先：

- Plan 中仍有能补齐缺口且未执行的步骤：路由 Executor；
- 无现成步骤、仍有预算：路由 Replanner，并持久化 `missingCausalRoles`；
- 预算耗尽：进入 Decision，但 Decision/fallback 继续 fail-closed，不能伪造角色。

多个 trigger 标记为 `ambiguousTrigger=true`，不得自动选择其中一个；Replanner 只能收集差分
证据或进入人工复核。

### Replanner

Replanner Prompt 接收 `missingCausalRoles` 和每个 discovered tool 的允许角色，只能生成能补齐
至少一个缺口的步骤。新步骤仍通过工具参数合同、intent 能力合同、执行 fingerprint 去重和剩余
预算检查。若模型返回与缺口无关的步骤，拒绝加入 Plan。已执行工具不能仅通过改变 intent 再次
调用；若证据已收集但 Plan intent 与角色覆盖冲突，必须依赖执行前覆盖修复或进入人工复核。

### Decision structured output

增加私有 Pydantic schema：component、mechanism、trigger、2～6 项 causalChain、非空
evidenceIds、0～1 confidence，`extra="forbid"`。Decision 调用复用 Validator 的 structured
invoker 行为：

1. 优先 `with_structured_output(..., method="function_calling", include_raw=True)`；
2. 不支持时兼容现有 raw model；
3. schema/JSON 错误时只追加固定格式修复提示并重试一次；
4. 模型调用异常使用已有脱敏错误分类，不读取或保存异常正文；
5. 成功解析后继续执行 label normalization、grounded chain repair 和确定性 Validator。

Decision 的错误审计新增允许列表阶段和代码，但不保存 Prompt、原始响应或 parsing error 内容。
整条 Benchmark 不自动重试。

## 安全与隔离

- 工具能力表只能引用工具通用语义，禁止引用 scenario ID、root-cause label 或 Oracle concept。
- Agent、Planner、RAG、工具能力表和 Decision 不得读取 `ground_truth.yaml`。
- `causalIntent` 不构成证据；没有支持性 Evidence ID 时不能产生 supported hypothesis 或 Decision。
- 角色归一化只修正枚举角色，不修改 summary、supports、refutes 或 evidenceIds。
- 缺少唯一 trigger 时 grounded fallback 继续返回 `None`。
- Validator 失败、Decision 缺失或角色歧义继续强制 manual review/deferred，
  `executionPermitted=false`。

## 兼容性

- 不改变现有 MCP 工具 schema、调用参数、Snapshot fixture 或真实 CLS 接口。
- `McpToolDefinition` 不增加远端协议字段；因果能力由本地 registry 包装。
- Artifact 继续读取历史 Observation 缺失 `causalRole` 为 context；新运行必须持久化角色来源审计。
- Benchmark 权重、通过阈值和历史 Archive 均不修改。

## 测试与验收

严格采用 TDD，并且不运行全量 pytest。专项测试至少覆盖：

1. Plan 缺失、非法或超出工具能力的 causalIntent 被拒绝；
2. 三个合法但全为 mechanism 的 Plan 在执行前被最小修复为 trigger/mechanism/impact；
3. 无法覆盖三类角色的工具集合不会被伪装成完整计划；
4. Tool Calling 参数和 fingerprint 在新增 intent 后保持不变；
5. 模型把 resource-order Observation 报为 mechanism 时，持久化角色按已验证 Plan 修正为 trigger；
6. Sufficiency 在缺 trigger 时路由定向 Replanner，而不是 Decision；
7. Replanner 不能添加与 missing role 无关或工具能力不兼容的步骤；
8. 多 trigger 保持歧义并 fail-closed；
9. Decision structured output 成功、一次格式修复、调用失败脱敏和重试耗尽；
10. Ground Truth、Prompt、原始响应和异常正文不进入 Artifact；
11. APY-013 脚本化链路得到 trigger → mechanism → impact，并通过确定性 Validator；
12. Ruff、Pyright、聚焦 OpenSpec 和既有十文件专项回归通过。

离线门全部通过后，只允许再运行一次真实 APY-013，不自动重试。验收要求：

- 三类公开因果角色覆盖完整且唯一 trigger；
- 产生 evidence-grounded Root Cause Decision；
- `observations_evaluated=5/5` 保持不回退；
- 失败时仍无自动恢复授权；
- PostgreSQL、Archive checksum 和文档记录保持一致。

## 风险与缓解

- **工具能力表过度具体：** 使用通用语义类别并用隔离测试禁止 scenario/oracle token。
- **Plan intent 自身判断错误：** 工具能力只限制可能角色，不保证答案；Evidence supports 和最终
  Validator 仍要求真实证据；执行前覆盖修复只满足调查结构，不生成支持关系或根因内容，角色
  歧义不自动消解。
- **定向 Replan 消耗预算：** 沿用现有总步骤和 Replan 上限，不新增无限循环。
- **structured output 供应商差异：** 复用现有兼容 raw fallback 和脱敏错误分类，不增加 SDK。
- **真实模型仍不稳定：** 保留一次格式修复与 deterministic fallback，完整运行不自动重试。
