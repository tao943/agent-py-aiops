## MODIFIED Requirements

### Requirement: Persisted conversation workspace

前端 SHALL 在 `/assistant` 通过共享后端 API 提供 owner-scoped 中文运维助手，创建、列出、选择和删除当前 user 的持久会话，并 SHALL 通过旧 `/chat` 路由安全重定向。

#### Scenario: User opens the assistant workspace
- **WHEN** 已认证 user 打开 `/assistant` 或旧 `/chat`
- **THEN** 应用 MUST 在 AIOps Shell 中加载仅属于该 user 的会话，并聚焦运维问答与 Incident 上下文

### Requirement: Published agent configuration at runtime

运维助手 SHALL 只使用服务端已发布且绑定到 Chat 节点的 Prompt/Skill 版本快照，Chat 页面 SHALL NOT 提供常驻 Prompt/Skill 编辑侧栏。

#### Scenario: User starts a configured chat run
- **WHEN** owner 发送一条新消息
- **THEN** 服务端 MUST 解析当前已发布 binding、保存不可变运行快照并按安全拼装规则执行
- **AND** UI MUST 仅显示安全配置摘要而不是可编辑正文侧栏

#### Scenario: No published binding exists
- **WHEN** Chat 节点没有可用的 published binding
- **THEN** 服务端 MUST 使用强制系统安全配置运行或返回稳定配置错误
- **AND** MUST NOT 自动使用未发布 draft

### Requirement: Visible keyboard focus in chat

聊天界面的输入框和所有可聚焦控件 SHALL 使用 `:focus-visible` 提供清晰键盘焦点，同时避免对鼠标点击添加不必要焦点装饰。

#### Scenario: Keyboard focus moves across chat controls
- **WHEN** keyboard user 在输入框、按钮、链接和会话控件间移动焦点
- **THEN** 当前控件 MUST 显示高对比、不会被裁切的焦点指示

## REMOVED Requirements

### Requirement: Chat assembly controls

**Reason:** Prompt 与 Skill 已迁移到独立 Agent 配置中心，常驻 Chat 侧栏无法表达发布、绑定和审计生命周期。

**Migration:** 现有配置兼容迁移为 resource/version/binding；Chat 只消费已发布快照。

### Requirement: Neutral chat focus treatment

**Reason:** 隐藏所有 focus outline 违反键盘可访问性要求。

**Migration:** 使用仅对键盘导航可见的 `:focus-visible` ring。
