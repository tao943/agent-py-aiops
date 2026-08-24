# 系统架构

## 产品边界

Agent Py 是一个本地优先的 AIOps 工作台。告警或对话负责把运维问题转换为事件，AIOps Agent 负责收集证据、比较候选根因并形成恢复决策；任何会产生副作用的恢复动作都必须通过确定性安全校验与 Policy Gate。

当前项目面向单机开发、作品演示和可复现评测，不宣称 Kubernetes、多区域高可用或无人值守生产部署。

## 端到端数据流

```text
Alert 或 Chat 入口
→ Single/Multi-Agent Router
→ CLS/PostgreSQL/Redis/Prometheus/RAG
→ Evidence Aggregator
→ Adjudicator / Decision
→ Validator / Policy Gate
→ 白名单自动恢复或人工审批
→ Verify / Audit
```

Alertmanager 告警和 Chat 确认入口最终复用同一套 AIOps 诊断与恢复闭环。Chat 负责解释、查询和确认，不复制一套独立的运维执行器。

## Agent 运行时

- Conversation Agent 使用 LangChain `create_agent`，处理对话、知识检索、事件查询、诊断启动与人工确认入口。
- AIOps Agent 使用 LangGraph Plan-Execute-Replan。Planner 生成证据计划，Executor 调用真实工具，Replanner 根据缺失证据决定继续调查或进入决策。
- 简单问题走 Single-Agent；复杂且跨数据源的问题可路由到最多两个并行 Specialist，再由 Evidence Aggregator 汇总到同一事实、裁决和验证链。
- 核心 AIOps Prompt、Validator、工具 allowlist 与恢复策略由服务端控制。用户 Prompt 和 Skill 不能扩大工具权限或绕过安全门。

## 证据与知识

- 腾讯云 CLS MCP 提供真实日志查询；PostgreSQL、Redis 和 Prometheus 工具提供数据库、缓存和指标证据。
- Milvus 向量召回与 BM25L 关键词召回并行执行，经 RRF 融合与 Qwen rerank 精排；引用保留各阶段排名和来源信息。
- Evidence、工具审计、假设状态、裁决、报告、恢复意图和验证结果按 owner scope 持久化，结论可以追溯到实际 Observation。
- RAG 只保存通用排查知识、SOP 和复盘卡，不导入 Benchmark 的 `scenario.yaml`、Snapshot、ground truth、oracle、provenance 或评分规则。

## 恢复治理

- 低风险且命中白名单的动作，只有在证据充分、Validator 通过且 Policy Gate 许可时才能自动执行。
- 高风险、证据不足、状态不确定、Validator 降级或验证失败时进入人工审批或人工复核。
- 恢复动作使用稳定意图与幂等键，避免 Worker 重试或网络中断导致副作用重复发生。
- 动作完成后必须独立验证；不可安全重放的动作在结果不确定时不得盲目补执行。

## 存储与基础设施

- PostgreSQL 16 是用户业务数据、durable job、事件、执行 checkpoint、恢复意图和审计记录的关系型事实来源。
- Redis 7 提供缓存、分布式限流和低延迟事件投递辅助，不是持久任务、最终状态或审计的唯一事实来源。
- Milvus 保存知识向量，并按 `ownerUserId`、`tenantId`、`knowledgeBaseId` 和 `documentId` 过滤。
- Docker Compose 管理 PostgreSQL、Redis、etcd、MinIO、Milvus、Attu、Alertmanager 与 Nginx；Prometheus 和故障服务由 `live-eval` profile 显式启动。
- Nginx 是本地统一入口，负责反向代理和边缘限流；FastAPI 内部限流继续保护模型、MCP 与后台资源预算。

## 前端工作区

| 路由 | 职责 |
|---|---|
| `/incidents` | 事件队列、严重度、诊断状态、审批与恢复意图摘要 |
| `/incidents/:incidentId` | 证据时间线、候选根因、工具审计、诊断报告和恢复闭环 |
| `/assistant` | 持久对话、结构化记忆、知识引用、工具活动和待确认动作 |
| `/knowledge` | 文档上传、分块、索引、检索与知识卡管理 |
| `/agent-config` | Prompt/Skill 版本、发布生命周期、绑定和审计 |
| `/integrations` | Alertmanager、CLS/MCP、模型和数据基础设施配置入口 |
| `/system` | 进程存活、依赖就绪、配置有效性、后台任务和 Eval 摘要 |

`/chat`、`/aiops` 和 `/mcp` 只保留为兼容重定向，分别转到 `/assistant`、`/incidents` 和 `/integrations`。

## 安全与隔离

- 用户身份来自认证上下文；聊天、知识、向量、事件、诊断、报告、恢复和审计都按 owner/tenant 过滤。
- 本地 `config/project.json` 与 `config/user.project.json` 被 Git 忽略；模板不包含密钥、云资源 ID 或演示密码。
- API、SSE、日志和审计摘要不得泄露密钥、原始异常、完整工具输出、隐藏推理或 Benchmark 答案。
- Benchmark Runner 隔离 Agent 可见输入与 ground truth；路径穿越、oracle 伪装和读取答案工具均属于安全失败。
- 恢复工具采用服务端 allowlist、参数合同、权限检查、幂等保护和追加式审计。

## 部署边界

当前推荐工作流是在单台开发机上使用 Docker Compose 运行基础设施，前端、FastAPI 后端和官方 CLS MCP Server 在本机进程中启动。真实模型、CLS 和 Docker Live Eval 需要调用者自行配置外部服务、额度与权限，并通过显式确认运行。

项目当前没有提供 Kubernetes manifests、云上高可用拓扑、跨区域容灾或任意命令执行能力。生产化部署仍需补充密钥管理、外部负载均衡、备份恢复、集中监控和容量规划。
