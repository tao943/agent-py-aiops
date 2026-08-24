## Why

当前 AIOps LangGraph 能持久化证据、根因、恢复提案和安全门，但正式链路只创建不可执行的审批请求；真正副作用只存在于掌握 scenario/oracle 的隔离 Live Eval Runner。项目需要一个不读取 benchmark 答案、默认关闭、可审批、最多执行一次且能独立验证的生产恢复控制面。

## What Changes

- 从 owner-scoped 持久诊断事实创建不可变 `RecoveryIntent`，客户端不能指定动作、目标或执行参数。
- 增加 PostgreSQL 恢复状态机、审批记录、append-only 审计和原子 Intent/Job/Event 写入。
- 复用 durable Background Job 与 `ExecutionCoordinator`，未知副作用结果转人工且不自动重放。
- 第一版仅支持白名单 Compose 服务重启与 PostgreSQL blocker 终止。
- Compose 自动恢复要求全局和目标显式开启；PostgreSQL 终止始终要求 Incident owner 在 600 秒内审批。
- 执行前重新读取 Incident、配置、报告、证据和审批；任何漂移都在副作用前拒绝。
- 恢复成功要求动作后置条件、健康/业务或锁关系检查以及 Alertmanager resolved。
- 将 Chat 的“创建恢复审批”兼容入口接到正式 Intent，但 Chat 不能审批或执行。

## Capabilities

### Added Capabilities

- `production-recovery`

### Modified Capabilities

- `background-job-runtime`
- `authorization-and-tenant-isolation`
- `stream-rag-chat`

## Impact

- 新增恢复领域模块、owner-scoped API、PostgreSQL 表与 Alembic revision `202608230001`。
- 扩展共享 TypeScript API 契约、错误目录、项目配置模板和运维文档。
- 不新增依赖、Redis 锁、外部工作流服务、任意脚本执行器或 Agent 写工具。
- 不改变诊断 LangGraph 主链；模型自由文本不能选择生产执行器。

