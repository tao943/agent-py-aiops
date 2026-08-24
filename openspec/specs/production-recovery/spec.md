# production-recovery Specification

## Purpose
TBD - created by archiving change add-production-recovery-execution. Update Purpose after archive.
## Requirements
### Requirement: Immutable server-derived Recovery Intent

系统 SHALL 仅从当前 owner 的持久 Incident、Diagnostic、Report、Evidence 和当前服务端白名单创建不可变 `RecoveryIntent`。客户端和模型输出 MUST NOT 指定最终 action、target、PID、Compose 路径、连接串、SQL 或执行权限。

#### Scenario: Client submits a diagnostic for recovery
- **WHEN** owner 使用 diagnostic ID 和可选说明创建 Recovery Intent
- **THEN** 服务端 MUST 从已验证 component、mechanism、required evidence facts 和唯一 selector 派生 action 与 target key
- **AND** 请求中的任何额外执行参数 MUST 被严格拒绝或忽略且不得进入 trusted facts

#### Scenario: Diagnosis has no unique selector
- **WHEN** 诊断事实缺失、selector 不匹配或多个 target 产生歧义
- **THEN** 系统 MUST NOT 创建可执行 Intent
- **AND** MUST 返回稳定的安全拒绝原因

### Requirement: Recovery is disabled and empty by default

系统 SHALL 默认配置 `productionRecovery.enabled=false`、空 Compose 白名单和空 PostgreSQL 白名单。

#### Scenario: Default project configuration is used
- **WHEN** 没有本地显式启用生产恢复和目标
- **THEN** 系统 MUST NOT 排队或执行任何恢复副作用
- **AND** MUST 将未执行原因公开为 `RECOVERY_DISABLED`

### Requirement: Governed recovery state machine

系统 SHALL 通过 PostgreSQL 原子状态转换管理 `proposed`、`awaiting_approval`、`queued`、`revalidating`、`executing`、`verifying` 和终态，非法转换 MUST 被稳定错误拒绝。

#### Scenario: Eligible Compose intent is created
- **WHEN** 全局开启、目标显式允许自动恢复且所有确定性创建门通过
- **THEN** Intent 与 Background Job 和首个审计事件 MUST 在同一事务中创建为 `queued`

#### Scenario: User cancels before execution claim
- **WHEN** owner 在执行 claim 前取消 queued Intent
- **THEN** Intent MUST 原子进入 `cancelled`
- **AND** Worker MUST NOT 调用执行器

#### Scenario: User cancels after execution claim
- **WHEN** Intent 已进入 `executing`
- **THEN** 取消 MUST 被拒绝为非法转换
- **AND** 系统 MUST 继续记录执行或未知结果

### Requirement: Owner-bound approval

PostgreSQL blocker 终止 SHALL 始终要求当前 Incident owner 的批准。批准 SHALL 绑定完整 Incident ID、Intent ID 和 proposal fingerprint，且有效期 MUST 为 600 秒。

#### Scenario: Owner approves current proposal
- **WHEN** owner 在有效期内提交完全匹配的 Incident ID confirmation
- **THEN** Approval、queued 转换、Background Job 和审计事件 MUST 在同一事务中持久化

#### Scenario: Approval is stale or mismatched
- **WHEN** approval 超过 600 秒或 proposal fingerprint/Incident 不匹配
- **THEN** Worker MUST NOT 执行副作用
- **AND** Intent MUST 进入安全终态或 `manual_intervention`

#### Scenario: Another owner attempts approval
- **WHEN** 已认证用户审批不属于自己的 Intent
- **THEN** API MUST 使用统一权限错误拒绝且不得泄露资源存在性

### Requirement: Fresh execution authorization

Worker SHALL 在副作用 claim 前重新读取 Incident、配置、target、Report、Evidence 和 Approval，并重新计算 proposal fingerprint。

#### Scenario: Incident self-resolves while queued
- **WHEN** Worker 重验时 Incident 已 resolved
- **THEN** Worker MUST 以零副作用终止或转人工

#### Scenario: Policy facts drift while awaiting approval
- **WHEN** 全局开关关闭、target 被移除、自动开关关闭、报告/证据变化或 approval 失效
- **THEN** Worker MUST NOT 获取副作用 claim
- **AND** MUST 追加不含敏感字段的拒绝审计事件

### Requirement: Side effects execute at most once

系统 SHALL 使用 PostgreSQL 唯一 execution key 和 side-effecting execution claim 协调恢复动作。执行开始后若结果无法确认，系统 MUST 转入 `manual_intervention`，并 MUST NOT 自动重放该动作。

#### Scenario: Worker loses contact after dispatch
- **WHEN** Worker 已进入 `executing` 且执行器返回超时或连接中断，无法证明动作未发生
- **THEN** Intent MUST 进入 `manual_intervention`
- **AND** 相同 execution key 的后续 Worker MUST NOT 再次调用执行器

#### Scenario: Worker crashes after completed execution persistence
- **WHEN** execution record 已 `completed` 但 Intent 尚处于 `executing`
- **THEN** 新 Worker MUST 复用已保存结果并从 `verifying` 继续
- **AND** MUST NOT 再次调用执行器

#### Scenario: Worker crashes during verification
- **WHEN** Intent 已进入 `verifying`
- **THEN** 新 Worker MAY 重放只读、幂等 verifier probes
- **AND** MUST NOT 重放副作用

### Requirement: Allowlisted Compose restart

Compose executor SHALL 仅对服务端解析的白名单文件和固定服务执行非 shell argv；自动执行 SHALL 同时要求全局开启和 target `automaticRecoveryEnabled=true`。

#### Scenario: Exact Compose target remains eligible
- **WHEN** fresh container identity 与 proposal 一致且所有执行门通过
- **THEN** executor MUST 仅调用 `docker compose -f <allowlisted-file> restart <allowlisted-service>`

#### Scenario: Compose command times out after start
- **WHEN** 子进程已启动但超时后无法确认结果
- **THEN** 系统 MUST 标记 unknown outcome 并转人工
- **AND** MUST NOT 自动第二次 restart

### Requirement: Approved PostgreSQL blocker termination

PostgreSQL executor SHALL 仅终止自身 fresh probe 在服务端固定 database identity 与 schema/relation 映射内确认的唯一 client blocker。Intent SHALL 只保存由逻辑资源和确定性事实生成的私有 relationship fingerprint；执行器 SHALL 拒绝 fingerprint 漂移、waiter、自身连接、后台进程、多个 blocker、身份变化或未识别关系。

#### Scenario: Fresh unique blocker matches approved proposal
- **WHEN** blocker/waiter/database/lock fingerprint 匹配且 approval 有效
- **THEN** executor MUST 使用参数化数据库调用终止该唯一 backend

#### Scenario: Blocker identity changed after approval
- **WHEN** fresh probe 与 proposal 的锁关系 fingerprint 不一致
- **THEN** executor MUST NOT 改选或终止另一 backend
- **AND** Intent MUST 转 `manual_intervention`

### Requirement: Independent multi-signal verification

系统 SHALL 在副作用后使用独立 verifier 判定恢复。Compose SHALL 检查容器身份变化、health、业务探针和 Incident resolved；PostgreSQL SHALL 检查 blocker 消失、waiter 推进或结束、锁等待恢复和 Incident resolved。

#### Scenario: Every required check passes
- **WHEN** 对应动作的所有 required checks 均在有界窗口内通过
- **THEN** Intent MUST 进入 `recovered`

#### Scenario: Any required check fails
- **WHEN** 任一 required check 失败或超时
- **THEN** Intent MUST 进入 `verification_failed`
- **AND** 系统 MUST NOT 自动再次执行或选择第二动作

### Requirement: Owner-scoped API and append-only safe audit

系统 SHALL 提供 owner-scoped Intent 创建、读取、审批、拒绝、取消和事件读取 API，并 SHALL 为每次状态转换追加可去重审计事件。公开投影 MUST NOT 包含凭据、DSN、SQL、PID、绝对路径、stdout/stderr、原始异常或完整工具输出。

#### Scenario: Client reconnects to events
- **WHEN** owner 使用 `afterSequence` 重新读取 Intent events
- **THEN** API MUST 返回该 Intent 后续持久事件且 sequence 单调递增

#### Scenario: Public event contains nested sensitive data
- **WHEN** 内部结果或异常包含禁止字段
- **THEN** serializer MUST 递归拒绝或删除禁止字段
- **AND** MUST 仅返回安全码和摘要

### Requirement: Chat compatibility without execution authority

现有 Chat 恢复请求确认后 SHALL 创建或复用同一个正式 Recovery Intent，但 Chat MUST NOT 批准或直接执行恢复。旧 request-only 审批记录 SHALL 保持只读和不可执行。

#### Scenario: User confirms recovery request in Chat
- **WHEN** owner 确认 pending Chat action
- **THEN** Chat job MUST 创建或复用正式 Intent
- **AND** MUST 返回其审批/排队状态而不是授予 execution permission

#### Scenario: Legacy approval request is displayed
- **WHEN** 系统读取旧 `aiops_recovery_approval_requests` 记录
- **THEN** 记录 MUST 标记为 legacy 且 `executionPermitted=false`
- **AND** MUST NOT 被转换为新 Intent 的有效批准

### Requirement: Alert-triggered automatic intent dispatch

系统 SHALL 仅为 Alertmanager webhook 持久化层创建、且成功完成的诊断自动派生正式 Recovery Intent。可信触发来源 MUST 由服务端覆盖写入，不能从 webhook payload、客户端、Prompt、Chat 或模型输出透传。

#### Scenario: Alertmanager diagnosis succeeds
- **WHEN** webhook ingestion 创建的诊断持久化为 `succeeded` 且恢复提案通过现有确定性策略门
- **THEN** durable diagnosis Job MUST 调用现有 `RecoveryIntentService` 创建或复用正式 Intent
- **AND** Compose 与 PostgreSQL MUST 分别保持现有自动排队和人工审批策略

#### Scenario: Untrusted payload claims Alertmanager origin
- **WHEN** 客户端或 webhook payload 提供任意 `triggerSource`
- **THEN** ingestion repository MUST 使用服务端值 `alertmanager` 覆盖 webhook 创建任务的来源
- **AND** 手动与 Chat 创建的诊断 MUST NOT 获得该来源标记

#### Scenario: Diagnosis Job retries after intent persistence
- **WHEN** Intent 已持久化但 diagnosis Job 尚未标记成功即崩溃
- **THEN** 重试 MUST 跳过 Agent、RAG、MCP 和 LLM
- **AND** MUST 复用同一 active Intent 与 recovery Job

#### Scenario: Cancellation arrives before dispatch
- **WHEN** 诊断已持久化成功但 Job 在 Intent 派生前收到取消请求
- **THEN** handler MUST NOT 创建 Recovery Intent 或 recovery Job
- **AND** MUST 保持取消语义

#### Scenario: Automatic dispatch emits an event
- **WHEN** 自动派生创建、复用或安全跳过
- **THEN** diagnosis Job MUST 追加仅包含公开 outcome、reason code、Intent ID 和 status 的持久事件
- **AND** 事件 MUST NOT 包含 trusted snapshot、凭据、DSN、PID、SQL、绝对路径、stdout/stderr 或原始异常
