# 可审计假设裁决与可恢复 Agent Workflow 设计

## 1. 背景与目标

当前 AIOps LangGraph 使用 `open / supported / refuted` 和浮点置信度表达候选根因，Evidence
Evaluator 与 Sufficiency Gate 在每轮取证后重复调用 LLM。真实 APY-003 已证明该模型无法准确
表达“某问题可能存在，但公开证据证明它不是本次事故的活跃原因”：停止的 upstream 进程已经
解释故障，而潜在端口不匹配既不能被严格反驳，也不应继续作为活跃竞争根因。结果是必要证据
10/10、主因正确，但因仍有 `open` 假设触发 `missing_root_cause_decision`。

真实 APY-002 还暴露了性能问题：约 509 秒中执行了约 16 次串行模型调用；工具与数据库操作通常
只需 0～0.1 秒。主要瓶颈是每轮 Evidence Evaluator、Sufficiency Gate，以及后续 Decision、
Validator、Recovery Planner 和 Report 的串行调用，而不是 Tool Calling 或 PostgreSQL。

本轮目标是：

- 用可审计 disposition 取代不稳定的 `open + confidence` 核心决策语义；
- 保持并加强“唯一活跃根因且无未解决竞争根因”的硬门禁；
- 将结构化事实判断改为确定性逻辑，只在真实语义歧义或高风险路径调用额外模型；
- 将正常路径模型调用压缩到 4～5 次、复杂路径不超过 8 次；
- 为节点、工具调用、恢复动作和 checkpoint 提供 PostgreSQL 最终幂等保障；
- 支持网络中断、Worker/容器重启后的安全续跑，不盲目重放副作用；
- 保持 v1 历史结果可读，并通过 10 个 Snapshot、4 个 Live 验证能力没有回退。

本轮不降低评分阈值、不读取 Ground Truth、不加入场景编号分支，也不把模型或网络波动伪装成
Agent 能力提升。

## 2. 复用评估

### 2.1 约束

- 后端使用 Python、Pydantic、LangGraph `>=1.2.8`、PostgreSQL 16、Redis 和 FastAPI；
- 当前已安装 `langgraph-checkpoint`，但图使用 `graph.compile()`，没有接入 checkpointer；
- 当前 `aiops_graph_checkpoints` 使用随机 UUID，只提供追加式审计快照，没有唯一执行语义；
- 不增加重量级依赖、外部服务、原生二进制或新的规则引擎；
- Redis 可以承担事件分发和未来争抢优化，但不能成为最终幂等真源。

### 2.2 GitHub 调研与选择

调研覆盖 `AIOps hypothesis adjudication`、`incident evidence state machine`、LangGraph reducer、
checkpoint saver 和 rules engine 等方向：

- `langchain-ai/langgraph`（MIT）：直接复用现有 StateGraph、reducer、checkpoint 协议和线程配置；
- `Tracer-Cloud/opensre`（Apache-2.0）：参考其证据溯源、结构化状态以及已验证/未验证主张分离，
  但它没有本项目所需的 `causally_inactive` 门禁或确定性 reducer；
- `jruizgit/rules`（MIT）：通用 durable rules engine，功能和依赖重量超过本轮所需，且无法直接
  表达项目特有证据门禁，因此不采用。

选择结果：直接复用现有 LangGraph 和 Pydantic；参考 OpenSRE 的证据溯源边界；项目内实现小型
强类型 Fact Adapter、Hypothesis Reducer、Validator Router 和 PostgreSQL checkpoint 适配器。
不增加第三方依赖。

## 3. 假设裁决领域模型

每个公开候选根因使用以下规范状态：

```text
HypothesisAssessment
├── hypothesis_id
├── disposition
│   ├── supported
│   ├── refuted
│   ├── causally_inactive
│   └── unresolved
├── evidence_ids[]
├── reason_code
├── assessment_source
│   ├── deterministic
│   └── llm_adjudicated
└── updated_at
```

语义如下：

- `supported`：公开证据正面支持该候选是本次事故的活跃根因；
- `refuted`：公开证据直接否定该机制或其必要条件；
- `causally_inactive`：该问题可能客观存在，但公开证据证明它没有参与本次事故；
- `unresolved`：证据不足、证据冲突或 Observation 无法解释，仍可能是活跃竞争根因。

`causally_inactive` 必须引用至少一条公开证据。不能仅因为另一个原因已经完整解释事故，就把
其他候选标记为不活跃；这种情况仍是 `unresolved`，从而避免遗漏并发根因。

新根因门禁为：

```text
恰好一个 supported
AND 不存在 unresolved 的活跃竞争根因
AND 每个 refuted / causally_inactive 都有公开 evidence_id
```

两个 `supported` 不允许通过浮点置信度强选一个；系统应生成复合根因或转人工复核。置信度可以
保留为展示和兼容信息，但不得参与上述安全门禁。

## 4. LangGraph 数据流

目标链路为：

```text
Planner (1 次 LLM)
→ Executor
→ Fact Adapter (确定性)
→ Hypothesis Reducer (确定性)
→ Sufficiency Gate (确定性)
   ├─ 仍有计划内证据：Executor
   ├─ 新组件/新机制：Replanner，最多 1 次 LLM
   ├─ 真实语义歧义：Adjudicator，最多 1 次 LLM
   └─ 证据充分：Decision，确定性组装
→ Conditional Validator
→ Recovery Planner
→ Policy Gate
→ Report
```

Fact Adapter 只解释公共工具 Schema、公共 Observation 和通用运维语义。它不得读取
`scenario_id`、Ground Truth、Oracle、Prompt 私有推理或评分标签。无法识别的新格式必须产生
`unknown / unsupported_observation_shape`，不能静默支持或反驳任何假设。

Reducer 以证据 ID 为输入进行幂等合并。高质量证据冲突时保持 `unresolved`；新的确定性证据
可以替代早期较弱判断，但必须保留全部状态转移审计。

Planner 仍负责候选假设和初始取证覆盖。工具结果暴露未规划的新组件或新异常机制时，允许最多
一次 Replanner，防止确定性链路因初始计划漏项而失去诊断能力。Report 只负责表达，不能反向
修改根因、证据、Validator 或恢复授权。

## 5. 条件式 LLM Validator

所有路径都执行 Deterministic Validator。只有以下任一确定性条件成立时，才额外调用一次独立
LLM Validator：

```text
任一关键 assessment_source == llm_adjudicated
OR 请求执行自动恢复
OR 恢复动作最高风险为 L2/L3
OR 结论是复合根因
OR 因果链跨组件
OR 存在高质量证据冲突
```

触发条件由代码计算，模型和报告文本不能改变。纯确定性闭环必须满足：恰好一个 `supported`、
不存在 `unresolved` 竞争根因、其他 disposition 均有公开证据、无高质量冲突，且恢复为
`no_action / proposal_only / manual_review`，此时 Deterministic Validator 通过即可。

LLM Validator 失败时，只有确定性检查全部通过才能保留诊断结论；恢复强制为
`manual_review`、`executionPermitted=false`，并记录安全错误子分类。确定性检查失败时不得使用
LLM 结论覆盖硬门禁。

## 6. 模型调用与超时预算

单个诊断预算为：

- Planner：1 次；
- Replanner：0～1 次；
- Adjudicator：0～1 次；
- LLM Validator：0～1 次；
- Recovery Planner：1 次；
- Report：1 次。

完全确定性的最短路径为 3 次；典型路径因一次 Replanner、Adjudicator 或 Validator 为 4～5
次；复杂路径为 6～7 次，硬上限为 8 次。达到上限后必须停止继续调查，输出证据不足及人工
复核建议，不能通过隐藏的无限重试继续消耗额度。

角色级超时：Planner、Replanner、Adjudicator 和 Validator 各 60 秒；Recovery Planner 和
Report 各 90 秒。网络类错误最多重试一次，Schema 格式修正最多一次，且重试仍计入调用预算。
整个诊断使用 5 分钟软时限和 8 分钟硬时限。已完成调用通过稳定执行键复用。

APY-002 的环境相关目标为 180～300 秒，APY-003 为 120～180 秒。供应商波动不作为 CI 硬失败
条件，但模型调用数、节点耗时、重试原因、缓存命中和最慢节点必须保存。

## 7. 三层幂等与 Checkpoint

### 7.1 节点执行幂等

```text
execution_key =
  task_id + graph_version + node_name + logical_iteration + input_fingerprint
```

输入 fingerprint 由规范化的公共状态生成，不包含时间戳、随机 ID、Secret 或模型私有推理。
相同输入的节点重试读取已完成结果，不重复调用模型，也不重复追加假设状态。

### 7.2 工具与恢复动作幂等

```text
tool_call_key =
  task_id + plan_step_id + tool_name + canonical_arguments_hash
```

只读工具重复执行时优先复用已完成结果；允许重试的失败记录新 attempt，但属于同一逻辑调用。
恢复动作额外使用稳定 `recovery_intent_id`，并保存目标、授权、执行前状态、执行后验证和回滚信息。
不可安全重放的动作如果结果未知，必须进入人工复核，不能自动补执行。

### 7.3 Checkpoint 与审计事件

保留两类语义不同的记录：

- `execution_checkpoint`：恢复执行的真源，同一 `execution_key` 唯一；
- `audit_event`：展示完整过程的追加式记录，使用稳定 `event_id` 去重，不参与状态恢复。

当前 `aiops_graph_checkpoints` 继续作为审计事件存储：旧 v1 行保持不可变，v2 行使用稳定
`event_id` 去重并标记图版本。v2 另增 PostgreSQL 执行记录，至少包含 `execution_key`、
`task_id`、`graph_version`、`node_name`、`logical_iteration`、`input_fingerprint`、`status`、
`output_payload`、`attempt_count` 和完成时间。状态为 `running / completed / failed / uncertain`。

PostgreSQL 唯一约束是最终正确性保障。竞争插入使用 `INSERT ... ON CONFLICT DO NOTHING`，未取得
执行权的 Worker读取已有记录：完成则复用；租约有效则等待；租约过期则领取新 attempt；状态
不确定且存在副作用则转人工复核。唯一冲突不得让后续读取所在事务进入失败状态。

本轮不加入 Redis 分布式锁。当前部署通常是单个 Uvicorn 进程和两个 asyncio 后台 Worker，
PostgreSQL 任务租约已负责正常领取竞争。未来多实例争抢显著时，可在 PostgreSQL 最终幂等之上
加入带 fencing token 的 Redis 短锁以减少重复计算，但不能用锁替代数据库事实。

## 8. 网络中断与安全续跑

“重跑”定义为从最后一个确定完成的 checkpoint 安全续跑，而不是重新执行整条 Agent 链路：

- 浏览器或 SSE 断开：后台任务继续，重连后从 PostgreSQL 回放缺失事件；
- Worker/容器重启：任务租约过期后由新 Worker领取，从最后完成节点继续；
- LLM 已返回且 checkpoint 已保存：复用结果，不再次调用；
- LLM 请求途中断网：新增失败或未知 attempt，可有限重试，但供应商无幂等查询时可能重复计费；
- 只读工具断网：使用相同 `tool_call_key` 重试或复用完成结果；
- 恢复动作途中断网：标记 `uncertain`，先探测真实系统状态，无法确认则人工复核；
- PostgreSQL 断开：当前节点不得标记完成，恢复后从上一个完成 checkpoint 继续。

只有相同 `task_id/run_id` 和相同 `graph_version` 才允许续跑；新 ID 是独立运行，不复用 checkpoint。

## 9. 失败降级

- Planner 失败：终止并记录 `planning_failed`，不生成伪结论；
- Fact Adapter 无法解释：标记 unknown，进入最多一次 Adjudicator；
- Adjudicator 失败：相关假设保持 unresolved，转人工复核；
- Deterministic Validator 失败：如果本次运行尚未使用 Replanner，则允许一次 Replanner；仍失败
  或 Replanner 预算已用完时安全终止；
- LLM Validator 失败：只允许确定性 grounded fallback，强制人工复核；
- Recovery Planner 失败：不执行恢复，生成确定性人工处置清单；
- Report 失败：生成模板报告，不影响已验证结论；
- checkpoint 保存失败：节点不得标记完成，副作用不得自动重放。

所有降级必须保存允许列表内的错误分类，不保存 Secret、原始 Prompt、模型私有推理、Ground Truth
或未经脱敏的原始云日志。

## 10. v1/v2 兼容

v2 使用 `graph_version = aiops-diagnostic-v2`，执行键包含版本。

- 已完成 v1 测评不可变，汇总时标记原图版本；
- 新运行全部使用 v2；
- 中断的 v1 不能从 v2 中间节点恢复，应标记 `migration_required` 并创建 v2 新执行；
- v1 证据可展示，但必须重新经过 v2 Fact Adapter 才能参与新决策；
- API 暂时保留旧 `hypothesisStates.status`，同时输出规范 `disposition`、`reasonCode`、
  `assessmentSource` 和证据 ID；
- 新 Agent、Validator 和评分器只能读取 `disposition`，旧 `status` 仅用于客户端兼容；
- `causally_inactive` 的兼容 `status` 可以投影为 `refuted`，但兼容值不得进入安全判断。

## 11. 测试与验收

### 11.1 单元与集成测试

- 覆盖四种 disposition、证据冲突、复合根因、未知 Observation 和状态幂等；
- 没有公开 evidence ID 时禁止产生 `refuted / causally_inactive`；
- 仅因另一原因能解释故障时，竞争根因仍为 unresolved；
- 覆盖全部六项 Validator 确定性触发条件及纯确定性跳过路径；
- 两个 Worker 使用相同执行键时只有一个调用模型；
- 参数顺序不同的等价工具调用生成相同调用键；
- 覆盖 checkpoint 完成后崩溃、保存前崩溃、租约接管和 PostgreSQL 唯一冲突；
- `uncertain` 副作用禁止重放，相同恢复意图并发提交最多生效一次；
- SSE 重连、数据库短断、Worker/容器重启可以安全续跑；
- v1 完成结果仍可读，v1 中断结果不能恢复进 v2；
- Ground Truth 隔离、路径穿越、嵌套 Oracle 字段和伪装场景 ID 继续被拒绝；
- 新日志格式和证据冲突不能被静默误判。

### 11.2 Benchmark 顺序

1. APY-003：验证 `causally_inactive` 能关闭非活跃端口竞争假设；
2. APY-002：验证调用数由约 16 次降至不超过 8 次；
3. 10 个 Snapshot 全部正式保存；
4. 4 个 Live 验证真实工具、Docker 故障与恢复策略；
5. 输出同模型、同配置、同知识库口径下的改造前后差分。

硬性验收：

- 10 个 Snapshot 和 4 个 Live 均产生可追溯结果，不丢失失败历史；
- required evidence 保持完整，不新增 `missing_root_cause_decision`；
- 不降低评分阈值，不修改 Ground Truth、评分权重或恢复授权；
- 单场景模型调用不超过 8 次；
- 同一逻辑工具调用、证据和恢复动作无重复；
- 所有 `causally_inactive` 均有公开证据；
- Validator 调用、跳过和降级原因可审计；
- APY-002/APY-003 根因结论不得回退。

## 12. 实施边界

本轮包含新假设模型、Fact Adapter、确定性 reducer/gate、条件式 Validator、LangGraph 路由压缩、
PostgreSQL 节点/工具/恢复幂等、v1/v2 兼容、网络续跑、观测指标和 14 场景差分验收。

本轮不包含 Redis 分布式锁、独立 Worker 服务、Kubernetes 多副本、新故障场景、新知识卡片、
评分阈值调整或高风险恢复权限放开。
