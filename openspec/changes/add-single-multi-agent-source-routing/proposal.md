## Why

当前 `evidence-driven-v4` 已具备可恢复的单 Agent 诊断闭环，但独立的 runtime 与 log
调查仍由同一个 Executor 串行执行。系统也不能以可解释、可审计的代码规则决定保持单 Agent、
进入确定性快路径，或升级为受控的数据源 Multi-Agent。

## What Changes

- 增加确定性的 `deterministic_fast_path`、`single_agent`、`multi_agent` Strategy Router。
- 将 Knowledge Retrieval 明确为只运行一次的 Knowledge Investigator；Multi v1 仅并行 Runtime 与 Log。
- 以代码拥有的只读能力描述符限制 Investigator 工具；未知 MCP 工具默认拒绝。
- 以结构化 EvidencePacket 和唯一 Aggregator 合并并行结果，禁止分支写共享裁决状态。
- 使用 `aiops-diagnostic-v3` 隔离新图 checkpoint，并复用现有 PostgreSQL 幂等执行真源。
- 只在内部 Benchmark CLI 支持 `auto|single|multi`，普通 API 不允许强制策略。
- 持久化定长安全 A/B 指标；未达到能力和性能门槛时不得默认启用 Multi-Agent。

## Capabilities

### Modified Capabilities

- `aiops-diagnosis-tasks`
- `agentpy-sre-benchmark`

## Impact

- 新增项目自有 Strategy Router、Investigator capability registry、EvidencePacket 和 Aggregator。
- 修改 LangGraph v4 拓扑、Artifact/terminal envelope 投影与 Live Benchmark CLI。
- 不新增第三方依赖、数据库表、Redis 锁、GitHub 集成或外部服务。
- 不改变 Ground Truth 隔离、评分阈值、Validator 标准或恢复授权边界。
