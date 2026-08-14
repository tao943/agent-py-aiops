# Live Benchmark 真实 CLS 证据链设计

日期：2026-08-14  
状态：已完成对话设计确认，等待书面规格审阅

## 1. 背景与目标

当前 `APY-LIVE-PG-LOCK-001` 已覆盖 PostgreSQL 行锁注入、业务超时、Agent 诊断、白名单恢复、独立验证和确定性评分，但诊断证据来自本地 PostgreSQL 工具。场景中的 `cls_region="docker-live"` 与 `cls_topic_id="local-postgres"` 仍是占位值，因此还不能证明生产 Workflow 能通过真实腾讯云 CLS 获取业务日志。

本改造在不破坏现有本地快速验证的前提下，为该场景增加真实 CLS 证据模式。完整链路必须证明：本次运行的结构化日志被上传到指定 CLS Topic、在限定时间内可检索、Agent 通过真实 CLS MCP `SearchLog` 使用了这些日志，并将查询、证据、推理、恢复和验证过程持久化为可审计记录。

本阶段不实现 CLS 告警规则或 webhook 自动触发。Benchmark 仍以预置告警开始，CLS 只作为真实证据源。

## 2. 约束

- 保留现有 PostgreSQL-only Live 模式，供离线、CI 和快速开发使用。
- CLS 模式不得静默回退到本地日志或伪造日志。
- CLS 负责请求错误与事件时间线；PostgreSQL 会话和锁图工具仍是数据库锁事实的权威来源。
- 现有恢复白名单、风险分级、ground truth 隔离和独立验证硬门槛不得弱化。
- 密钥继续只存在于被 Git 忽略的 `config/user.project.json` 或运行环境变量中，不进入报告、日志、测试快照或 Git。
- 不引入新的 CLI、CLS 客户端框架或自定义腾讯云签名实现。

## 3. 复用评估

### 3.1 检索范围

已检查项目依赖、现有模块、相邻实现和官方开源实现，重点包括：

- 项目已有 `tencentcloud-cls-sdk-python==1.0.4` 上传能力；
- `apps/backend/scripts/generate_and_upload_cls_logs.py` 已实现安全合成日志上传；
- `apps/backend/src/super_ai/mcp_client.py` 的 `LocalMcpClient` 已支持真实 SSE/streamable HTTP MCP 工具发现和调用；
- 项目启动脚本已能运行官方本地 CLS MCP Server；
- 腾讯云官方 Python SDK/API 合同可用于资源验证；
- `tccli` 候选的包元数据未提供清晰 license/homepage，且 `CreateIndex help` 出现递归错误。

### 3.2 选择

采用“直接复用 + 小范围扩展”：

- 直接复用 `LocalMcpClient` 和官方 CLS MCP Server；
- 扩展现有 CLS 上传路径，使其支持 Live run 级结构化日志；
- 在现有 Live runner/diagnostics/scoring 边界内增加证据源选择和审计规则；
- 不采用 `tccli`，不增加依赖，不复制第三方实现。

该选择的主要风险是外部 CLS 最终一致性和网络波动，因此设计有界轮询，并把环境失败与 Agent 能力失败分开。

## 4. 方案比较

### 4.1 双模式复合证据（采用）

`local` 模式使用现有 PostgreSQL 工具；`cls` 模式同时提供真实 CLS `SearchLog` 和 PostgreSQL 证据工具。该方案既能测试线上证据链，又能保留快速、低成本的本地回归，并避免用日志替代锁图事实。

### 4.2 CLS 完全替换本地证据（不采用）

接口较少，但日志不能可靠证明实时阻塞关系。Agent 容易根据超时文本猜测根因，证据质量低于当前 Benchmark 要求。

### 4.3 CLS 下载后转成本地快照（不采用）

运行稳定，但绕过真实 MCP 查询，无法证明生产 Workflow 对 CLS 的发现、调用、范围限定和审计能力。

## 5. 架构与职责

### 5.1 运行模式

Live runner 增加显式证据源选择：

```text
--evidence-source local
--evidence-source cls
```

- `local`：保持现有行为，报告标记 `evidence_source=local`。
- `cls`：要求真实 CLS 配置、上传、可检索性和 `SearchLog` 调用全部成功；报告标记 `evidence_source=cls`。

默认值保持 `local`，避免普通本地测试和 CI 意外访问外部服务或消耗额度。要求完整线上证据链时必须显式选择 `cls`。

### 5.2 组件边界

1. **Live log emitter**
   - 根据真实故障运行状态生成结构化实验日志；
   - 每条日志包含运行身份和可关联字段；
   - 只处理日志构造，不负责诊断或评分。

2. **CLS uploader/poller**
   - 使用项目现有 CLS SDK 上传日志；
   - 有界轮询，直到本次运行日志可通过真实 CLS 查询；
   - 返回上传与可检索性审计，不向调用方暴露密钥。

3. **Composite evidence toolset**
   - 复用 `LocalMcpClient` 发现和调用官方 CLS MCP `SearchLog`；
   - 同时暴露现有 `InspectPostgresSessions`、`InspectPostgresLockGraph` 和 `VerifyServiceHealth`；
   - 不把预轮询结果直接作为 Agent 证据，Agent 仍必须自己调用 `SearchLog`。

4. **Evidence validator**
   - 验证 Topic、时间窗口、`run_id`、`scenario_id`、`incident_id`；
   - 阻止跨运行日志进入有效证据集合；
   - 检查工具调用和证据引用是否已持久化。

5. **Live result classifier/scorer**
   - 先判定运行有效性，再执行现有 100 分能力评分；
   - 将外部基础设施失败与 Agent 失败分开。

## 6. 数据流

```text
预置 benchmark alert
  -> 注入 PostgreSQL 行锁
  -> 触发有界业务超时
  -> 生成带 run_id/scenario_id/incident_id 的结构化日志
  -> 上传到配置的真实 CLS Topic
  -> 有界轮询，确认日志已可检索
  -> 启动生产 Agent workflow
  -> Agent 调用 SearchLog 与 PostgreSQL 诊断工具
  -> 保存工具审计、证据引用、竞争假设与根因决策
  -> 通过白名单策略执行或建议恢复
  -> 独立验证服务健康和锁解除
  -> 先判定运行有效性，再执行确定性评分
```

轮询只负责确认基础设施准备就绪，不替代 Agent 的工具调用。Agent 的 `SearchLog` 查询必须重新出现在工具审计中。

## 7. 日志和查询契约

每条 Live 日志至少包含：

```text
run_id
scenario_id
incident_id
service
environment
event
level
trace_id
component
message
timestamp
```

查询必须同时限定：

- 配置的 region 和 Topic；
- 本次故障开始前的小幅安全缓冲到查询时刻的有界时间窗口；
- `run_id` 完全匹配；
- `scenario_id` 完全匹配；
- `incident_id` 完全匹配。

证据验证以结构化字段为准，不接受只在自由文本 `message` 中出现运行标识的记录。查询返回其他运行的数据时，这些记录必须被拒绝并产生隔离审计。

## 8. 审计模型

每次 CLS Live 运行必须持久化以下信息：

- 运行身份：`run_id`、`scenario_id`、`incident_id`；
- CLS 范围：region、Topic ID、查询起止时间；
- 上传审计：预期日志数、已确认上传数、上传时间、批次标识；
- 轮询审计：尝试次数、每次结果数、最终可检索时间、超时原因；
- 工具审计：`SearchLog` 参数、状态、受限结果摘要、证据引用；
- 证据归属：每条有效记录的运行身份与 `trace_id`；
- 决策链：候选假设、支持证据、反驳证据、根因选择和淘汰理由；
- 恢复审计：动作、白名单结果、风险等级、人工审批要求和执行结果；
- 独立验证：服务健康、锁状态和业务探针结果。

报告可以保存非密钥资源 ID，但不得序列化 SecretId、SecretKey、Authorization header 或 SDK 客户端配置。

## 9. 有效性与错误处理

最终状态分为：

```text
VALID_PASS      基础设施有效，Agent 能力通过
VALID_FAIL      基础设施有效，Agent 诊断、恢复或验证失败
INFRA_INVALID   CLS、MCP 或实验环境失败，本次能力结果无效
```

以下任一条件触发 `INFRA_INVALID`：

- CLS 上传失败或无法确认上传完整性；
- 有界轮询结束后，本次运行日志仍不可查询；
- CLS MCP Server 不可用、未发现 `SearchLog` 或调用失败；
- 查询成功但没有本次运行的日志；
- 返回证据属于其他 `run_id`、scenario 或 incident，且没有足够的本次运行证据；
- `SearchLog` 调用、参数、结果或引用未进入审计记录；
- 独立验证基础设施本身不可用，无法判断恢复结果。

`INFRA_INVALID` 不生成 Agent 0 分，也不得伪装成 `VALID_FAIL`。CLS 模式下禁止自动切换到 `local`。错误信息应指出失败阶段和可重试性，但不包含密钥。

## 10. 推理与多根因处理

系统不能把超时现象直接等同于数据库锁。Agent 必须维护多个竞争假设，例如数据库锁、连接池耗尽、下游服务超时或流量突增，并根据证据逐步支持或反驳：

- CLS 日志用于确认受影响请求、错误类型、时间线和 trace 关联；
- PostgreSQL sessions/lock graph 用于确认等待事件、阻塞 PID 和锁关系；
- RAG 只提供排查方法和候选模式，不能作为本次事故事实；
- 根因决策必须引用本次运行证据，并记录其他主要假设被淘汰的理由。

对于 PostgreSQL 锁场景，只引用 CLS 超时日志而没有数据库锁证据，不满足根因证据硬门槛。

## 11. 评分设计

评分分两层执行：

1. **运行有效性门槛**：CLS 上传、查询、范围隔离和审计全部满足；
2. **现有 100 分 Agent 能力评分**：根因判断、证据质量、恢复决策、执行安全和恢复验证。

CLS 接入本身不增加分数，以免成功调用外部接口造成能力虚高。只有有效运行才计算 100 分。

CLS 模式新增硬门槛：

- 存在成功且可审计的真实 `SearchLog` 调用；
- 查询范围绑定本次运行；
- 根因结论至少引用一条有效 CLS 证据；
- PostgreSQL 锁事实由数据库工具支撑；
- Agent、Prompt、RAG 和报告不能读取 ground truth；
- 自动恢复通过白名单和风险策略；
- 恢复结果由独立验证器确认。

现有各能力维度的分值不因证据源变化而调整，保证 `local` 与 `cls` 的有效结果仍可比较；报告同时展示证据源，避免混淆两种覆盖范围。

## 12. 测试策略

### 12.1 普通 CI

- 日志字段和查询范围构造单元测试；
- 运行身份、Topic 和时间窗口隔离测试；
- `INFRA_INVALID` 分类和敏感字段脱敏测试；
- 工具审计、证据引用和报告序列化测试；
- 使用固定 MCP 响应的适配器契约测试；
- Docker PostgreSQL 故障与 `local` 模式回归。

固定 MCP 响应只验证项目内部边界，测试名称和报告不得将其称为真实 CLS 验证。

### 12.2 真实 CLS Live 验收

真实验收覆盖：

- 向配置 Topic 上传本次运行日志；
- 有界轮询直至可检索；
- 通过官方 CLS MCP `SearchLog` 获取相同运行日志；
- Agent 结合 CLS 与 PostgreSQL 证据完成诊断；
- 持久化完整审计、恢复与独立验证；
- 通过确定性有效性门槛和能力评分。

该测试依赖云凭据、网络和外部额度，不进入无密钥的普通 PR CI。它作为本地显式命令或受保护的手动 GitHub Actions workflow 运行，并明确展示 `INFRA_INVALID`、`VALID_FAIL` 或 `VALID_PASS`。

### 12.3 关键失败测试

- 上传成功但索引延迟超过期限；
- `SearchLog` 返回空结果；
- 返回其他 `run_id` 的日志；
- 查询时间窗口或 Topic 错误；
- MCP 工具发现失败或调用超时；
- Agent 未引用 CLS 证据；
- Agent 只凭日志断言数据库锁；
- 审计持久化中断；
- 输出包含疑似密钥字段。

## 13. 安全与运行成本

- Live 日志只包含实验数据，不上传真实用户隐私；
- 本地密钥配置继续被 Git 忽略；
- 默认 `local` 模式防止开发和 CI 意外产生云调用；
- 轮询采用有界次数、退避和总超时，不无限等待；
- 日志不在每次测试后主动删除，使用 Topic 当前 7 天保留期便于短期审计；
- 任何错误输出和报告写入前执行敏感字段过滤。

## 14. 非目标

- 不创建 CLS 告警规则；
- 不实现 webhook 自动触发诊断；
- 不扩展到新的 Live 故障类型；
- 不改变现有知识库内容或 Retrieval Eval；
- 不新增自动修复动作；
- 不让普通 PR CI 使用真实云凭据；
- 不以 CLS 日志替代 PostgreSQL 权威证据。

## 15. 验收标准

实现完成后应满足：

1. 同一场景可显式运行 `local` 或 `cls` 证据模式；
2. `local` 模式保持现有行为和评分结果；
3. `cls` 模式上传并查询只属于本次运行的真实 CLS 日志；
4. Agent 工具审计显示真实 `SearchLog` 及其范围和证据引用；
5. 根因决策同时使用 CLS 时间线证据和 PostgreSQL 锁证据；
6. 上传、查询、隔离或审计失败产生 `INFRA_INVALID`，且不回退；
7. 有效运行继续使用现有 100 分能力评分；
8. 报告不包含任何云密钥；
9. 普通 CI 不需要腾讯云凭据，真实 CLS 验收可以显式运行；
10. 现有白名单恢复、ground truth 隔离和独立验证测试继续通过。
