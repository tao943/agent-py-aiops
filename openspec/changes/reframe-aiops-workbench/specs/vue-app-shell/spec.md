## MODIFIED Requirements

### Requirement: Event-first authenticated workspace

认证后的应用 SHALL 将 `/incidents` 作为默认首页，并 SHALL 提供事件中心、调查工作台、运维助手、知识中心、Agent 配置、集成中心和系统状态入口。

#### Scenario: Authenticated user opens root route
- **WHEN** 已认证用户访问 `/`
- **THEN** router MUST 导航到 `/incidents`，并在共享 Shell 中显示 owner-scoped Incident 队列

#### Scenario: User navigates between workspaces
- **WHEN** user 使用桌面侧栏或窄屏导航
- **THEN** 应用 MUST 提供 `/incidents`、`/assistant`、`/knowledge`、`/agent-config`、`/integrations` 和 `/system` 入口
- **AND** `/incidents/:incidentId` MUST 在同一 Shell 中显示调查工作台

#### Scenario: User opens a legacy route
- **WHEN** 已认证 user 访问旧 `/chat`、`/aiops` 或 `/mcp` 路由
- **THEN** router MUST 分别安全重定向到 `/assistant`、`/incidents` 或 `/integrations`

### Requirement: Accessible responsive workspace shell

应用 Shell SHALL 提供高密度、响应式、可通过键盘操作的中文工作区，并 SHALL 尊重用户的 motion 偏好。

#### Scenario: Keyboard user navigates controls
- **WHEN** user 使用键盘在链接、按钮和表单控件间移动
- **THEN** 每个可交互控件 MUST 使用清晰的 `:focus-visible` 指示
- **AND** 焦点样式 MUST NOT 仅依赖颜色变化

#### Scenario: User opens a narrow viewport
- **WHEN** viewport 宽度为 375px 或其他窄屏尺寸
- **THEN** 主导航和路由内容 MUST 保持可读、可操作且没有水平溢出
- **AND** 关键操作触控目标 MUST 至少为 44px

#### Scenario: User prefers reduced motion
- **WHEN** 浏览器设置 `prefers-reduced-motion: reduce`
- **THEN** Shell MUST 禁用非必要位移和循环动画，并保持状态变化可理解

## REMOVED Requirements

### Requirement: Chat sessions live in the workspace sidebar

**Reason:** 全局导航改为稳定的 AIOps 一级能力，Chat 会话历史属于运维助手工作区，不能挤占所有路由的全局侧栏。

**Migration:** 运维助手在 `/assistant` 内提供会话切换；现有 Chat store 仍是会话状态来源。
