# 生产恢复执行闭环设计

**日期：** 2026-08-23  
**状态：** 已确认，待实施计划  
**关联设计：** `2026-08-23-aiops-workbench-frontend-redesign-design.md`

## 1. 背景

当前 AIOps LangGraph 能完成证据调查、根因决策、恢复建议、Validator 与 Policy Gate，但正式
运行链路只记录 proposal，`executionPermitted=false`。真正的副作用执行只存在于隔离的 Live
Eval 驱动中；其授权依赖测试框架掌握的 scenario、run、注入 PID、容器和 ground truth，不能
直接暴露为生产恢复接口。

本设计将 Live Eval 已验证的“确定性授权 → 幂等执行 → 独立验证 → 安全清理/转人工”模式
产品化。生产恢复不读取 benchmark oracle，不允许模型或前端指定任意命令、Compose 路径、
服务名、数据库地址或 PID。

## 2. 目标

- 诊断 LangGraph 完成后生成不可变 `RecoveryIntent`；
- 低风险、显式启用的 Compose 服务重启可以自动进入执行；
- PostgreSQL blocker 终止必须由 Incident owner 人工批准；
- 审批、重新验证、执行、结果判定与恢复验证全程持久化；
- 网络中断、Worker 重启或重复请求不会重复副作用；
- 结果未知、验证失败或安全条件变化时立即转人工；
- 前端在同一 Incident 时间线展示诊断与恢复，但不能绕过服务端状态机。

## 3. 非目标

- 不允许任意 MCP 写工具进入第一版生产恢复白名单；
- 不允许 LLM 决定审批、目标身份或最终执行权限；
- 不从 Live Eval 复制 scenario/oracle 判断到生产运行；
- 不实现 Kubernetes、SSH、云厂商运维命令或通用脚本执行器；
- 不自动重试副作用，不自动切换到第二种恢复动作；
- 不实现双人审批、组织级 RBAC 或值班排班；
- 不承诺撤销服务重启或数据库会话终止这类不可逆动作。

## 4. Reuse-first 评估

### 4.1 项目内直接复用

- `AiopsDiagnosticService` 的 Evidence、Decision、Recovery Plan、Validator 与 Policy Gate；
- `ExecutionCoordinator`、`AiopsExecutionRepository` 与 `execution_kind="recovery"` 幂等语义；
- `BackgroundJobRuntime` 的持久 job、lease、重试唤醒与 Worker 恢复；
- Alert Incident 生命周期和 verification 字段；
- Live Eval 的 `ComposeServiceRestarter`、PostgreSQL Recovery Policy 与 Auto Closure，仅提取
  不依赖 oracle 的边界和验证思想，不直接复用 benchmark 类型；
- owner-scoped Repository、统一 API envelope、错误目录、Rate Limiter 和 Tool Audit。

### 4.2 GitHub 调研

| 候选 | 许可证/状态 | 可复用点 | 决策 |
|---|---|---|---|
| `StackStorm/st2` | Apache-2.0，2026-08 活跃 | Live Action 状态、参数脱敏、执行记录与策略重试 | 仅参考；整套 Mongo/消息系统过重 |
| `robusta-dev/robusta` | MIT，2026-08 活跃 | 告警触发的显式 remediation playbook 与有界 job | 仅参考；Kubernetes 专用 |
| `argoproj/argo-workflows` | Apache-2.0，2026-08 活跃 | suspend/resume 与人工干预状态 | 仅参考；引入第二工作流引擎不合适 |

最终选择：包装现有 PostgreSQL Background Job 与 Execution Coordinator，自定义有界恢复状态机；
不新增第三方依赖或外部自动化服务。

## 5. 架构边界

```text
Alertmanager Incident
  -> Diagnostic Task / LangGraph
  -> persisted Report + Recovery Plan + Policy Audit
  -> Production Recovery Service creates immutable RecoveryIntent
  -> deterministic eligibility gate
       -> awaiting_approval
       -> queued (low risk + explicit auto enable)
  -> durable Background Job
  -> preflight revalidation
  -> Recovery Executor Registry
       -> ComposeRestartExecutor
       -> PostgresBlockerTerminationExecutor
  -> independent Verification Service
  -> Incident recovery state + append-only audit
```

诊断 LangGraph 不等待审批，也不持有生产写工具。恢复状态机通过稳定的 Diagnostic/Report/
Evidence/Proposal 引用衔接，并在 UI 中与诊断事件合并展示。

## 6. Recovery Intent

`RecoveryIntent` 是一次不可变的生产恢复意图，至少包含：

- `id`、`owner_user_id`、`incident_id`、`diagnostic_task_id`、`report_id`；
- `action`、`target_key`、规范化参数与 `proposal_fingerprint`；
- `risk_tier`、`automatic_eligible`、`approval_required`；
- 证据 ID、Validator origin、Policy authorization code；
- `status`、`execution_key`、Background Job ID；
- 审批有效期、创建/开始/完成时间和公开失败码；
- 执行前事实快照、公开执行摘要和验证摘要。

Intent 创建时重新读取 owner-scoped 持久 Report/Evidence。客户端只提交 Diagnostic ID 和
可选人类说明，不能提交 action、target、PID、Compose 路径或执行权限。

稳定指纹：

```text
proposal_fingerprint = sha256(
  owner_user_id + incident_id + diagnostic_task_id + report_id
  + action + target_key + canonical_arguments + sorted_evidence_ids
)
```

同一 proposal fingerprint 最多存在一个未终结 Intent。提案变化必须创建新的 Intent，旧审批
不继承。

## 7. 状态机

```text
proposed
  -> awaiting_approval
  -> queued
  -> denied

awaiting_approval
  -> queued      (owner approve, <= 10 minutes)
  -> rejected
  -> expired

queued
  -> revalidating
  -> cancelled   (execution lease acquired前)

revalidating
  -> executing
  -> manual_intervention

executing
  -> verifying
  -> manual_intervention

verifying
  -> recovered
  -> verification_failed
```

非法状态转换使用稳定错误码拒绝。审批只允许当前 Incident owner；需要输入完整 Incident ID
二次确认。批准记录绑定 proposal fingerprint，10 分钟后过期。批准不等于无条件执行，Worker
获取 job 后必须重新执行全部安全检查。

## 8. 配置与安全默认值

配置继续来自 `config/project.json` 与被忽略的 `config/user.project.json`：

```json
{
  "productionRecovery": {
    "enabled": false,
    "approvalTtlSeconds": 600,
    "composeTargets": [],
    "postgresTargets": []
  }
}
```

提交模板中 `enabled=false` 且白名单为空。Compose target 明确配置 project-owned Compose 文件
标识、固定服务名、健康 URL、业务探针和 `automaticRecoveryEnabled`。实际绝对路径只在服务端
解析并验证位于允许的项目目录中，不出现在 API、日志和审计摘要中。

PostgreSQL target 引用已有服务端数据库配置标识，不接受连接字符串。API 和审计不得显示凭据、
主机详情或原始 SQL。

## 9. Compose 服务重启执行器

第一版动作名固定为 `restart_compose_service`。

### 9.1 自动执行硬门

必须同时满足：

- production recovery 全局开启；
- target 在服务端 Compose 白名单中；
- target 显式 `automaticRecoveryEnabled=true`；
- Diagnostic 成功且 Report 已持久化；
- Evidence sufficiency 为 sufficient；
- 确定性 Validator 通过，允许的 semantic origin 不改变安全事实；
- Recovery Plan action/target 与白名单定义精确匹配；
- 没有同 Incident 的 active Recovery Intent；
- 当前容器身份与 preflight 事实一致；
- execution key 未完成、未失败且未处于 unknown outcome。

缺失任一条件时不自动执行：可审批的情况进入 `awaiting_approval`，身份或证据不一致进入
`manual_intervention`，配置关闭显示 `recovery_disabled`。

### 9.2 执行

调用固定参数数组：

```text
docker compose -f <server-resolved allowlisted file> restart <allowlisted service>
```

不使用 shell 字符串、不拼接用户输入、不接受额外 flags。子进程有固定 timeout；超时后终止
进程树并把结果标记为 unknown，绝不自动补执行。

### 9.3 验证

同时要求：

1. 容器 ID 或启动时间相对 preflight 发生变化；
2. 配置的服务健康检查成功；
3. 有界业务探针成功；
4. 关联 Alertmanager Incident 在验证窗口内 resolved。

任一失败进入 `verification_failed`，不再次重启。

## 10. PostgreSQL blocker 终止执行器

动作名固定为 `terminate_postgres_blocker`，风险等级为 high，始终需要人工批准。

### 10.1 执行前硬门

- production recovery 全局开启；
- target 在 PostgreSQL 恢复目标白名单中；
- owner 审批存在、fingerprint 匹配且未超过 600 秒；
- PID 来自执行器自己的 fresh database probe，不采用模型/前端参数；
- 目标 PID 仍存在且 backend type 为允许的 client backend；
- 目标仍阻塞 Diagnostic 证据关联的 waiter；
- 目标不是 waiter、恢复执行连接、系统后台进程或未识别连接；
- database identity、锁资源指纹和 blocker/waiter 关系与 proposal 匹配；
- 单个 Intent 只允许一个 target PID；
- execution key 未进入 completed 或 unknown outcome。

不满足任何条件都进入 `manual_intervention`。系统不会改选另一 blocker。

### 10.2 执行与验证

执行器使用参数化数据库调用终止目标 backend。随后同时验证：

1. blocker PID 已消失；
2. waiter 已继续或原事务已结束；
3. 锁等待指标恢复；
4. 关联 Alertmanager Incident resolved。

终止会话不可回滚；验证失败时不再终止其他 PID。

## 11. 幂等、租约与未知结果

执行身份：

```text
execution_key = sha256(
  recovery_intent_id + action + target_key + proposal_fingerprint
)
```

使用现有 `AiopsExecutionRepository` 的唯一约束作为最终正确性保障，`execution_kind="recovery"`
且 `side_effecting=true`。Background Job 可重试调度，但 side-effect handler 按以下规则处理：

- completed：读取原结果，不再执行；
- 未获取执行租约：可由新 Worker 获取；
- 明确 preflight failure：不执行并进入人工；
- 执行开始后异常且无法确认结果：记录 unknown outcome，进入人工；
- 明确未执行的基础设施失败：终结当前 Intent，由用户创建新 Intent；
- 不自动创建第二 attempt，不使用 Redis 作为幂等真相源。

## 12. 审批与审计

审批记录包含 approver user ID、proposal fingerprint、Incident ID 确认摘要、批准/拒绝时间和过期
时间。不得记录密码、token、完整 Prompt 或自由格式秘密。

每次状态转换追加 `recovery_audit_events`，使用稳定 `event_id` 去重。公开事件仅包含：动作类型、
target key、前后状态、授权/拒绝码、执行耗时、公开结果与验证检查。服务端日志可记录安全错误
类别，但不能记录连接串、Compose 绝对路径、SQL、原始异常或工具输出。

## 13. API 与前端行为

owner-scoped API：

```text
POST /aiops/diagnostics/{task_id}/recovery-intents
GET  /aiops/recovery-intents/{intent_id}
POST /aiops/recovery-intents/{intent_id}:approve
POST /aiops/recovery-intents/{intent_id}:reject
POST /aiops/recovery-intents/{intent_id}:cancel
GET  /aiops/recovery-intents/{intent_id}/events
```

approve 请求只包含完整 Incident ID confirmation。创建 Intent 后由服务端选择动作、目标和风险。
前端恢复页展示安全门、审批倒计时、执行状态、验证检查与人工介入原因。自动恢复默认关闭或目标
未启用时必须明确显示，不能把“未执行”渲染成“等待系统恢复”。

## 14. 测试

### 14.1 单元与策略测试

- 每个硬门的通过/拒绝分支；
- 配置默认关闭和空白名单；
- 模型/客户端 action、PID、路径注入无效；
- proposal fingerprint、审批 TTL 和状态转换；
- 公开审计 allowlist 与敏感字段拒绝。

### 14.2 PostgreSQL 集成测试

- 并发创建同一 Intent 只产生一个 active record；
- 并发审批、过期与 Worker 获取的原子转换；
- Background Job/Intent/Audit 原子创建；
- execution key 冲突安全读取；
- Worker lease 过期恢复但不重放 side effect；
- owner 隔离和跨租户 not-found；
- migration upgrade/downgrade 不破坏现有 approval 数据。

### 14.3 执行器测试

- Compose 固定 argv、路径边界、服务白名单、timeout、进程树终止；
- 容器身份变化、health、业务探针、告警 resolved 四重验证；
- PostgreSQL fresh probe、PID/连接/锁关系校验和参数化终止；
- unknown outcome 转人工且执行次数保持 1；
- 验证失败不产生第二次副作用。

### 14.4 Live 与 UI 验收

- 复用隔离 `live-eval-order-api` 证明自动重启闭环；
- 在 PostgreSQL Live fixture 中由真实 owner 审批后终止精确 blocker；
- 前端桌面/窄屏均能查看 Intent、审批、执行和验证；
- 非 owner、过期审批、错误 Incident ID、重复点击和刷新重连均安全失败或收敛；
- 最终审计能从 Recovery Intent 追溯 Diagnostic、Evidence、Report、Approval、Execution 和
  Verification。

## 15. 成功标准

- 诊断 Agent 与前端都无法直接调用生产写工具；
- 自动执行只发生在全局开启且 target 显式允许的低风险 Compose 服务；
- PostgreSQL blocker 终止始终需要当前 owner 的有效审批；
- 所有目标在执行前使用 fresh trusted facts 重验；
- side effect 在并发、超时、断线和 Worker 重启下最多发生一次；
- unknown outcome、验证失败和安全条件变化均转人工，不自动重试；
- 恢复成功必须通过动作后置条件、健康/业务检查和 Alertmanager resolved；
- UI 与持久审计呈现一条完整、可复现且不泄露敏感数据的 Incident 闭环。
