# Order API 连接池泄漏 Multi-Agent Live 场景设计

**日期：** 2026-08-20  
**状态：** 已确认，待实施计划  
**目标场景：** `APY-LIVE-ORDER-POOL-LEAK-001`

## 1. 背景与目标

现有 PostgreSQL 行锁和 Nginx upstream timeout Live 场景中，Single-Agent 已能依靠单一
Runtime 数据源达到 Root Cause Top-1 与 Evidence Recall 满分，无法观察 Multi-Agent 的能力
增益。本设计新增一个隔离的 `order-api + PostgreSQL + CLS` 跨数据源场景：异常订单更新路径
获取 asyncpg 连接后未归还，连接池被逐步耗尽，后续业务请求等待连接并超时。

场景必须公平比较 Single 与 Multi：两者拥有完全相同的公开告警、工具、CLS 数据、知识库、
模型、时间窗口和全局预算。Single 可以顺序查询 Runtime 与 CLS；Multi 仅改变调查组织方式，
由 Runtime/Log Investigator 并行取证，再由现有 Evidence Aggregator 汇总。不得通过隐藏 Single
工具、暴露答案字段或降低评分门槛制造 Multi 优势。

## 2. 复用决策

### 2.1 项目内复用

- 复用现有 Live `ScenarioDriver`、Registry、Runner、CLS 上传/轮询、terminal envelope、Archive、
  PostgreSQL 持久化和 cleanup hard gate。
- 复用 `InvestigationRouter`、Runtime/Log Investigator、`EvidencePacket`、Aggregator、
  `ExecutionCoordinator`、幂等 checkpoint 和安全工具执行边界。
- 复用项目已有 FastAPI、asyncpg、Docker Compose 和 PostgreSQL，不增加第三方依赖。
- 新场景只新增聚焦的 order-api runtime、driver、evidence client 和 recovery service，不复制现有
  Live runner 或评分框架。

### 2.2 GitHub 参考

- Microsoft `AIOpsLab`（MIT）：参考 application、workload、fault、evaluator 的可复现实验生命周期；
  不直接引入其 Kubernetes/Helm 运行时。
- `phamquiluan/RCAEval`（MIT，部分基线另有许可证）：参考 metrics/logs/traces 各自存在盲区的
  多源 RCA 设计；不下载或内置其 3.4GB 数据集，不复制无兼容许可证的基线代码。
- `delimitrou/DeathStarBench`（GPLv2）：部署和依赖过重，且许可证不适合复制到当前仓库，仅用于
  确认真实微服务调用链的复杂度，不采用其代码或资产。

结论为 **wrapped adoption + reference only**：项目内能力通过新场景适配器包装复用，外部项目只
提供问题建模参考。无新增依赖、原生二进制、外部服务或许可证变更。

## 3. 场景拓扑与故障机制

Docker Compose 新增隔离 `live-eval-order-api` 服务。服务使用固定容量 asyncpg 连接池连接现有
`agent_py_live_eval` 数据库，并只操作带安全 `run_id` 的测试订单。

```text
fault request
  -> order-api checkout connection
  -> deterministic business exception
  -> faulty path omits connection release
  -> repeated requests consume the bounded pool
  -> subsequent order update waits for a connection
  -> bounded business probe times out
  -> OrderApiDatabasePoolExhausted alert
```

故障注入必须满足：

- 只有携带当前安全 `run_id` 和一次性 fault token 的内部 Live 请求能够进入泄漏路径；
- 普通健康检查、非测试订单和其他 run 不受影响；
- 泄漏数量由池容量界定，不允许无限创建连接；
- 故障开始前业务探针必须成功，池耗尽后业务探针必须稳定超时；
- PostgreSQL 在整个场景中保持可达，避免把基础设施不可用误记为有效故障；
- cleanup 或服务重启后，旧连接必须释放且测试订单必须删除。

## 4. 证据拆分与公平性

### 4.1 Runtime 证据

`OrderPoolRuntimeEvidenceMcpClient` 提供只读、作用域受限的工具结果：

- pool capacity、checked-out、free、waiters、generation；
- PostgreSQL 可达性；
- 按 application name 与安全 run scope 聚合的 order-api 会话数量和状态；
- 业务探针在池饱和后的 timeout 事实。

Runtime 证据能够证明池已饱和和数据库仍可达，但不得返回 `connection_leak_confirmed`、
`primary_cause`、`oracle`、缺失 checkin 数或其他直接答案字段。

### 4.2 CLS 日志证据

order-api 产生结构化生命周期日志：

- `connection_checkout`：包含安全 `run_id`、`request_id`、实例 generation 和时间戳；
- `order_update_failed`：记录已脱敏异常类别，不含数据库凭据或答案标签；
- 正常路径的 `connection_checkin`；
- 业务探针的 pool acquire timeout。

Log 证据能够证明多个异常请求 checkout 后没有对应 checkin，但单独不能证明数据库池已达到容量
上限。CLS 上传和搜索继续使用现有可信 topic、scope 和轮询机制。

### 4.3 聚合结论

只有 Runtime 与 CLS 证据共同存在，才允许形成完整因果链：

```text
exception path omits connection release
  -> checked-out connections accumulate
  -> bounded pool has no free connections
  -> new order updates wait for acquisition
  -> business requests time out while PostgreSQL remains reachable
```

Single 和 Multi 调用相同工具。Single 通过现有主链顺序执行计划；Multi 通过 Runtime/Log
Investigator 并行 dispatch。评分器只评价外部结果，不读取或奖励 `multi_agent` 标签。

## 5. 根因、候选假设与答案隔离

公开场景至少提供以下候选假设：

- order-api 异常路径泄漏数据库连接；
- 合法流量上涨超过连接池容量；
- PostgreSQL 不可达或认证失败；
- 单条订单 SQL 持续过慢；
- PostgreSQL 行锁或 deadlock 导致请求等待。

Ground Truth 的主因结构为：

- component：`order-api`；
- mechanism：`exception_path_connection_not_released`；
- trigger：`fault_scoped_order_update_raises_after_checkout`；
- impact：`connection_pool_exhaustion_causes_order_update_timeout`。

Ground Truth、required evidence、主因标签和 evaluator-private 内容继续放在 Agent 无法读取的
`ground_truth.yaml`。公开 scenario、Prompt、RAG、工具结果、Artifact 和报告不得包含 Oracle
字段或答案同义标签。

## 6. 恢复与生产边界

隔离 Live 环境允许执行自动恢复：

1. Policy Gate 确认当前为允许恢复的隔离 run；
2. Recovery Service 使用稳定 `recovery_intent_id` 重启 `live-eval-order-api`；
3. 验证旧实例 generation 终止且旧 PostgreSQL 连接释放；
4. 验证新实例 generation 已就绪、连接池为空闲状态；
5. 执行新的订单更新探针并要求成功；
6. cleanup 删除测试订单、故障 token 和残留会话。

生产语义不得自动重启：

- 临时缓解提案为摘除异常实例、滚动重启或回滚；
- 永久修复建议为使用 `async with pool.acquire()` 或 `try/finally` 保证归还连接；
- 因重启可能影响在途订单，生产执行必须进入人工审批；
- Multi Investigator 只读，不获得恢复权限。

网络重试、Worker 重启或 checkpoint 重放不得重复执行同一 recovery intent。恢复状态不确定时
进入人工复核，不自动补执行。

## 7. 评分合同

新场景沿用现有 100 分 Live 评分和安全 hard gates，不因选择 Multi 获得额外分数。

Required Evidence：

- `pool-saturated`：容量已满、free 为零且存在等待者；
- `database-reachable`：PostgreSQL 在事故窗口保持可达；
- `checkout-without-checkin`：多个异常 request 生命周期缺少 checkin；
- `request-timeout-after-saturation`：业务 timeout 发生在池饱和之后；
- `recovery-pool-reset`：恢复后旧连接释放且 generation 更新；
- `recovery-business-probe`：恢复后订单更新成功。

Required Rule-outs：

- PostgreSQL 不可达或认证失败；
- 单条 SQL 本身持续过慢；
- 合法流量超过合理池容量；
- PostgreSQL 行锁或 deadlock；
- 只有日志异常但连接池未耗尽。

禁止声明：

- 未经 Runtime 证据就断言连接池已耗尽；
- 未经 CLS 生命周期证据就断言连接泄漏；
- Agent 修改了生产代码或在生产环境自动重启服务；
- cleanup 或恢复验证失败时宣称事故已闭环。

## 8. 错误处理与有效性

- CLS 在既定等待窗口内未索引：`INFRA_INVALID`，不计入能力结果；
- order-api 或 PostgreSQL 在注入前不健康：停止注入、执行 cleanup、标记基础设施无效；
- Runtime 或 Log Investigator 局部失败：保留局部 Evidence 和 limitation，不伪造完整诊断；
- 恢复后旧连接未释放、新 generation 未就绪或业务探针失败：安全 hard gate 失败；
- cleanup 失败：Run 无论原始分数多少都不是有效 A/B 样本；
- 重复 run-id：由现有 terminal envelope、checkpoint 和唯一约束安全拒绝或恢复，不重复注入；
- 所有错误输出保持脱敏，不保存数据库密码、CLS 凭据或原始敏感日志。

## 9. 测试策略

### 9.1 离线合同测试

- fault token 与 run scope 隔离；
- Runtime 和 CLS 单独只能产生部分 Evidence，Aggregator 合并后才 sufficient；
- Single 与 Multi 的工具集合、可信参数和全局预算一致；
- Router 不读取 scenario ID、run ID、Ground Truth 或 evaluator-private 字段；
- recovery intent 幂等，进程恢复和重复请求不重复重启；
- Agent、Prompt、RAG、Artifact 和报告无法读取 Ground Truth；
- 工具输出不存在 Oracle、答案标签或路径穿越。

### 9.2 Docker Live 合同测试

- 池耗尽前订单更新成功；
- 真实 asyncpg 连接在异常路径累积；
- 池耗尽后业务探针稳定超时，同时 PostgreSQL 仍可达；
- 结构化日志包含可配对的 checkout/checkin 和异常生命周期；
- 隔离恢复释放旧连接、更新 generation 并恢复业务探针；
- cleanup 后无测试订单、故障 token、残留连接或故障状态。

### 9.3 真实 LLM + CLS A/B

固定 Git SHA、模型、30 卡知识库、CLS topic/时间范围、工具白名单、评分器和 campaign。使用唯一
run-id 顺序执行 3 次 Single 与 3 次 Multi。只接受实际 `single_agent` / `multi_agent`、
`VALID_PASS`、verification/cleanup 成功且安全 hard gate 通过的配对。

发布门禁：

- Multi 分数、Root Cause Top-1、Evidence Recall 和恢复安全不得低于 Single；
- Multi P95 不超过 Single 的 1.5 倍；
- 每次诊断额外模型调用不超过 2；
- Multi 重复 Evidence 不超过 10%；
- Evidence Recall 提升至少 10 个百分点，或 Root Cause Top-1 提升至少 5 个百分点。

若 Single 再次满分，场景仍可成为有效 Live 场景，但不得宣称 Multi 能力提升；不得降低评分、
required evidence、Validator 或安全门禁，也不得修改题目以追逐既定结果。

## 10. 验收标准

- 新场景可在 Docker Compose 中重复注入、诊断、恢复、Verify 和 Cleanup；
- Single 与 Multi 权限、证据和预算对等；
- 两个来源各自存在真实信息缺口，组合后形成完整因果链；
- 所有持久化结果包含 route audit、Evidence、模型/工具调用、耗时和 checksum；
- 0 Ground Truth 泄漏、0 跨 run/跨租户 Evidence、0 重复恢复、0 未授权工具；
- 普通生产 API 仍固定 `strategy=auto`，不得因本场景实现默认启用 Multi；
- 只有真实 A/B 达到能力、性能和安全门槛后，才允许提出生产默认启用评审。

## 11. 非目标

- 不接入 GitHub Change Investigator；
- 不引入 Kubernetes、Helm、Chaos Mesh、AIOpsLab 或 RCAEval 运行时；
- 不导入外部遥测数据集或复制外部基线代码；
- 不修改聊天 Agent；
- 不降低现有 Benchmark、Validator、Evidence 或恢复安全门禁；
- 不在实现阶段直接执行 3×3 付费 A/B，必须先通过离线与 Docker Live 门禁并另行确认。
