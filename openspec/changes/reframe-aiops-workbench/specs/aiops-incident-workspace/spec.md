## ADDED Requirements

### Requirement: Owner-scoped Incident queue

系统 SHALL 将当前 owner 的告警、诊断和恢复闭环投影为 Incident 列表，并 SHALL 使用稳定的 opaque cursor 分页。

#### Scenario: Owner lists incidents
- **WHEN** 已认证 owner 请求 Incident 列表
- **THEN** API MUST 仅返回该 owner 的 Incident，并按 `(updatedAt DESC, id ASC)` 稳定排序
- **AND** 响应 MUST 返回 `nextCursor`，客户端 MUST NOT 构造或解释 cursor

#### Scenario: Equal timestamps cross a page boundary
- **WHEN** 多个 Incident 的 `updatedAt` 相同且列表按较小 limit 分页
- **THEN** 后续页面 MUST NOT 重复或遗漏 Incident

#### Scenario: Another owner requests an incident
- **WHEN** user 请求不属于自己的 Incident
- **THEN** API MUST 统一拒绝且 MUST NOT 泄露 Incident、Diagnostic 或 Recovery Intent 是否存在

### Requirement: Auditable incident detail

调查工作台 SHALL 展示安全的告警摘要、诊断状态、公开证据链、根因结论、引用、恢复提案和验证里程碑，但 SHALL NOT 展示隐藏推理或敏感执行数据。

#### Scenario: Diagnosis evidence is available
- **WHEN** Incident 已收集诊断证据和报告
- **THEN** detail API MUST 返回公开 fact、来源工具名称、观察摘要、评估结果、引用和时间顺序
- **AND** UI MUST 将事实、假设裁决和最终结论明确区分

#### Scenario: Internal data contains sensitive fields
- **WHEN** 内部状态含凭据、DSN、SQL、PID、绝对路径、stdout/stderr、原始异常、完整工具输出或 checkpoint state
- **THEN** public serializer MUST 删除或拒绝这些字段
- **AND** UI MUST 仅显示稳定安全码和摘要

### Requirement: Formal Recovery Intent projection

每个 Incident SHALL 最多投影一个当前正式 Recovery Intent；选择历史记录时 SHALL 确定性使用最新正式记录，并 SHALL 排除 legacy Chat request 和 Live Eval execution。

#### Scenario: Incident has multiple historical intents
- **WHEN** 同一 Diagnostic Task 存在多个历史正式 Intent
- **THEN** API MUST 确定性选择最新记录并返回一次 `recoveryIntentId`、`recoveryExecutionStatus` 和 `productionRecoveryExecution=true`

#### Scenario: Incident has only non-production recovery records
- **WHEN** 只有 legacy Chat approval rows 或 Live Eval execution keys
- **THEN** Incident MUST NOT 将它们投影为正式生产 Recovery Intent

### Requirement: Bounded recovery status refresh

前端 SHALL 独立于诊断 SSE 刷新非终态正式 Recovery Intent，并 SHALL 保留最后成功状态而不伪造恢复结果。

#### Scenario: Recovery remains active on a visible page
- **WHEN** Intent 为 `queued`、`revalidating`、`executing` 或 `verifying` 且页面可见
- **THEN** Recovery Store MUST 每 2 秒有界读取 Intent，并使用最后 durable `afterSequence` 获取新增事件

#### Scenario: Page visibility or terminal state changes
- **WHEN** 页面隐藏、重新可见或 Intent 进入终态
- **THEN** Store MUST 在隐藏时暂停、可见时立即刷新，并在所有终态或 view disposal 时停止轮询

#### Scenario: Recovery refresh fails
- **WHEN** 一次刷新返回错误或超时
- **THEN** Store MUST 保留最后成功状态、标记 `stale=true` 并提供 retry
- **AND** MUST NOT 将失败转换为 `recovered`

### Requirement: Governed recovery controls

调查工作台 SHALL 从服务端 capability 和正式 Intent 状态决定可见动作，且 SHALL NOT 向前端提供通用执行能力。

#### Scenario: Compose recovery is automatic
- **WHEN** Compose Intent 已自动排队或执行
- **THEN** UI MUST 仅展示状态、公开事件和验证结果
- **AND** MUST NOT 显示“执行恢复”按钮

#### Scenario: PostgreSQL recovery awaits approval
- **WHEN** 正式 Intent 为 `awaiting_approval` 且服务端允许当前 owner 审批
- **THEN** UI MAY 显示绑定 Incident ID 的批准、拒绝或取消动作
- **AND** 所有 mutation MUST 由服务端再次校验 owner、状态、fingerprint 和有效期
