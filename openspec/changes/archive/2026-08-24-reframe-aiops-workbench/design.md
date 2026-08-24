## Context

系统已经具备 Alertmanager ingestion、durable diagnosis Job、LangGraph 诊断、证据/报告持久化、正式 Recovery Intent、受控执行和独立验证，但前端仍按能力模块分散展示，用户无法从一个 Incident 看到完整闭环。现有 Prompt/Skill 能被 Chat 使用，却没有可发布配置的生命周期、节点绑定、不可变运行快照与审计。

## Goals / Non-Goals

**Goals:** 事件优先导航；owner-scoped Incident 读模型；证据可审计调查页；正式 Recovery Intent 状态刷新和人工审批；可发布 Agent 配置；兼容迁移；专业、紧凑、响应式且可访问的 Vue 工作台。

**Non-Goals:** 替换 LangGraph、引入通用 RBAC、让前端直接执行 Compose、开放 PostgreSQL 自动终止、展示隐藏 reasoning/checkpoint/原始工具输出、增加第二套前端框架或状态管理。

## Decisions

### Incident is a server-owned projection

Incident API 从当前 owner 的告警、诊断任务、公开证据链、报告和正式 Recovery Intent 生成读模型。客户端不拼接跨资源状态，也不通过文案推断恢复成功。列表按 `(updatedAt DESC, id ASC)` 稳定排序，并使用服务端生成的 opaque cursor。

### Formal Recovery Intent remains the control-plane truth

调查页只读取 production recovery API。`queued/revalidating/executing/verifying` 等非终态每 2 秒有界轮询，页面隐藏时暂停、重新可见时立即刷新，终态停止。刷新失败保留最后成功投影并标记 stale。Compose 自动恢复只显示进度；PostgreSQL 只有服务端返回可审批能力时显示批准/拒绝动作。

### Configuration is versioned and bounded

Prompt 与 Skill 统一为 owner-scoped Agent Resource；可编辑 draft，published version 不可变，deprecated 不再用于新绑定。Binding 把已发布版本关联到允许的 Agent 节点。每次运行保存版本、内容摘要、允许工具交集和安全策略的不可变快照；运行时先拼装强制系统安全提示，再加入有界的不可信用户配置文本。

### Authorization is enforced per mutation

第一版本地个人 workspace 中，每个已认证 owner 是自己资源的管理员，服务端通过 `capabilities.canManageConfiguration` 投影能力。隐藏按钮只是表现层，每个 create/update/publish/deprecate/bind mutation 都独立校验认证 owner，跨 owner 统一拒绝且不泄露资源存在性。

### Existing Vue stack is retained

继续使用 Vue 3、TypeScript strict、Pinia、Vue Router、Lucide 和原生 CSS token/primitives。外部 UI 项目只提供 IA 与无障碍参考，不复制受限代码、不增加 PrimeVue/Element Plus/Naive UI。

### Safety-sensitive details stay summarized

UI 可展示 evidence facts、公开工具名称、状态、引用和安全摘要；不得展示隐藏模型 reasoning、凭据、DSN、SQL、PID、Compose 绝对路径、stdout/stderr、原始异常、完整工具输出或 checkpoint state。

## Risks / Trade-offs

- Incident 投影增加后端查询复杂度；通过聚焦 repository、稳定分页和 owner-scoped API 测试控制风险。
- 2 秒轮询会增加请求量；只对可见页面的非终态 Intent 启用，并在终态/卸载时停止。
- 兼容迁移会暂时保留两套 API 表面；旧端点仅做适配，不成为第二事实来源。
- 轻量设计系统需要维护 primitives，但避免重量依赖和现有样式体系冲突。

## Migration Plan

1. 先增加共享契约、Incident 读模型与新 Shell，保留旧路由重定向。
2. 新增 revision `202608230002`，把现有 Prompt/Skill 转为 owner-scoped resource/version/binding，保留原始 ID 映射。
3. Chat 改为读取已发布配置快照；迁移期间旧端点委托新服务。
4. 完成聚焦契约、后端、前端、权限和响应式验收后，再移除常驻配置侧栏与旧导航。

## Open Questions

无。第一版本地 workspace 的管理员边界、恢复权限与轮询策略已锁定；更细 RBAC 和 WebSocket 推送留待后续独立变更。
