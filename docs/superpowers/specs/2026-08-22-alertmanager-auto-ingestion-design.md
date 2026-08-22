# Alertmanager 自动告警接入与诊断触发设计

**日期：** 2026-08-22  
**状态：** 已确认  
**目标版本：** 第一版可靠 MVP

## 1. 背景与目标

AgentPy 已具备 Alertmanager 活跃告警读取、CLS 证据查询、RAG、LangGraph 诊断、
PostgreSQL 诊断记录、Redis 运行时、Background Job、Outbox、Nginx 和恢复安全门。
当前缺口是告警仍需用户手动选择并启动诊断，无法展示生产系统中的自动触发闭环。

本设计新增 Alertmanager Webhook 接入，使系统能够在收到 `firing` 告警后自动创建
Incident、诊断任务和持久化后台作业；重复通知不会重复调用 Agent，`resolved` 通知会
关闭 Incident，但不会取消已经开始的诊断。

第一版成功闭环为：

```text
测试脚本或 Prometheus
        -> Alertmanager
        -> Nginx Webhook 限流
        -> Alert Ingestion API
        -> PostgreSQL Incident / Event / Diagnostic Task / Background Job
        -> 现有 aiops_diagnosis Handler
        -> LangGraph 诊断与审计报告
```

## 2. 范围

### 2.1 本阶段包含

- Alertmanager Webhook v4 接入；
- 每个 Source 固定服务账号与知识库；
- Bearer Token 鉴权；
- 配置化标签过滤；
- `groupKey` 级 Incident 生命周期；
- PostgreSQL 最终幂等与事务性任务创建；
- Redis 短租约竞争抑制及 PostgreSQL 降级；
- firing、重复 firing、resolved、orphan resolved 和 filtered 审计；
- Nginx 独立限流；
- 结构化运行指标；
- 使用本地 Alertmanager 的端到端验收。

### 2.2 本阶段不包含

- Prometheus 服务、指标采集和告警规则；
- 腾讯云 CLS 告警规则或 CLS Webhook Adapter；
- 按告警标签动态选择租户；
- 告警成员变化后的增量诊断；
- resolved 后额外调用 LLM 做恢复验证；
- 自动执行高风险恢复动作；
- 前端 Incident 管理页面；
- 完整告警平台、排班、升级、通知编排和静默管理；
- 全量 Benchmark 或全量 pytest。

## 3. Reuse-first 评估

### 3.1 约束

- Python 3.10+、FastAPI、SQLAlchemy 2、asyncpg、PostgreSQL 16；
- 继续使用现有 Redis、Background Job、Outbox、Nginx 和 LangGraph；
- 不增加外部服务、原生二进制或重量级依赖；
- 不接受许可证不明确或会改变项目许可证义务的代码；
- Webhook 不得信任 Payload 中的租户、知识库或恢复权限。

### 3.2 GitHub 调研

| 候选 | 许可证 | 结论 |
|---|---|---|
| [prometheus/alertmanager](https://github.com/prometheus/alertmanager) | Apache-2.0 | 参考 Webhook v4 Message、2xx 成功和 5xx 可重试语义；继续直接运行现有镜像 |
| [grafana/oncall](https://github.com/grafana/oncall) | AGPL-3.0 | 完整事故响应平台，功能和部署过重，许可证义务不适合作为第一版依赖 |
| [keephq/keep](https://github.com/keephq/keep) | GitHub API 返回 NOASSERTION | 功能和部署过重，许可证未明确，不采用代码或依赖 |

通用关键词 `alertmanager webhook fastapi` 与 `alertmanager incident management python`
未返回可靠候选，因此又核对了三个明确相关的上游项目。最终选择：

- **直接采用：** 现有 Alertmanager、PostgreSQL、Redis、Background Job、Outbox、Nginx；
- **参考实现：** Alertmanager 官方 Webhook v4 数据契约和重试语义；
- **自定义实现：** 项目内部轻量 Adapter、Incident 状态机和原子 Repository；
- **新增依赖：** 无。

## 4. 架构与组件边界

新增 `super_ai.alert_ingestion` 包：

```text
alert_ingestion/
  domain.py          不可变领域对象、状态和安全结果
  config.py          Source、限制和标签过滤配置
  alertmanager.py    Webhook v4 解析、白名单化、哈希
  repositories.py   Incident/Event 原子持久化协议
  sqlalchemy.py      PostgreSQL 事务与并发幂等
  redis_runtime.py   可选短租约竞争抑制
  service.py         firing/resolved/filtered 状态机
  metrics.py         内存计数和延迟快照
  routes.py          FastAPI Router、鉴权、大小限制和安全响应
```

现有 `super_ai.alerts` 继续只负责主动读取活动告警，不承担 Webhook 接入。现有
`api/app.py` 只负责组装依赖和挂载 Router，业务逻辑不写入该大文件。

### 4.1 数据流

```text
POST /aiops/alerts/webhook/alertmanager/{source_id}
  -> 查找已启用 Source
  -> 校验 Content-Length 和实际读取字节数
  -> 常量时间校验 Bearer Token
  -> 解析 Alertmanager v4 Message
  -> 白名单化并计算 payload_sha256 / group_key_hash
  -> 应用 Source 标签过滤器
  -> Redis 短租约（可选，失败不阻塞）
  -> PostgreSQL 原子状态迁移
  -> commit 后唤醒现有 BackgroundJobRuntime
  -> 202 安全响应
```

API 不等待 Agent、LLM、CLS 或 RAG 完成。

## 5. Source 配置与租户边界

tracked 配置只保存环境变量名称，不保存 Token：

```json
{
  "alertIngestion": {
    "enabled": true,
    "maxBodyBytes": 262144,
    "maxAlertsPerDelivery": 50,
    "redisLeaseMilliseconds": 2000,
    "sources": [
      {
        "id": "local-alertmanager",
        "enabled": true,
        "ownerUserId": "user_service_account",
        "knowledgeBaseId": "kb_user_service_account",
        "tokenEnvironmentVariable": "AGENTPY_ALERT_WEBHOOK_TOKEN_LOCAL",
        "allowedLabels": {
          "environment": ["test", "prod"],
          "severity": ["warning", "critical"]
        }
      }
    ]
  }
}
```

启动时必须验证：

- Source ID 唯一且只含安全 URL 字符；
- `ownerUserId`、`knowledgeBaseId` 和 Token 环境变量名非空；
- 当前单知识库约束下，`knowledgeBaseId == "kb_" + ownerUserId`；
- Token 环境变量存在且值至少 32 个字符；
- allowedLabels 非空，键和值长度有界；
- Source Payload 不能覆盖以上任何字段。

服务账号和知识库由运维预先创建。Webhook 不使用用户 Session，也不接受
`ownerUserId`、`knowledgeBaseId` 或 `executionPermitted` 请求字段。

## 6. Alertmanager v4 安全契约

### 6.1 Delivery 顶层字段

只接受并标准化：

- `version`：必须为 `4`；
- `status`：必须为 `firing` 或 `resolved`；
- `receiver`；
- `groupKey`：必须为非空字符串，只计算 SHA-256；
- `externalURL`：只保留可解析的 `scheme://host[:port]` origin；
- `alerts`：1 到 50 条；
- `truncatedAlerts`：非负整数。

`groupKey` 原文、`commonLabels`、`commonAnnotations`、`groupLabels` 和未知字段均不
持久化。完整原始请求仅在内存中参与 SHA-256，随后释放。

### 6.2 Alert 白名单

Labels：

```text
alertname, service, severity, environment, cluster, namespace,
pod, instance, job, run_id, incident_id, trace_id
```

Annotations：

```text
summary, description, sop
```

同时保留 `startsAt`、`endsAt`、`generatorURL` 的安全 origin。单字符串最多 2048 字符，
每个标签键值最多 256 字符；超长内容被确定性截断并记录 `truncated=true`，不把原值写入
日志、数据库或 Prompt。

用于诊断的公开 Alert 摘要只由白名单字段构成。查询文本采用固定模板：

```text
Investigate {alertname} affecting {service}. Severity: {severity}.
Summary: {summary}
```

## 7. 数据模型

### 7.1 `aiops_alert_incidents`

- `id`：随机稳定业务 ID；
- `owner_user_id`；
- `source_id`；
- `group_key_hash`：64 位小写 SHA-256；
- `status`：`active | resolved`；
- `alert_name`、`service`、`severity`；
- `starts_at`、`last_seen_at`、`resolved_at`；
- `delivery_count`；
- `diagnostic_task_id`；
- `created_at`、`updated_at`。

PostgreSQL 部分唯一索引保证同一
`owner_user_id + source_id + group_key_hash` 最多只有一个 `active` Incident。

### 7.2 `aiops_alert_events`

- `id`：由 `owner + source + delivery status + payload_sha256` 派生；
- `incident_id`：filtered 和 orphan resolved 可为空；
- `owner_user_id`、`source_id`；
- `status`：`firing | resolved`；
- `disposition`：`incident_created | duplicate_updated | incident_resolved | filtered | orphan_resolved`；
- `payload_sha256`；
- `normalized_payload`：仅安全白名单字段；
- `received_at`。

事件 ID 唯一。Alertmanager 对相同 Payload 的网络重试不会重复添加 Event，但每次已验证的
Delivery 仍会更新 Incident 的 `delivery_count` 与 `last_seen_at`。

## 8. 生命周期与事务

### 8.1 状态机

```text
无 active + firing
  -> 创建 Incident、Diagnostic Task、Background Job 和 Event

已有 active + firing
  -> 更新 last_seen_at / delivery_count / 最新安全摘要
  -> 不创建新 Diagnostic Task 或 Background Job

已有 active + resolved
  -> Incident 标记 resolved
  -> 已开始的诊断继续完成
  -> 不增加 LLM 调用

无 active + resolved
  -> 记录 orphan_resolved Event
  -> 不创建 Incident 或诊断

resolved 后再次 firing
  -> 新建下一轮 Incident、Diagnostic Task 和 Background Job
```

### 8.2 原子创建

`SQLAlchemyAlertIngestionRepository` 在一个 SQLAlchemy Session 事务中完成：

1. 使用 PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` 尝试创建 active Incident；
2. 只有成功插入 Incident 的事务才创建 Diagnostic Task 和 Background Job；
3. 更新 Incident 的 `diagnostic_task_id`；
4. 使用稳定 Event ID 冲突安全追加 Alert Event；
5. commit 后返回结果。

并发失败路径不依赖捕获失效事务：冲突请求读取并锁定 active Incident，更新重复计数后返回
已有 Diagnostic Task。这样避免 PostgreSQL 唯一约束冲突污染整个 Session。

Background Runtime 的 `start()` 在 commit 后调用。若唤醒失败，API 仍返回 202；持久化 Job
会在应用启动或下一次调度唤醒时由现有租约 Worker 领取。

## 9. Redis 角色

Redis 不是正确性来源。它只提供 2 秒短租约以降低相同 groupKey 的并发数据库争用：

- Key 只含 `source_id` 和 `group_key_hash`；
- Value 是随机 lease token；
- 获取使用 `SET NX PX`；
- 释放使用 compare-and-delete Lua，不能删除其他请求的 lease；
- 未获取 lease 的请求进行短暂有界等待，然后仍进入 PostgreSQL；
- Redis 超时或不可用时立即使用 PostgreSQL；
- Redis 故障记录 degraded 指标，不返回 5xx。

Redis 不能用于跳过 Event 审计、delivery_count 更新或 PostgreSQL 唯一约束。

## 10. API、鉴权与响应

### 10.1 Endpoint

```text
POST /aiops/alerts/webhook/alertmanager/{source_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Bearer Token 使用 `hmac.compare_digest` 校验。缺失、错误和格式不合法统一返回 401，不透露
Source Token 是否存在。Source 不存在或禁用返回 404。

### 10.2 成功响应

首次 firing：

```json
{
  "status": "accepted",
  "incidentId": "incident_...",
  "diagnosticTaskId": "diagnostic_...",
  "duplicate": false,
  "filtered": false,
  "redisMode": "primary"
}
```

filtered、orphan resolved 的 Incident/Diagnostic ID 为 `null`。重复 firing 返回已有 ID 且
`duplicate=true`。响应不包含 labels、annotations、Token、原始 groupKey 或 Payload。

### 10.3 状态码

| 情况 | 状态码 | 持久化/重试语义 |
|---|---:|---|
| 首次、重复、filtered、resolved | 202 | 已安全处理，Alertmanager 不重试 |
| Token 缺失或错误 | 401 | 无持久化 |
| Source 不存在或禁用 | 404 | 无持久化 |
| Body 超过 256 KiB | 413 | 无持久化 |
| JSON 或 Schema 非法 | 422 | 无持久化 |
| PostgreSQL 或事务失败 | 503 | 无成功响应，Alertmanager 按 5xx 重试 |
| Redis 不可用 | 202 | PostgreSQL 降级 |
| commit 后 Worker 唤醒失败 | 202 | Job 已持久化 |

## 11. Nginx 与 Alertmanager 配置

Nginx 增加独立限流区：

```nginx
limit_req_zone $binary_remote_addr zone=alert_webhook_per_ip:10m rate=5r/s;
```

Webhook Location 使用 `burst=20 nodelay`、256 KiB `client_max_body_size`，继续代理到现有
FastAPI upstream。该路径不复用用户 API 的限流预算。

本地 Alertmanager receiver 指向同一 Compose 网络中的 Nginx：

```yaml
receivers:
  - name: agent-py-webhook
    webhook_configs:
      - url: http://nginx/aiops/alerts/webhook/alertmanager/local-alertmanager
        send_resolved: true
        max_alerts: 50
        http_config:
          authorization:
            type: Bearer
            credentials_file: /run/secrets/alert_webhook_token
```

Secret File 不提交 Git。后端从 Source 配置指定的环境变量读取同一个 Token。

## 12. 安全与数据最小化

- 请求体硬上限 256 KiB，单 Delivery 最多 50 条 Alert；
- 不记录 Authorization、Token、原始 Body、原始 groupKey 或未知字段；
- 原始 Body 只计算 SHA-256；
- 日志只包含 Source、Incident ID、Disposition、数量、延迟、Redis 模式和错误类别；
- Payload 中的租户、知识库和恢复权限无效；
- 自动诊断不能绕过现有 Policy Gate；
- 自动诊断默认不允许执行恢复动作；
- 所有数据库查询按 `owner_user_id` 限定；
- API/日志/数据库敏感字段扫描必须为 0 泄漏。

## 13. 可观测性

新增进程内 `AlertIngestionMetrics`，通过现有运行状态响应暴露聚合值：

- `webhookReceivedTotal`；
- `incidentCreatedTotal`；
- `duplicateSuppressedTotal`；
- `filteredTotal`；
- `resolvedTotal`；
- `orphanResolvedTotal`；
- `ingestionFailedTotal`；
- `redisDegradedTotal`；
- `ingestionLatencyMs` 的 count/sum/max；
- `diagnosisEnqueueLatencyMs` 的 count/sum/max。

第一版不引入 Prometheus Client 依赖，也不创建新的 Metrics Server。

## 14. 测试策略

### 14.1 解析和领域单测

- v4 firing/resolved 正常解析；
- 非法 version/status、空 groupKey、空 alerts、超过 50 条被拒绝；
- 白名单、截断、origin 清洗和稳定 SHA-256；
- Payload owner/knowledgeBase/executionPermitted 不产生权限效果；
- 多值 allowedLabels 的匹配和 filtered 结果。

### 14.2 PostgreSQL 与并发测试

- 相同 groupKey 并发 20 次只创建一个 active Incident；
- 只产生一个 Diagnostic Task 和 Background Job；
- 重复 firing 更新计数且不增加模型任务；
- 稳定 Event ID 去重；
- resolved 后再次 firing 新建生命周期；
- resolved/firing 竞争结果符合事务提交顺序；
- 唯一索引竞争后 Session 可继续使用；
- Redis 不可用时 PostgreSQL 幂等仍成立。

### 14.3 API 与安全测试

- Bearer Token 成功、缺失、错误和格式错误；
- 未知/禁用 Source；
- Content-Length 与流式实际字节双重大小限制；
- 422 Schema 响应；
- filtered、duplicate、resolved、orphan resolved 安全响应；
- PostgreSQL 失败 503；
- Runtime 唤醒失败仍返回 202；
- 响应、日志和持久化不含敏感内容；
- Nginx 独立限流和 Secret File 配置契约。

### 14.4 端到端验收

使用现有 `publish_java_ecommerce_alerts.py` 发布 firing，确认：

1. Alertmanager 调用 Webhook；
2. 两秒内返回 202；
3. Incident 为 active；
4. Diagnostic Task 和 Background Job 已创建；
5. LangGraph 运行并持久化报告；
6. 重复 firing 不创建第二个任务；
7. resolved 关闭 Incident，不增加 LLM 调用；
8. resolved 后新 firing 创建新生命周期；
9. Redis 停止时重复 firing 仍安全去重；
10. cleanup 后无遗留测试 Incident 或 Job。

## 15. 验收标准

- 首次合法 Webhook 在 2 秒内返回 202；
- 同一 groupKey 并发 20 次只有一个 Diagnostic Task；
- 重复、filtered、resolved 不增加不必要的 LLM 调用；
- Redis 不可用时正确性不变；
- PostgreSQL 失败返回 503；
- 敏感字段审计 0 泄漏；
- 自动触发不能绕过恢复审批与 Policy Gate；
- 新增聚焦 pytest、Ruff 和 Pyright 全部通过；
- 本阶段不要求全量 pytest 或全量 Benchmark。

## 16. 实施与发布顺序

1. 先实现纯解析器、配置和领域状态；
2. 再实现 PostgreSQL Schema、Repository 和并发幂等；
3. 接入现有 Diagnostic Task 与 Background Job；
4. 添加 Redis 短租约与降级；
5. 挂载 API、指标和 Nginx；
6. 更新 Alertmanager receiver、开发文档和发布脚本；
7. 完成离线聚焦回归；
8. 最后执行本地 Alertmanager 真实端到端验收。

生产部署默认保持 Source `enabled=false`，配置服务账号、知识库和 Secret 后再显式启用。
