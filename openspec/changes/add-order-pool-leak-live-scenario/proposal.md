## Why

现有 Live 场景主要依靠单一 Runtime 数据源即可得到完整根因，不能公平验证 Runtime 与 Log
协作时 Multi-Agent 是否产生真实能力增益。项目需要一个可重复、答案隔离且具有真实服务数据路径的
跨源场景，而不是通过隐藏 Single 工具或合成答案日志制造差异。

## What Changes

- 在隔离 Compose profile 中增加使用真实 asyncpg pool 的 `order-api`。
- 用 run-scoped 异常订单更新路径稳定耗尽连接池，并保留 PostgreSQL 可达和无锁等待反证。
- 从 order-api 实际连接生命周期事件生成 CLS records，不使用 evaluator 故障答案模板。
- 为 Single 与 Multi 暴露相同工具、可信参数、模型、知识库、全局预算和评分器。
- 只允许 Runtime/Log Investigator 的串行或并行调度不同，并共享同一个并发安全预算。
- 在隔离环境提供 scoped、幂等的 order-api restart；生产语义仅生成需人工审批的提案。
- 延续 terminal envelope、PostgreSQL 结果、Archive checksum、checkpoint、答案隔离和安全 hard gates。

## Capabilities

### Modified Capabilities

- `agentpy-sre-benchmark`
- `aiops-diagnosis-tasks`

## Impact

- 新增一个隔离 FastAPI 服务、Live Driver、Runtime MCP client 和场景级 CLS record provider。
- 修改 Compose、Live registry、CLI、评分与 Investigation Router。
- 不新增项目依赖、外部运行时、数据库产品或生产写工具。
- 不改变现有评分阈值、Validator、Ground Truth 隔离或普通 API 的 `auto` 路由策略。
