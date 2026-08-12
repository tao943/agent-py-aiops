## ADDED Requirements

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
