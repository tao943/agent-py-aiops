## Why

当前前端仍以通用 Chat、知识库、智能诊断和 MCP 页面组织能力，告警、诊断、证据、正式 Recovery Intent 与验证结果没有投影成同一个可追溯事件工作流；Prompt/Skill 也以 Chat 常驻侧栏配置，缺少发布、绑定、运行快照和审计边界。需要将产品重构为事件优先的 AIOps 工作台，并让 Agent 配置成为受服务端权限和安全策略约束的正式能力。

## What Changes

- 认证后默认进入 `/incidents`，提供事件中心、调查工作台、运维助手、知识中心、Agent 配置、集成中心和系统状态。
- 新增 owner-scoped Incident 投影 API，把告警、诊断、证据链、报告、正式 Recovery Intent 和验证状态聚合为稳定、可分页的读模型。
- 调查工作台持续刷新非终态 Recovery Intent，并只展示服务端正式状态；Compose 自动恢复不提供前端执行按钮，PostgreSQL 恢复仍要求 owner 审批。
- 将 Chat 的 Prompt/Skill 常驻侧栏迁移为独立 Agent 配置中心，支持资源、不可变版本、发布/弃用、节点绑定、运行快照和追加式审计。
- Chat 路由迁移为 `/assistant`，运行时只加载已发布且已绑定的配置快照；用户配置不能扩大工具白名单或绕过 Validator、Policy Gate 和恢复审批。
- 恢复键盘可见焦点、响应式导航、加载/空/错误/陈旧状态，并保持 typed API/SSE client 边界。

## Capabilities

### Added Capabilities

- `aiops-incident-workspace`
- `agent-configuration`

### Modified Capabilities

- `vue-app-shell`
- `chat-experience`

## Impact

- 扩展共享 TypeScript 契约、FastAPI owner-scoped 读模型、PostgreSQL Agent 配置模型和 Alembic revision `202608230002`。
- 重构 Vue Router、应用 Shell、Pinia stores 和主要工作区页面，但不迁移 React、不引入重量 UI 框架。
- 兼容迁移现有 Chat Prompt/Skill；旧端点在变更期间作为适配层保留，不能静默丢失数据。
- 不改变诊断 LangGraph、正式恢复控制面或工具权限边界，不向 Chat 或前端暴露生产写工具。
