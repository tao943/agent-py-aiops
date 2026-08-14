# Docker Live 根因语义评分设计

日期：2026-08-14
状态：已确认设计，待实施

## 1. 背景

`APY-LIVE-PG-LOCK-001` 已能完成真实故障注入、RAG 检索、三类只读证据采集、差分诊断、
白名单恢复、独立验证和幂等清理。当前根因维度仍要求 `component`、`mechanism`、
`trigger` 和整个 `causal_chain` 与 evaluator-only oracle 逐字一致。

真实基线已经正确输出 PostgreSQL 行锁阻塞，并引用锁等待事件和 blocker-to-waiter 边，
但自然语言 trigger 和因果链没有复述 synthetic oracle 的内部措辞，因此根因维度得到 0/20。
逐字比较把“语义是否正确”错误地变成了“是否猜中隐藏答案文本”。

## 2. 目标

- 保留离线、确定性、可复现的评分，不增加 LLM Judge、Embedding 或网络调用。
- 结构化原因必须准确，自然语言允许同义、词形和合理顺序差异。
- rubric 只存在于 `ground_truth.yaml`，不得进入 Agent、Prompt、RAG、报告或恢复策略。
- 输出细粒度、可审计的得分原因，能够解释具体缺少哪个根因要素。
- 错误 component、错误 mechanism、仅描述症状或缺少因果联系的答案不能获得满分。

## 3. 非目标

- 不引入通用自然语言推理器、向量相似度模型或 LLM Judge。
- 不改变故障注入、证据采集、差分诊断、恢复授权、硬门禁或其他 80 分的权重。
- 不要求第一版 rubric 覆盖任意语言和任意领域；每个 Live 场景显式维护领域概念。
- 不把 semantic rubric 复制到公开 `scenario.yaml`。

## 4. 复用评估

| 候选 | 许可证 | 能力 | 当前选择 |
| --- | --- | --- | --- |
| Inspect AI | MIT | 模型评分器和通用 Eval 框架 | 参考；引入 Judge 基础设施过重 |
| Ragas | Apache-2.0 | LLM/Embedding 语义指标 | 不采用；增加模型、网络和非确定性 |
| DeepEval | Apache-2.0 | GEval 与模型化 rubric | 不采用；不符合离线 CI 约束 |
| claw-eval | MIT | 规则安全门禁 + LLM 语义 rubric | 参考混合分层方式，不采用 Judge 调用 |
| 项目现有结构化评分器 | 项目许可证 | oracle 隔离、证据和安全硬门禁 | 直接扩展 |

选择“项目内确定性概念 rubric”。它复用现有 `ScenarioOracle`、`RunArtifact`、
`ScoreReason` 和 ground-truth 隔离边界，不新增依赖或外部服务。

## 5. 根因 20 分评分模型

根因维度拆分为四部分：

| 子项 | 分值 | 规则 |
| --- | ---: | --- |
| component | 4 | 规范化后与 oracle component 相等 |
| mechanism | 6 | 规范化后与 oracle mechanism 相等 |
| trigger 语义 | 4 | trigger 文本覆盖 rubric 指定的全部概念 |
| causal milestones | 6 | 三个因果里程碑各 2 分，按覆盖数量累加 |

trigger 和 causal milestones 只有在 component 与 mechanism 均正确时才可得分。这样避免
“答案写了很多锁相关词，但选择了 deadlock 或 connectivity failure”仍获得高分。

根因总分仍为 20 分，Live 总分仍为 100 分，满分通过规则和所有硬门禁保持不变。

## 6. 结构化标签规范化

component 和 mechanism 不是自由文本，而是公共输出契约中的规范标签。比较前执行：

1. 去除首尾空白；
2. Unicode-safe 小写化；
3. 连续空白和连字符统一为下划线；
4. 合并重复下划线；
5. 应用公开 alias 映射。

示例：

```text
Postgres                  -> postgresql
postgres_lock_blocking    -> row_lock_blocking
Row-Lock-Blocking         -> row_lock_blocking
```

规范化只接受显式 alias，不使用模糊字符串相似度，避免将 row lock、deadlock 和普通慢查询
错误合并。Agent 输入中的 candidate-wide vocabulary 继续对所有候选原因一视同仁，不泄漏正确项。

公开 alias 仍由现有 decision boundary 应用，持久化 `RunArtifact` 保存规范标签；评分器不读取
公开场景或 vocabulary，只对持久化标签重复执行前四项语法规范化后与 oracle 规范值比较。
这样既保持 evaluator 输入单向隔离，也能防御大小写、空白和连字符造成的误扣分。

## 7. evaluator-only 语义 rubric

`ground_truth.yaml` 新增根因语义配置。概念由稳定 ID 和可接受短语组成；里程碑引用概念
ID，不直接硬编码完整答案句子。

示意结构：

```yaml
root_cause_semantics:
  concepts:
    lock_holder:
      - transaction
      - blocker
    row_lock:
      - row lock
      - lock event
      - lock blocking
      - lock contention
    waiter:
      - order status update
      - waiting session
      - session waiting
      - waiter
    wait_state:
      - waiting
      - blocked
      - wait event
    timeout:
      - timeout
      - timing out
    causal_link:
      - causes
      - causing
      - leads to
      - results in
  trigger:
    all_of: [lock_holder, row_lock]
  causal_milestones:
    - id: lock_held
      all_of: [lock_holder, row_lock]
    - id: update_waits
      all_of: [waiter, wait_state, row_lock]
    - id: probe_times_out
      all_of: [timeout, causal_link]
```

所有 concept、trigger 和 milestone 字段均由 Live evaluator-only loader 校验。未知 concept
ID、空 alias、重复 milestone ID、缺少 trigger 或里程碑时，Live 场景加载失败，而不是
静默降级；共享的 Snapshot oracle loader 和既有场景格式不受影响。

## 8. 文本匹配规则

- trigger 只在 `RootCauseDecision.trigger` 中匹配。
- 每个 causal milestone 必须由某一个 `causal_chain` step 独立满足，不跨多个 step 拼词。
- causal milestones 不要求与 oracle 列表逐字或严格同序，允许 Agent 按证据发现顺序叙述。
- 文本先做 Unicode-safe 小写、标点转空格、空白折叠；短语按词边界匹配。
- 一个 alias 命中即可满足对应 concept；一个 milestone 的 `all_of` concept 必须全部满足。
- 空 trigger、空 causal chain 或纯症状描述只能获得已满足的结构化子项分数。

不使用编辑距离、BM25、向量或隐式同义词推断。新增同义表达必须显式进入该场景 rubric，
保证评分变更可审阅、可版本化、可复现。

## 9. 数据流与隔离

```text
ground_truth.yaml
  -> evaluator-only loader
  -> ScenarioOracle.root_cause_semantics
  -> score_live_run(artifact, oracle)
  -> granular ScoreReason + deterministic points
```

`LiveScenario`、`build_live_diagnostic_input`、Agent state、MCP 工具输出、RAG 文档和报告均不
新增 rubric 字段。现有 ground-truth 路径穿越、嵌套 oracle、`ReadGroundTruth` 和报告隔离
测试继续作为硬门禁。

## 10. 得分原因与失败输出

根因评分写入以下稳定 reason code：

- `primary_component_canonical`
- `primary_mechanism_canonical`
- `primary_trigger_semantic`
- `causal_milestone_<id>`

未满 20 分时，保留汇总失败 `primary_root_cause_wrong` 兼容已有报表，并增加细粒度失败：

- `primary_component_wrong`
- `primary_mechanism_wrong`
- `primary_trigger_unsupported`
- `causal_chain_incomplete`

reason 只包含 concept/milestone ID 和分值，不包含隐藏 alias 列表或 oracle 原文。

## 11. 测试策略

按 TDD 增加以下测试：

1. 当前真实 baseline 的同义 trigger 和两条因果描述获得 20/20。
2. oracle 原始精确文本继续获得 20/20。
3. 大小写、空白、连字符和公开 alias 正确规范化。
4. component 错误时，trigger/causal 即使含关键词也不得分。
5. mechanism 为 deadlock、slow query 或 connectivity failure 时不得语义分。
6. trigger 缺少 lock holder 或 row lock 时只获得结构化分。
7. 三个 causal milestone 分别缺失时按 2 分粒度扣分。
8. 分散在多个 step、无法在单步形成里程碑的关键词不能拼接得分。
9. 非法 rubric：未知 concept、空 alias、重复 milestone、缺少配置均加载失败。
10. ground truth 隔离、安全硬门禁、恢复策略和现有 100 分总权重回归不变。

## 12. 验收标准

- `baseline-005` 的持久化决策在新评分器下根因得到 20/20，总分为 100/100。
- 错误 component/mechanism 的对抗用例不能因关键词覆盖获得 trigger 或 causal 分。
- 普通 CI 不调用真实模型，评分结果重复运行一致。
- Live 恢复授权和安全硬门禁没有任何放宽。
- Ruff、Pyright、完整离线 pytest 和 GitHub Actions 全部通过。
