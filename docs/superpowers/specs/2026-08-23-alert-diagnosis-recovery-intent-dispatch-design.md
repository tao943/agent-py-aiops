# 告警诊断自动派生 RecoveryIntent 设计

**日期：** 2026-08-23  
**范围：** 只把 Alertmanager 自动创建的诊断任务接入正式 Production Recovery 控制面。

## 目标

当 Alertmanager firing 告警创建的 AIOps 诊断成功并持久化 Report 与 Evidence 后，系统自动
调用现有 `RecoveryIntentService`：低风险且显式允许自动恢复的 Compose 提案进入 `queued`，
PostgreSQL blocker 提案进入 `awaiting_approval`。手动诊断、Chat 发起的诊断和没有充分证据的
诊断不得通过该入口自动创建可执行恢复。

本变更只连接两个已有子系统，不新增恢复执行器、工作流平台、模型调用或外部服务。

## 复用评估

### 当前项目

- 直接复用 `AlertIngestionService` 与 `SQLAlchemyAlertIngestionRepository` 创建 Incident、
  Diagnostic Task 和 `aiops_diagnosis` Job 的同一事务。
- 直接复用 `BackgroundJobRuntime` 的租约、重试、超时和失败语义。
- 直接复用 `RecoveryIntentService.create_result()` 的 owner scope、事实派生、策略门与
  PostgreSQL 幂等约束。
- 直接复用 `production_recovery` Worker；本变更不暴露生产写工具。
- 直接复用 Background Job Event 记录自动派生结果，不新增审计数据库表。

### GitHub 参考

- Robusta（MIT）的 Prometheus trigger → remediation playbook 表明告警事件与恢复动作之间
  应保留显式策略门。
- StackStorm/st2（Apache-2.0）的 trigger → rule → action 把触发来源、规则判断和动作执行
  分成独立边界。
- Argo Events（Apache-2.0）的 sensor → trigger 与 retry policy 说明触发交付应具备稳定身份和
  可恢复调度。

三者均只作架构参考。直接引入会形成第二控制面、额外服务和供应链负担，而本项目已经具备
durable Job、规则门、Intent 与执行协调器，因此不新增依赖。

## 方案选择

选择在现有 `aiops_diagnosis` Job 成功尾部派生 Intent。Job 只有在派生动作收敛后才标记成功；
Worker 崩溃或数据库暂时不可用时，原 Job 负责重试。若 Diagnostic Task 已经是 `succeeded`，
重试路径跳过 Agent/LLM，只执行 Intent 补偿派生。

不选择独立 dispatch Job，因为诊断提交与第二个 Job 入队之间需要新的跨事务恢复协议；不选择
通用 Outbox，因为会扩大所有诊断写入路径和消费者架构，超出本次连接需求。

## 架构与组件

### 1. 服务端触发来源标记

`SQLAlchemyAlertIngestionRepository` 创建 Diagnostic Task 时，在现有 `input_payload` 顶层写入：

```json
{
  "triggerSource": "alertmanager"
}
```

该字段由告警持久化层覆盖写入，不能从 webhook payload、客户端诊断请求、Prompt 或模型输出
透传。`schedule_for_incident()` 创建的人工/Chat 诊断不写该标记。

第一版只接受精确值 `alertmanager`，不设计可扩展字符串注册表。

### 2. AutoRecoveryIntentDispatcher

新增聚焦服务：

```python
class AutoRecoveryIntentDispatcher:
    async def dispatch(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
    ) -> AutoRecoveryDispatchResult: ...
```

它读取 owner-scoped Diagnostic Task，只承担入口资格和结果分类：

1. Task 不存在：返回 `task_unavailable`，调用方按不可恢复错误处理；
2. `triggerSource != "alertmanager"`：返回 `not_alert_triggered`；
3. Task 不是 `succeeded`：返回 `diagnostic_not_succeeded`；
4. 调用 `RecoveryIntentService.create_result()`；
5. `RecoveryIntentNotEligible`：返回 `proposal_not_eligible`，不创建旁路执行；
6. 成功时返回 `created` 或 `reused`、Intent ID 和公开 status。

Dispatcher 不读取 benchmark scenario、ground truth、PID、Compose 路径或 SQL，不判断 action，
也不自行启动执行器。所有恢复资格继续由 `RecoveryIntentService`、`RecoveryProposalAdapter` 和
`RecoveryPolicy` 决定。

### 3. aiops_diagnosis Job 接入

`_aiops_job_handler()` 调整为两个阶段：

```text
读取 Task
├─ Task 尚未 succeeded：运行现有诊断流
└─ Task 已 succeeded：跳过诊断流（Job 崩溃后的补偿路径）
        ↓
调用 AutoRecoveryIntentDispatcher
        ↓
追加公开安全 Job Event
        ↓
handler 返回，Background Job 标记 succeeded
```

正常诊断结束后必须重新读取 Task。只有最终状态是 `succeeded` 才调用 Dispatcher。`failed`、
`cancelled` 或缺失 Task 保持现有失败/取消语义。

如果派生创建遇到未分类的数据库或运行时错误，handler 抛出异常，让 durable Job 按现有策略
重试。重试看到已成功的 Task 后不得再次运行 Agent、RAG、MCP 或 LLM，只补偿派生 Intent。

### 4. 安全 Job Event

每次派生收敛追加一个 Job Event：

```json
{
  "type": "recovery.intent.dispatch",
  "outcome": "created | reused | skipped",
  "reasonCode": "not_alert_triggered | diagnostic_not_succeeded | proposal_not_eligible | null",
  "intentId": "公开 Intent ID 或 null",
  "status": "公开 Intent status 或 null"
}
```

Event 不包含 trusted snapshot、提示词、工具原始输出、凭据、PID、DSN、SQL、Compose 路径或
异常正文。意外异常继续由 Background Job 使用已有安全错误摘要记录，不复制到公开事件。

## 数据流

```text
Alertmanager webhook
→ Alert ingestion transaction
   → active Incident
   → Diagnostic Task(triggerSource=alertmanager)
   → aiops_diagnosis Job
→ Agent/LLM diagnosis
   → persisted Report + Evidence
   → Task succeeded
→ AutoRecoveryIntentDispatcher
   → RecoveryIntentService
      → no grounded proposal: skipped
      → Compose allowlisted + auto enabled: queued
      → PostgreSQL blocker: awaiting_approval
→ existing production_recovery Worker
→ at-most-once side effect
→ bounded independent verification
→ recovered / safe terminal state
```

## 幂等与崩溃恢复

- 同一 owner、Incident、Task、Report、action、target 和 Evidence 产生稳定 proposal fingerprint。
- `RecoveryIntentService` 与数据库部分唯一索引收敛并发或重复派生为同一个 active Intent。
- Intent 创建成功但 `aiops_diagnosis` Job 尚未标记成功时发生崩溃，Job 重试只调用 Dispatcher，
  返回 `reused`，不重新运行诊断。
- Compose Intent 创建为 `queued` 时已原子创建 `production_recovery` Job；当前 runtime 继续轮询
  即可获取，不需要裸 `asyncio.create_task()`。
- PostgreSQL Intent 只进入 `awaiting_approval`，自动告警入口不能审批。

## 配置语义

不新增配置字段：

- `productionRecovery.enabled=false` 仍保证无副作用；
- Compose 仍要求 target `automaticRecoveryEnabled=true`；
- PostgreSQL 仍始终要求 owner 审批；
- 没有匹配 selector 或充分 Evidence 时不产生可执行 Intent。

Alertmanager 来源标记只表示“允许尝试派生”，不代表授权恢复。

## 错误处理

| 情况 | 结果 |
| --- | --- |
| 手动或 Chat 诊断 | `skipped/not_alert_triggered`，无自动 Intent |
| 诊断 failed/cancelled | 不派生，保持原诊断终态 |
| 无匹配白名单或证据不足 | `skipped/proposal_not_eligible` |
| Recovery 全局关闭 | 由现有策略产生非执行终态，零副作用 |
| Intent 已存在 | `reused`，不创建第二个恢复 Job |
| 派生阶段数据库暂时失败 | 原 `aiops_diagnosis` Job 重试，跳过 LLM |
| Intent 已创建后 Worker 崩溃 | 由现有 production recovery 幂等和租约恢复 |

## 测试设计

### 单元与集成测试

1. Alert ingestion 创建的 Task 带服务端 `triggerSource=alertmanager`；手动 Incident 调度不带。
2. Dispatcher 对手动、未完成、不合格、创建成功和重复 Intent 返回稳定分类。
3. `aiops_diagnosis` 首次成功后调用 Dispatcher 并追加安全 Event。
4. 模拟 Intent 已创建后 handler 崩溃；重试不得再次调用 Diagnostic Runner/LLM，只返回 reused。
5. 非 Alertmanager 诊断保持当前行为，Dispatcher 不调用 RecoveryIntentService。
6. PostgreSQL 自动派生只得到 `awaiting_approval`，没有 approval 或执行调用。
7. 事件递归检查禁止敏感键和值。

### 真实验收

扩展隔离 Order Pool 自动告警场景，要求从 Alertmanager webhook 创建的 Task 自动产生正式
RecoveryIntent，而不是旧 Live Eval recovery execution key。验收关联：

```text
Incident ID
→ Diagnostic Task ID
→ Report/Evidence IDs
→ formal RecoveryIntent ID
→ production recovery execution key
→ recovered verification/audit
```

重复同一告警交付和诊断 Job 重投不得产生第二次容器重启。真实验收继续只操作
`live-eval-order-api`，不自动恢复 PostgreSQL。

## 验收标准

- 只有 Alertmanager 自动创建的成功诊断尝试自动派生 Intent。
- 手动与 Chat 诊断的现有行为不变。
- 自动派生不新增恢复工具或绕过策略门。
- 崩溃重试不重复调用 LLM，也不创建第二个 active Intent 或恢复 Job。
- Compose 可进入正式 `queued → ... → recovered` 链路。
- PostgreSQL 只进入 `awaiting_approval`。
- 所有公开事件不泄露敏感信息。
- 聚焦 pytest、Ruff、strict Pyright 和 OpenSpec validation 通过。
