## ADDED Requirements

### Requirement: Chat recovery requests use the formal Intent boundary

Chat Agent SHALL 只能预览并在用户确认后创建或复用 owner-scoped Recovery Intent。Chat tool allowlist MUST NOT 暴露审批、Compose restart、PostgreSQL termination 或任意生产写执行器。

#### Scenario: Confirmed Chat recovery request
- **WHEN** owner 确认 `create_recovery_approval` pending action
- **THEN** Background Job MUST 调用正式 Recovery Intent service
- **AND** Chat MUST 仅展示 Intent 当前状态和后续人工操作入口

#### Scenario: Chat attempts to approve or execute
- **WHEN** 模型、Prompt 或 Skill 尝试调用审批或生产恢复执行能力
- **THEN** Tool policy MUST 在调用前拒绝
- **AND** MUST NOT 创建 execution claim 或副作用

