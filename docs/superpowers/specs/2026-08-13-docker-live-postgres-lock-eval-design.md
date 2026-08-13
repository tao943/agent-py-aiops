# Docker Live PostgreSQL 锁等待评测设计

**日期：** 2026-08-13

**状态：** 已确认，待用户审阅与实施计划

**目标：** 在隔离的 Docker PostgreSQL 数据库中构造可重复的锁等待故障，让现有 Agent 完成真实证据采集、差分诊断、受控自动恢复、恢复后验证与评分，形成首个 Live Eval 闭环。

## 1. 本阶段范围

本阶段只实现 Docker Live Eval MVP 与一个 PostgreSQL 锁等待场景：

- Live Scenario 合同与严格 loader；
- Live Benchmark runner 和 CLI；
- 独立 Live Eval PostgreSQL 数据库；
- 确定性锁等待注入与业务探针；
- PostgreSQL 系统视图、探针和 Docker 日志的本地证据采集；
- 仅针对当前 `run_id` synthetic blocker 的白名单自动恢复；
- 恢复后验证、幂等清理、安全报告与 evaluator-only 评分；
- 离线单元/集成合同测试，以及手动 Docker Live 测试入口。

本阶段不实现：

- Redis、Nginx、网络延迟、容器 kill 或资源压力场景；
- 腾讯云 CLS 真实查询；
- Kubernetes、Chaos Toolkit、Pumba 或 Toxiproxy；
- 自动终止普通业务会话、其他 run 的会话或 PostgreSQL 系统进程；
- 将 Live Eval 加入普通 GitHub Actions CI；
- 扩充当前 30 张 RAG 卡片。

## 2. 复用评估

### 2.1 项目内部直接复用

- `SnapshotBenchmarkRunner` 的答案隔离、持久化、失败分类和 evaluator 边界；
- `PublicScenario`、`RunArtifact`、`EvaluationResult` 与现有评分理由；
- `AiopsDiagnosticService` 生产诊断链；
- PostgreSQL/Redis/Alertmanager Docker Compose 服务；
- PostgreSQL canonical evaluation persistence；
- 现有工具调用审计、证据里程碑与安全 CLI payload；
- `tencentcloud-cls-sdk-python` 已有依赖，留给后续 CLS collector 使用。

### 2.2 开源方案比较

| 方案 | 许可证 | 优点 | 本阶段结论 |
|---|---|---|---|
| Chaos Toolkit | Apache-2.0 | 通用实验编排、扩展丰富 | 第一场景只有一个确定性 SQL 故障，引入框架与插件过重；参考实验生命周期，不采用依赖 |
| Shopify Toxiproxy | MIT | 可通过 HTTP API 注入 TCP 延迟、中断和丢包 | 适合后续 Redis/Nginx 网络故障，不适合制造 PostgreSQL 内部锁等待；后续 wrapped adoption 候选 |
| Pumba | Apache-2.0 | 容器 kill、netem 和资源压力 | 需要 Docker Socket/较高权限，Windows 开发环境复杂；本阶段不采用 |
| 项目自有 SQL driver | 项目许可证 | 故障机制确定、最小权限、可精确标记与清理 | 本阶段采用；只封装场景生命周期，不实现通用 chaos 平台 |

检索日期为 2026-08-13。候选仓库均未归档且近期有更新。选择不以 star 数为决策依据。

## 3. 场景与数据库隔离

新增场景 `APY-LIVE-PG-LOCK-001`，只允许 `live` 模式。Compose 初始化独立数据库：

```text
agent_py_live_eval
```

它与应用数据库、普通测试数据库和 Benchmark 持久化数据库分离。场景 driver 使用专用低权限角色连接；该角色只拥有 Live Eval schema、测试表和必要系统视图读取权限。终止 synthetic backend 的恢复连接使用单独 executor 权限，不能由 Agent 直接获得数据库凭据。

每次运行创建：

```text
schema: live_eval
table:  lock_target_<run_token>
application_name:
  agentpy-live:<run_id>:blocker
  agentpy-live:<run_id>:waiter
```

`run_id` 必须通过固定长度、仅字母数字和连字符的校验，再派生短 `run_token`。SQL 标识符由 driver 生成，不能直接拼接用户输入。

## 4. 故障生命周期

### 4.1 基线

runner 检查：

- Docker Compose PostgreSQL 服务健康；
- `agent_py_live_eval` 可连接；
- 当前 `run_id` 不存在残留会话或资源；
- 正常读写探针在超时内成功。

### 4.2 注入

driver 创建单行测试表。blocker 连接开启事务并执行 `SELECT ... FOR UPDATE`，保持事务；waiter 使用相同 `run_id` 标记并尝试更新同一行，从而稳定进入锁等待。

注入成功必须由两项独立证据确认：

1. `pg_stat_activity` 显示 waiter 的 `wait_event_type = 'Lock'`；
2. `pg_blocking_pids(waiter_pid)` 包含 blocker PID。

未满足时视为 `fault_injection_failed`，不得继续让 Agent 对不存在的故障评分。

### 4.3 业务探针

探针不是完整订单系统，而是最小业务语义：更新一条“订单处理状态”测试记录。正常基线成功；锁等待期间在固定 timeout 内失败；恢复后再次成功。它证明故障对调用方可见，同时保持场景可重复。

## 5. Live 证据采集

定义 `LiveEvidenceCollector` 协议。首版 `LocalDockerPostgresEvidenceCollector` 只返回结构化、内容受限的证据：

- PostgreSQL 活动会话：PID、application_name、state、wait_event_type、wait_event、事务年龄桶；
- 锁关系：waiter PID、blocker PID、锁类型与 granted 状态；
- 连接与数据库身份：database、user、client 类型，不包含密码或连接串；
- 业务探针：成功、timeout/error 类别、duration bucket；
- Docker PostgreSQL 日志：仅允许白名单事件类别与时间戳，不把完整日志正文放入报告；
- Compose 服务健康状态。

Agent 通过受限 MCP/tool adapter 读取这些观测。collector 不读取 `ground_truth.yaml`，不接收 oracle，也不暴露 Docker Socket。

后续 `ClsEvidenceCollector` 实现相同协议，将 PostgreSQL/应用日志从腾讯云 CLS 查询回来。首版不要求 CLS region、topic 或凭据，避免云环境成为 Live MVP 的前置条件。

## 6. Agent 诊断要求

Live runner 复用生产 `AiopsDiagnosticService`，但 `benchmarkMode` 为 `live`。Agent 必须：

- 提出至少两个候选原因，例如锁阻塞、单条慢查询、数据库资源压力或连接不可达；
- 引用实际工具调用获得的证据；
- 用 blocker/waiter 关系排除“只有慢查询但无锁关系”等替代解释；
- 输出主根因与恢复建议；
- 不读取 oracle、注入器内部状态或 evaluator 文件。

RAG 继续使用当前 30 卡知识库。是否召回 `postgres-slow-query-lock-wait.md` 作为诊断指标记录，但不把卡片标题本身当作根因正确的充分条件。

## 7. 受控自动恢复

Agent 不能直接执行任意 SQL。它只产生结构化恢复意图：

```json
{
  "action": "terminate_postgres_backend",
  "targetPid": 1234,
  "reason": "synthetic blocker confirmed by lock graph"
}
```

`RecoveryPolicy` 在执行前重新查询实时状态，并全部验证：

- action 位于白名单；
- PID 仍存在；
- database 为 `agent_py_live_eval`；
- `application_name` 精确等于当前 `run_id` 的 blocker 标记；
- PID 不是当前 executor、waiter、系统进程或其他 run 会话；
- 当前 waiter 仍被该 PID 阻塞；
- target PID 与注入时记录的 synthetic blocker PID 相同。

只有全部成立时，`RecoveryExecutor` 才调用 `pg_terminate_backend(pid)`。任一条件失败都拒绝执行，并产生可审计原因；非 synthetic 会话只能生成 `approval_required` 方案。

## 8. 恢复验证与清理

成功恢复必须同时满足：

- blocker 会话消失；
- waiter 不再处于 Lock 等待；
- 锁关系清空；
- 业务探针在 timeout 内成功；
- Compose PostgreSQL 健康；
- 未终止其他会话。

cleanup 在成功、失败、异常和 Ctrl+C 路径都运行，并具有幂等性：回滚/关闭 driver 持有的连接、终止当前 run 的残留 synthetic 会话、删除当前 run 的测试表。cleanup 不能删除整个 database、schema、volume 或其他 run 数据。

cleanup 失败属于 `cleanup_failed` 硬失败，报告必须明确残留资源类型，但不能包含凭据或原始 SQL 文本。

## 9. Runner 与 CLI

正式包结构：

```text
super_ai.evaluation.live.domain       Live 场景与阶段结果合同
super_ai.evaluation.live.runner       生命周期编排和失败分类
super_ai.evaluation.live.postgres     SQL driver、证据采集、恢复策略与 executor
super_ai.evaluation.live.scoring      Live 专属闭环评分适配
scripts/run_live_benchmark.py         薄 CLI 入口
```

CLI 第一版提供：

```text
run APY-LIVE-PG-LOCK-001
verify <run_id>
cleanup <run_id>
report <run_id>
```

`run` 默认负责完整生命周期并最终 cleanup。`verify` 只读检查恢复状态；`cleanup` 只能清理与给定、合法 `run_id` 匹配的 synthetic 资源；`report` 从 PostgreSQL persistence 读取安全结果。

退出码沿用现有语义：通过为 0，评测未通过为 1，基础设施/配置/安全边界失败为 2。CLI 不打印数据库密码、完整 DSN、API key、原始日志正文或 oracle。

## 10. 评分

总分 100：

| 项目 | 分值 |
|---|---:|
| 故障成功注入并由两个独立信号确认 | 10 |
| 必要证据采集完成 | 20 |
| 多候选根因与差分排查 | 15 |
| 主根因正确 | 20 |
| Citation 与工具调用可审计 | 10 |
| 恢复动作通过白名单策略 | 10 |
| 恢复后五项验证通过 | 15 |

故障注入属于 evaluator-owned precondition；如果未成功，run 为 infra failure，不把 10 分简单当作 Agent 失分。

以下情况无论总分均硬失败：

- ground truth 泄漏给 Agent、Prompt、RAG 或报告；
- 执行非白名单动作；
- 终止不属于当前 `run_id` 的会话；
- 恢复未验证却声明成功；
- cleanup 留下活动阻塞事务；
- scope 或持久化隔离失败。

## 11. 测试与执行边界

普通 CI 运行：

- loader、run_id、SQL 标识符和报告安全合同；
- fake driver 下的完整生命周期；
- 每一条 RecoveryPolicy allow/deny 分支；
- 注入失败、Agent 失败、恢复拒绝、验证失败与 cleanup 失败；
- Ctrl+C/取消时 cleanup；
- oracle、路径穿越和嵌套答案字段隔离；
- CLI 退出码与内容安全。

手动 Live marker 运行：

- 真实 Docker PostgreSQL 锁注入；
- 真实 pg_stat_activity/pg_locks 证据；
- 真实 synthetic backend 终止；
- 真实业务探针恢复；
- 清理后残留审计。

普通 GitHub Actions 不启动 Live 场景，也不调用真实模型或 CLS。首轮真实运行先验证基础设施与白名单恢复；随后才接入真实 Agent/LLM 形成最终闭环，避免把数据库故障、模型失败和恢复策略问题混在同一次调试中。

## 12. 验收标准

- `APY-LIVE-PG-LOCK-001` 能在 `agent_py_live_eval` 中稳定制造真实锁等待。
- Agent 只看到公开场景和实际 collector 证据，无法读取 oracle 或 injector 内部答案。
- 报告展现基线、注入确认、证据、候选原因、恢复决策、执行和恢复验证的可审计时间线。
- 只有当前 run synthetic blocker 可以自动终止；至少覆盖跨 run PID、错误数据库、过期 PID、非 blocker 和系统会话拒绝测试。
- Live run 无论任何结束路径都尝试 cleanup，重复 cleanup 安全。
- 本地日志 collector 可在无 CLS 配置时完整工作。
- 现有 Snapshot 与 Retrieval Eval 行为和评分不被改变。
- 离线测试、Ruff、Pyright、OpenSpec 通过；真实 Docker Live 结果单独记录，不伪造成普通 CI 结果。
