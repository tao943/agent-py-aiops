# agent-configuration Specification

## Purpose
TBD - created by archiving change reframe-aiops-workbench. Update Purpose after archive.
## Requirements
### Requirement: Owner-scoped Agent resources

系统 SHALL 将 Prompt 和 Skill 保存为当前 owner 的 Agent Resource，并 SHALL 为每个 mutation 独立执行认证与 owner 授权。

#### Scenario: Owner manages a resource
- **WHEN** 已认证 owner 创建、修改、发布、弃用或绑定自己的资源
- **THEN** 服务端 MUST 校验 `capabilities.canManageConfiguration` 和资源 owner
- **AND** 响应 MUST 仅返回该 owner 的配置

#### Scenario: Cross-owner mutation is attempted
- **WHEN** user 使用另一 owner 的 resource、version 或 binding ID 发起 mutation
- **THEN** 服务端 MUST 统一拒绝且 MUST NOT 泄露资源是否存在

### Requirement: Draft, published and deprecated lifecycle

Agent Resource Version SHALL 遵循 `draft -> published -> deprecated` 生命周期。Draft 可编辑，published 内容不可变，deprecated 不得用于新 binding。

#### Scenario: Owner edits a draft
- **WHEN** owner 保存尚未发布的版本
- **THEN** 服务端 MUST 验证内容和元数据并更新该 draft

#### Scenario: Owner publishes a valid draft
- **WHEN** draft 通过验证且 owner 请求发布
- **THEN** 服务端 MUST 原子标记该版本为 `published` 并追加审计事件
- **AND** 后续内容修改 MUST 创建新 draft 而不是改写 published version

#### Scenario: Owner deprecates a published version
- **WHEN** owner 弃用 published version
- **THEN** 服务端 MUST 阻止新的 binding 使用该版本
- **AND** 已保存的历史运行快照 MUST 保持可审计

### Requirement: Published node bindings

系统 SHALL 仅允许把 published 且未 deprecated 的版本绑定到服务端允许的 Agent 节点，并 SHALL 对节点类型与资源类型进行校验。

#### Scenario: Owner binds configuration to a node
- **WHEN** owner 将 Prompt 或 Skill 版本绑定到 Chat 或 AIOps 的允许节点
- **THEN** 服务端 MUST 验证版本状态、节点 allowlist、资源类型和 owner 后原子保存 binding

#### Scenario: Invalid binding is requested
- **WHEN** binding 指向 draft、deprecated version、未知节点或类型不兼容资源
- **THEN** 服务端 MUST 使用稳定错误拒绝且 MUST NOT 改变当前有效 binding

### Requirement: Immutable runtime configuration snapshot

每次 Agent 运行 SHALL 保存其实际使用的不可变配置快照，包括版本标识、内容摘要、绑定节点、允许工具交集和强制安全策略标记。

#### Scenario: Bound configuration is assembled
- **WHEN** Chat 或 AIOps 节点开始一次运行
- **THEN** assembler MUST 先加入不可覆盖的 mandatory system prompt，再加入有界的不可信 published configuration text
- **AND** effective tools MUST 是 server allowlist 与配置请求的交集
- **AND** `policy_gate_required` MUST 为 `true`

#### Scenario: Configuration changes after a run
- **WHEN** owner 发布新版本或改变 binding
- **THEN** 已完成运行的 snapshot MUST 保持原版本和摘要，不得被追溯改写

### Requirement: Append-only configuration audit

系统 SHALL 为配置创建、验证、保存、发布、弃用和 binding 变更追加 owner-scoped 审计事件。

#### Scenario: Configuration mutation succeeds
- **WHEN** 任一资源或 binding mutation 提交成功
- **THEN** 服务端 MUST 追加包含 actor、resource/version、动作和时间的安全审计事件
- **AND** 事件 MUST NOT 包含凭据、完整 Skill 文件、隐藏系统提示或原始异常

### Requirement: Compatible migration of existing Chat configuration

系统 SHALL 将现有 owner-scoped Chat Prompt、Skill 和选中关系迁移为 resource/version/binding，且 SHALL 在迁移期间保留旧 API 作为新服务的兼容适配层。

#### Scenario: Existing configuration is migrated
- **WHEN** revision `202608230002` 应用于含旧 Chat 配置的数据
- **THEN** 每个资源 MUST 保持原 owner、可见名称和有效选择语义
- **AND** 重复运行 migration MUST NOT 创建重复 published version 或 binding

#### Scenario: Legacy endpoint is called during transition
- **WHEN** 旧 Chat 配置端点被兼容客户端调用
- **THEN** adapter MUST 读写同一新配置服务并执行相同 owner 和安全校验
