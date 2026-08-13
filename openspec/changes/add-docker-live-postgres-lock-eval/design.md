## Context

现有 Snapshot runner 已提供答案隔离、生产诊断适配、结构化 artifact 与 PostgreSQL 审计。
Live 纵切必须复用这些边界，同时把故障注入器的 PID、DSN 和资源所有权与 Agent 隔离。

## Goals / Non-Goals

**Goals:** 隔离注入真实 PostgreSQL 行锁；采集双信号；安全终止 synthetic blocker；独立验证恢复；全路径清理；透明评分。

**Non-Goals:** Redis/Nginx 故障、Docker Socket、通用 chaos framework、CLS 依赖、自动处置非 synthetic 会话。

## Decisions

### 生命周期由单一 runner 所有

顺序固定为 preflight、baseline、inject、confirm、diagnose、recover、verify、evaluate、cleanup；cleanup 位于 `finally` 且可重复执行。

### Agent 只接收所有权无关的证据

collector 将真实会话映射为 blocker/waiter 角色，只公开 wait event、阻塞边和健康探针。PID、application name、SQL、DSN、注入记录和 oracle 不进入 Prompt、RAG、报告或工具描述。

### 恢复执行前重新授权

policy 重新检查数据库、当前 run application name、注入 PID、实时阻塞边以及 executor/waiter/system 排除项。任一条件不满足即拒绝，非 synthetic 目标只生成审批方案。

### 本地与 CLS 共享 collector 边界

第一版使用本地 PostgreSQL 系统视图。后续 CLS 只能实现相同的只读证据接口，不得改变答案隔离、恢复授权或评分合同。

## Risks / Trade-offs

- 单一确定性场景覆盖有限，但能优先验证恢复安全和真实闭环。
- PID 对诊断有价值却可能提示恢复目标，因此 Agent evidence 使用角色引用，executor 内部保留真实 PID。
- 新 init SQL 不会在既有 volume 重放，运维文档提供只创建缺失数据库的兼容命令。
