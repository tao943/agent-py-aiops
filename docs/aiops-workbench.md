# AIOps 工作台

当前前端是事件优先的运维控制台，而不是通用聊天页面。认证后的根路由进入 `/incidents`，所有查询和变更均使用当前用户的 owner scope。

## 工作区

- `/incidents`：事件队列、严重级别、诊断状态、审批与正式 Recovery Intent 投影。
- `/incidents/:incidentId`：证据时间线、假设与反证、工具审计、诊断报告和受控恢复闭环。
- `/assistant`：持久 Chat Run、结构化记忆、知识引用、工具活动和待确认动作。Prompt 与 Skill 不在对话页内编辑。
- `/knowledge`：知识卡、SOP、复盘和排查文档的上传、分块、索引与检索追踪。
- `/agent-config`：`draft -> published -> deprecated` 版本生命周期、节点绑定与审计。当前只允许用户资源绑定 `conversation`；Planner、Specialist、Adjudicator、Validator、Recovery Planner 和 Report 使用服务端受控 Prompt。
- `/integrations`：Alertmanager、CLS/MCP、模型、通知和数据基础设施入口。
- `/system`：分别展示进程存活、依赖就绪、配置有效性、owner-scoped 后台任务和已持久化 Eval 摘要。

## Agent 配置运行语义

发布版本不可原地修改；编辑已发布版本会创建新草稿。“回滚”通过重新绑定历史 published version 完成。新 Chat Run 在创建时保存精确的 Prompt/Skill 版本快照、内容摘要与有效工具交集，Worker 重试继续使用原快照，不跟随后续绑定变化。

用户 Prompt 和 Skill 作为不受信任的已发布配置加载在强制系统安全提示之后，不能扩大服务端工具 allowlist，也不能绕过 Validator、Policy Gate 或恢复审批。核心 AIOps 节点仍由仓库内版本化 Prompt 和 LangGraph 状态机控制。

## 恢复控制

页面只把正式 `ProductionRecoveryIntent` 视为可执行恢复对象。legacy Chat approval、Live Eval execution key 或模型文本不能冒充生产恢复。自动恢复必须通过当前证据重校验和 Policy Gate；高风险、证据不足、状态不确定或验证失败时进入人工复核。

## 状态与安全

前端明确区分 loading、empty、error、partial、stale、permission denied 和 degraded。`/health` 只代表 API 进程在线，`/ready` 才代表运行依赖；Redis 不可用会被标为非阻塞降级，但不会被误报为完全健康。

界面不渲染 token、DSN、SQL、PID、文件路径、原始异常、完整工具输出、checkpoint state 或模型隐藏 reasoning。桌面与 390px 窄屏共享同一信息架构，并保留键盘焦点、44px 触控目标和 reduced-motion 支持。
