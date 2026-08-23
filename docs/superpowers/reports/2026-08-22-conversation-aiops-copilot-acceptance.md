# Conversation Agent 与 AIOps Agent 关联验收

**日期：** 2026-08-22  
**验收 Git SHA：** `d06798e0794d0d04f9f19eac59db6a8f40235c2f`  
**数据库 revision：** `202608220002 (head)`  
**结论：** 聚焦验收通过

## 交付范围

- 规则优先的 Chat Intent Router 与按意图最小工具白名单；
- owner-scoped Incident、Diagnostic、Report、Evidence 和恢复审批 Bridge；
- 诊断启动、Chat Run、事件、工具调用和审批请求的 PostgreSQL 幂等持久化；
- Background Job 重试与 Worker 恢复；
- 基于 PostgreSQL sequence 的 SSE 回放及前端断线续订；
- 模型 reasoning 不持久化、不经 SSE/API 返回、不在 UI 展示；
- 12 场景离线 Conversation Eval 与五项不可抵消安全硬门。

## 验收结果

| 检查 | 结果 |
|---|---|
| 后端 12 个聚焦测试文件 | 68/68 通过 |
| Conversation Eval | 12/12 场景通过；7/7 scorer/硬门测试通过 |
| Ruff（Chat、Alert ingestion、相关测试） | 通过 |
| Pyright（完整 backend） | 0 errors、0 warnings |
| API contracts | 25/25 通过，TypeScript typecheck 通过 |
| Frontend 聚焦测试 | 18/18 通过，Vue TypeScript typecheck 通过 |
| Alembic | 数据库 current 与 heads 均为 `202608220002` |

后端聚焦集合包含 Intent Router、AIOps Bridge、Chat Run Repository/API、Conversation
Eval、stream/session/memory、active alerts、alert ingestion 和 PostgreSQL Background Jobs。

## Conversation Eval 指标

安全 fake runner 的 12 场景基线结果：

| 指标 | 结果 |
|---|---:|
| intent accuracy | 1.0 |
| target extraction | 1.0 |
| allowed-tool precision | 1.0 |
| task completion | 1.0 |
| grounding | 1.0 |
| idempotency | 1.0 |
| cross-tenant isolation | 1.0 |
| recovery safety | 1.0 |
| structured safety fidelity | 1.0 |
| SSE replay correctness | 1.0 |
| reasoning leakage | 0 |

跨租户访问、越权工具、reasoning 泄露、自动恢复执行、结构化安全字段不一致五个硬门均为
零失败；每个硬门的负向注入测试均能使整套 Eval 失败。该结果只证明 Conversation Agent
编排与安全契约，不代表真实 LLM 的故障诊断准确率。

## 安全审计说明

`restart_service`、`execute_recovery` 和 `executionPermitted=true` 在 Chat 运行时代码中无命中。
reasoning 关键词仅命中四类防护位置：Conversation Eval 泄露检测、公共 Run Event 禁止字段、
供应商兼容事件识别后丢弃、历史 metadata 清洗。它们都不会向用户输出或持久化 reasoning。

Chat 只允许创建状态为 `pending` 且 `executionPermitted=false` 的人工审批请求；第一版没有
批准或执行恢复的 API/Tool。

## 未执行项与边界

- 未运行全量 `pytest`：用户明确要求只做聚焦验证，且设计将全量测试列为本阶段非目标；
- 未运行真实 LLM、CLS、Retrieval、Snapshot 或 Docker Live：避免额度消耗和外部环境噪声；
- 未删除旧 `messages:stream`：迁移期保留兼容并标记 deprecated；
- Conversation Eval 使用离线 fake observation，不替代现有 AIOps Benchmark。
