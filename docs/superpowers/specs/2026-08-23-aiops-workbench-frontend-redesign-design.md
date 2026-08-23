# AIOps 运维工作台前端重构设计

**日期：** 2026-08-23  
**状态：** 已确认，待实施计划  
**技术路线：** Vue 3 + TypeScript + Pinia + Vue Router + Lucide Vue + 项目内轻量设计系统

## 1. 背景

当前前端已经覆盖认证、对话、知识库、AIOps 与 MCP 管理，但产品信息架构和视觉表达仍以
“通用 AI 聊天工具”为中心。Prompt、Skill 等配置占据 Chat 主界面，却没有形成可发布、
可绑定、可审计的真实运行能力；AIOps 页也没有清楚表达从告警到恢复验证的完整闭环。

本次重构将产品重新定位为**事件驱动、本地优先、证据可审计的 AIOps 运维工作台**，以
Incident 生命周期为主线呈现现有 Agent、RAG、MCP、CLS、恢复审批与 Eval 能力，并补齐
真实的 Prompt/Skill 配置闭环。

## 2. 设计目标

- 默认从事件中心进入告警处理，而不是从空白聊天页开始；
- 清楚呈现“告警 → 事件 → 调查 → 根因 → 恢复 → 验证”的安全闭环；
- 让 Single/Multi-Agent 路由、Specialist、证据、假设、Validator 和 checkpoint 可审计；
- 将对话 Agent 保持为自然语言入口，不复制 AIOps 诊断与安全决策；
- 把 Prompt 与 Skill 建成真实的版本化运行配置，而非装饰性编辑器；
- 统一知识、外部集成、系统 readiness 与 Eval 状态的产品表达；
- 建立可复用、可访问、响应式的轻量 Vue 设计系统；
- 复用现有 API、Store、SSE 与业务能力，不以伪数据掩盖后端缺口。

## 3. 非目标

- 不迁移到 React；
- 不引入完整 UI 框架并重写现有业务组件；
- 不展示模型隐藏思维链或原始敏感工具输出；
- 不允许 Chat、Prompt 或 Skill 绕过 Validator、Policy Gate 与恢复审批；
- 不把系统状态页做成与业务脱节的监控大屏；
- 不在本次重构中创建第二套前端、API client 或状态管理体系。

## 4. Reuse-first 评估

### 4.1 项目内复用

继续直接采用现有 Vue 3、Pinia、Vue Router、Lucide Vue、typed API/SSE client、认证状态、
知识库 Store、AIOps Store 和共享契约。页面不能直接拼接后端协议，新增契约仍从
`packages/api-contracts` 向后端与前端同步。

### 4.2 GitHub 调研

| 候选 | 许可证与适配性 | 决策 |
|---|---|---|
| `primefaces/primevue` | MIT；Timeline、Drawer、DataTable 完整，但整包接入较重 | 仅参考无障碍组件 API |
| `element-plus/element-plus` | MIT；Vue 3 成熟，但视觉容易退化为通用后台模板 | 不采用 |
| `tusen-ai/naive-ui` | MIT；主题能力完善，但会引入整套组件与迁移成本 | 不采用 |
| `grafana/oncall` | AGPL-3.0；Incident 详情与时间线具有参考价值 | 仅参考信息架构，不复制代码 |
| `langfuse/langfuse` | 主体 MIT；Trace 树、时间排序与详情布局成熟 | 参考 Agent 执行 Trace |
| `SigNoz/signoz` | 主体 MIT；可观测性详情与 Trace 表达成熟 | 参考证据时间线与详情布局 |

最终选择：**现有技术栈直接采用，外部项目仅作交互参考，在项目内定制少量 UI primitives。**
不新增重量级依赖，不接受许可证传播风险。

## 5. 产品能力地图

### 5.1 事件中心

- 展示活跃、调查中、待审批、恢复中与已恢复 Incident；
- 展示严重级别、影响服务、数据来源、负责人、处理时长和当前阶段；
- 对相关告警先聚合为 Incident，避免同一问题重复调查；
- 快速预览领先假设、关键证据、Specialist 进度与下一步动作。

### 5.2 调查工作台

- 展示 Single/Multi-Agent 路由结果与 Specialist 执行状态；
- 展示 Planner、调查、聚合、裁决、决策和 Validator 的公开执行链；
- 展示竞争假设、支持/反驳证据、工具审计、因果链与恢复提案；
- 展示自动恢复权限、人工审批、安全降级、checkpoint 与恢复验证。

### 5.3 运维助手

- 提供运维问答、知识检索、Incident 查询和诊断状态查询；
- 可将异常描述升级为正式调查，或创建人工恢复审批请求；
- 回答引用真实 Knowledge、Incident、Report、Evidence 或 Tool Result；
- 不直接执行恢复，不修改 AIOps 的安全字段和诊断结论。

### 5.4 知识中心

- 管理知识卡片、SOP、故障博客、原始文档与已解决案例；
- 展示 chunk、索引任务、Milvus 状态和来源可信度；
- 通过检索实验室解释 BM25、向量、RRF、rerank 和最终 citation；
- 将已解决事件转为候选知识，审核后再发布。

### 5.5 Agent 配置

- 管理 Agent 节点、路由策略、Prompt、Skill、模型与工具权限；
- Prompt/Skill 支持草稿、校验、测试、发布、绑定、审计与回滚；
- 每次运行持久化实际使用的 Prompt/Skill 版本；
- 已发布版本不可原地覆盖，修改必须产生新草稿。

### 5.6 集成与系统状态

- 管理 Alertmanager、Prometheus、CLS、MCP、模型服务和 Webhook；
- 展示 PostgreSQL、Redis、Milvus、Nginx、LLM 等组件 readiness；
- 展示后台任务、队列、checkpoint 和最近失败；
- 汇总 Snapshot、Retrieval、Conversation 与 Live Eval 最近结果。

## 6. 一级信息架构

新版一级导航为：

1. **事件中心**：默认首页和 Incident 队列；
2. **调查工作台**：当前事件的诊断、证据、决策与恢复闭环；
3. **运维助手**：自然语言查询和受限操作入口；
4. **知识中心**：知识、文档、案例、索引和检索实验；
5. **Agent 配置**：Agent、Prompt、Skill、模型、工具与发布；
6. **集成中心**：告警、指标、日志、MCP、模型和 Webhook；
7. **系统状态**：基础设施、后台任务、readiness 与 Eval。

桌面端使用可折叠左侧导航；窄屏使用抽屉导航。顶部只保留环境、全局搜索、未处理告警、
系统健康和账户入口。

## 7. 事件中心设计

顶部只展示与处置直接相关的四项指标：活跃事件、待人工审批、自动恢复执行中、最近 24 小时
安全闭环率。主体采用事件列表与选中事件预览的 master-detail 布局。

事件行展示：

- 严重级别、状态、告警标题和影响服务；
- Alertmanager、CLS 等来源与环境；
- 当前处理阶段、Single/Multi 模式；
- 已运行时间、负责人和自动恢复许可状态。

预览区展示影响摘要、领先假设、证据完整度、Specialist 进度、下一步动作和进入调查工作台
的主操作。列表需提供 loading、empty、error、partial 与 stale 状态，不能用静态数字伪造数据。

## 8. 调查工作台设计

页面由事件概要、安全状态、调查主视图和事件侧栏组成。主视图使用以下标签页控制信息密度：

- **概览**：当前结论、缺失证据、风险和建议动作；
- **调查过程**：Planner、Specialist、Aggregator、Adjudicator、Decision、Validator；
- **假设与证据**：支持/反驳证据、来源、时间、可信度和充分性；
- **工具调用**：参数摘要、耗时、状态、幂等键和失败分类；
- **恢复闭环**：提案、审批、执行、验证和回滚；
- **完整审计**：追加式公开事件时间线。

侧栏展示影响范围、Agent 路由、实际 Prompt/Skill 版本、checkpoint 和审计身份。页面不展示
隐藏思维过程，只展示可验证的计划、动作、证据、假设状态变化和决策依据。

`inconclusive` 必须说明缺少什么证据。LLM Validator 失败但确定性校验通过时，明确展示
“安全降级为人工复核”，不能误报为诊断完全失败。

## 9. 运维助手设计

页面保留会话主区，将引用、工具执行和 Run 详情放入可访问的详情抽屉。Prompt/Skill 编辑器
不再常驻 Chat。建议问题改为真实任务，例如查询服务超时、查看 APY 事件证据、检索 PostgreSQL
排查卡片和创建人工恢复审批。

上下文压缩、模型、Intent Router、工具 allowlist 和执行预算属于 Run 详情；默认界面只突出
用户问题、可验证回答、引用和下一步操作。

## 10. 知识中心设计

知识中心分为知识卡片、文档来源、故障案例和检索实验室四个视图。内容必须显示来源、版本、
适用组件、更新时间、索引状态和可信级别。检索实验室按阶段展示候选、排名和引用理由，帮助
区分“被召回”和“最终被 Agent 使用”。

## 11. Prompt 与 Skill 运行模型

### 11.1 Prompt

Prompt 按 Conversation、Planner、Specialist、Aggregator、Adjudicator、Validator 等节点分类，
包含模板、变量、输出 Schema、模型参数、适用范围和版本元数据。

生命周期：

```text
草稿 -> Schema/变量校验 -> 测试运行 -> 发布版本 -> 绑定 Agent -> 启用 -> 审计/回滚
```

只有发布版本可以绑定运行节点。发布版本不可原地编辑，回滚通过重新激活历史版本完成。

### 11.2 Skill

这里的 Skill 是产品内的 Agent 能力定义，不是 Codex 本地 `SKILL.md`。每个 Skill 包含：

- 名称、说明、适用场景和版本；
- 输入输出 Schema；
- 可调用工具、数据源与权限范围；
- 风险等级、超时、重试和幂等策略；
- 可绑定 Agent 节点与启用状态。

Skill 只能缩小或组合既有权限，不能越过系统工具 allowlist、owner scope 或恢复 Policy Gate。

### 11.3 运行时审计

每次 Chat Run 与 Diagnostic Task 记录绑定配置的稳定版本 ID。配置发布、绑定、启停和回滚均
形成 owner-scoped 审计事件，以保证结果可复现。密钥与完整 Prompt 内容不进入普通运行日志。

第一版实现立即启用，数据模型保留灰度发布扩展点，但不提前实现复杂流量分配。

## 12. 集成中心与系统状态

集成中心按告警与指标、日志与检索、模型服务、通知入口、数据基础设施分组。每项展示连接
状态、最近检查、响应耗时、可用工具、权限范围和脱敏配置摘要。密钥只能更新，不能回显明文。

系统状态页区分 `/health` 的“进程存活”和 `/ready` 的“完整能力可用”，并回答：

1. 能否接收告警；
2. 能否完成 Agent 调查；
3. 能否安全完成恢复闭环。

系统状态还展示后台任务、队列积压、checkpoint、版本环境与四类 Eval 的最近持久化结果。

## 13. 视觉系统

采用浅色专业运维控制台方向：

- 暖灰工作区、深石墨导航；
- 青绿色用于品牌和健康状态；
- 蓝色用于调查与执行；
- 琥珀色用于等待、风险和需关注状态；
- 红色只用于严重故障与危险操作；
- 正文高对比深灰，正文不小于 14px，关键内容 15–16px；
- 高密度表格、Trace 和证据区通过行高、分组与渐进披露保证可读性。

禁止大面积 AI 渐变、玻璃拟态、无意义装饰图表、Emoji 图标、过小状态文字和取消 focus
outline。颜色必须同时配合文字或图标传达状态。

项目内建立 CSS design tokens 和少量通用 primitives：Button、Badge、Panel、Drawer、Tabs、
Timeline、EmptyState、Skeleton、ConfirmDialog。图标继续统一使用 Lucide Vue。

## 14. 响应式与可访问性

- 桌面端提供完整 master-detail 和调查 Trace；
- 平板收起侧栏，详情进入抽屉；
- 手机支持事件查看、审批和关键状态，不压缩完整 Trace 编辑器；
- 所有交互可由键盘完成，提供清晰 focus-visible；
- 状态不只依赖颜色，图标按钮必须有可访问名称；
- 尊重 `prefers-reduced-motion`，动画不能阻塞任务；
- 危险恢复操作在所有尺寸下均需要明确确认。

## 15. 数据、权限与错误状态

- 所有事件、配置、知识、审计和集成继续按已认证 owner/tenant 过滤；
- 前端不能把客户端提交的 owner/tenant 当成授权依据；
- Prompt/Skill 发布、绑定、回滚与危险操作需要明确权限和审计；
- 页面完整支持 loading、empty、error、partial、stale、permission denied 与 retrying；
- 不显示原始异常、凭据、隐藏 reasoning、完整工具返回或 checkpoint 原始 state；
- API/SSE 契约变化必须先更新共享契约并补合同测试。

## 16. 实施边界与验收方向

本次选择完整重构范围，但实施必须按可独立验收的 vertical slice 推进，避免一次性替换全部
页面。实现计划至少覆盖：

1. OpenSpec change、共享术语和路由迁移策略；
2. 设计 tokens、primitives、应用 Shell 和事件中心；
3. 调查工作台与现有 AIOps 数据适配；
4. 运维助手、知识中心、集成中心和系统状态迁移；
5. Prompt/Skill 后端模型、迁移、API、权限、发布绑定与运行时加载；
6. Agent 配置前端与审计；
7. 桌面/窄屏、可访问性、契约、组件和端到端验收。

可见 UI 交付必须附桌面和窄屏截图。最终验证至少包括前端 typecheck/test/build、共享契约、
后端目标 pytest/Ruff/Pyright、OpenSpec 校验和与运行环境相称的真实 API 冒烟测试。

## 17. 成功标准

- 用户登录后能从事件中心识别最需要处理的 Incident，并进入调查；
- 用户能理解 Agent 当前阶段、证据是否充分、为什么得出根因和为何需要人工介入；
- 自动恢复、人工审批、安全降级和恢复验证不会被混淆；
- Conversation Agent 能查询真实 AIOps 结果，但不能绕过安全边界；
- 知识检索排名、来源和最终引用可解释；
- Prompt/Skill 可以版本化发布、绑定、运行、审计和回滚；
- 系统能区分进程在线、依赖就绪和完整闭环可用；
- UI 在桌面与窄屏均可用，键盘焦点、文字尺寸和状态表达符合可访问性要求；
- 不新增重量级 UI 依赖，不迁移 React，不破坏现有 typed client、SSE 与 owner 隔离。
