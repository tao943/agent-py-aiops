## Context

正式诊断结果包含持久化 Decision、Evidence、Report、Recovery Plan、Validator 和 Policy Gate，但现有 `aiops_recovery_approval_requests` 被约束为 `pending` 且 `execution_permitted=false`。Live Eval 的恢复代码依赖 run/scenario、注入 PID 和 oracle，只能参考安全边界，不能直接作为生产授权来源。

## Goals / Non-Goals

**Goals:** 不可变 Intent；owner 审批；服务端白名单；执行前 fresh revalidation；PostgreSQL 幂等真相；副作用最多一次；Worker 可恢复；独立验证；安全公开审计。

**Non-Goals:** Kubernetes、SSH、云厂商写操作、任意 MCP 写工具、通用脚本、双人审批、RBAC/排班、自动回滚、自动尝试第二动作。

## Decisions

### Separate diagnostic and recovery control planes

LangGraph 只产生证据和建议，不持有生产写工具。`RecoveryIntentService` 从当前 owner 的持久 Incident、Diagnostic、Report 和 Evidence 派生 Intent；Background Job 在诊断图之外运行，但前端把两者投影为同一 Incident 时间线。

### Deterministic proposal routing

执行动作不解析 LLM 的 action/target 自由文本。服务端配置使用唯一 `(component, mechanism)` selector 和 required evidence fact keys，将已通过确定性验证的诊断映射到固定 action/target key；没有唯一匹配即不可执行。

### PostgreSQL state machine and unit of work

Intent、Approval、Audit Event 和 Background Job 使用 PostgreSQL。`create_intent_with_job_and_event` 与 `approve_with_job_and_event` 各自在同一数据库事务完成，避免 queued-without-job 或 approval-without-event。

### At-most-once side effects

副作用通过现有 `ExecutionCoordinator` 以 `execution_kind=recovery`、`side_effecting=true` 和稳定 execution key claim。执行开始后若结果未知，状态转人工；Background Job 不自动重放。若 execution 已 `completed` 而 Intent 尚未进入验证，Worker 复用结果并只继续 verifier。

### Fresh execution authorization

Worker 在 claim 前重新读取 Incident、当前配置、报告、证据和审批并重新计算 fingerprint。Incident 已 resolved、配置关闭、白名单/自动开关变化、报告或证据漂移、审批过期或不匹配都必须零副作用拒绝。

### Two bounded executors

Compose 只执行固定 argv `docker compose -f <resolved allowlisted file> restart <allowlisted service>`，不使用 shell。PostgreSQL target 还必须把诊断中的逻辑锁资源映射到固定 schema/relation，并绑定 named database identity；Intent 仅保存不可逆 relationship fingerprint。执行器只终止 fresh probe 在该物理边界内唯一确认且与指纹匹配的 client blocker，使用参数化调用；PID 不来自模型、前端或旧证据。

### Independent verification

Compose 要求容器身份变化、health、业务探针和 Incident resolved；PostgreSQL 要求 blocker 消失、waiter 推进/结束、锁等待恢复和 Incident resolved。失败不会触发第二次副作用。

### Chat compatibility

现有 Chat 工具名称和确认 UX 保留，但确认后创建/复用正式 Intent。Chat 无权批准或执行。旧 request-only 记录只读且始终不可执行，不迁移为审批权限。

### Alert-triggered dispatch remains in the durable diagnosis job

Alertmanager ingestion 在创建 Diagnostic Task 的同一事务内强制写入服务端来源标记。现有 `aiops_diagnosis` Job 在诊断成功并持久化 Report/Evidence 后调用聚焦 Dispatcher，Dispatcher 只检查可信来源和任务终态，所有提案、策略和 Intent 幂等仍由 `RecoveryIntentService` 决定。派生不进入 LangGraph，因此模型和 Agent 不获得生产恢复权限。

Job 在派生前再次检查取消。若 Intent 已创建后 Job 崩溃，重试检测到 Task 已 `succeeded` 后跳过整个 Agent/LLM 链，仅补偿调用 Dispatcher；PostgreSQL 唯一约束收敛为同一 active Intent。Task 缺失、持久层错误或未分类异常使 Job 安全失败或重试，不能伪装为正常跳过。

## Risks / Trade-offs

- 状态机和执行器增加实现复杂度，但保持在现有 PostgreSQL/Job/Execution 基础之内，避免第二工作流引擎。
- Compose 重启是缓解，不是永久修复；验证成功仍需审计记录该限制。
- Alertmanager resolved 可能晚于服务恢复，因此 verifier 使用有界窗口；超时进入 verification failed，不重复动作。
- PostgreSQL 会话终止不可撤销，因此始终人工审批且只允许唯一 fresh blocker。
