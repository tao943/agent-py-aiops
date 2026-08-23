## MODIFIED Requirements

### Requirement: User-owned data isolation

系统 SHALL 按 tenant 范围隔离知识库文档、文档索引任务、向量 chunks、聊天会话、聊天消息、工具调用审计、AIOps 诊断任务、证据、报告、图 checkpoints、Recovery Intent、Approval 和 Recovery Audit Event。

#### Scenario: Owner accesses recovery state
- **WHEN** 已认证 user 创建、读取、审批、拒绝、取消或订阅 Recovery Intent
- **THEN** Repository 和 API MUST 使用当前 user ID 限定 Incident、Diagnostic、Intent、Approval、Job 和 Event

#### Scenario: Cross-owner recovery access
- **WHEN** user 请求另一 owner 的 Intent 或事件
- **THEN** 系统 MUST 使用统一权限错误拒绝且不得泄露资源存在性、状态或目标

