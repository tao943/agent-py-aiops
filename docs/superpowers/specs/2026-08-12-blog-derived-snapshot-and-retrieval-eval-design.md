# AgentPy 博客知识、差分 Snapshot 与 Retrieval Eval 设计

**日期：** 2026-08-12

**状态：** 已确认，待实施计划

**目标：** 将审核后的公开运维资料提炼为通用差分排查知识，在不把评测答案写入 RAG 的前提下，扩展 PostgreSQL 与 Redis Snapshot 场景，并建立独立的检索质量评测。

## 1. 范围与阶段

本切片保留现有 Nginx 差分场景 `APY-003`、`APY-006`，新增 PostgreSQL 与 Redis 各一组同症状、不同根因的 Snapshot：

| 故障族 | 场景 | 主要机制 |
|---|---|---|
| Nginx 502 | APY-003 | 上游进程不可用 |
| Nginx 502 | APY-006 | upstream 端口不匹配 |
| PostgreSQL 连接获取超时 | APY-002 | 慢事务或锁等待长期占用连接 |
| PostgreSQL 连接获取超时 | APY-011 | 应用借出连接后未归还 |
| Redis 请求失败 | APY-007 | Redis 服务停止、端口拒绝连接 |
| Redis 请求失败 | APY-012 | Redis 已恢复但客户端连接池保留失效连接 |

`APY-001` 至 `APY-010` 的既有设计含义保持不变。新增配对原因使用 `APY-011`、`APY-012`，避免旧评测记录和文档中的编号含义漂移。

本切片只实现 Snapshot 与 Retrieval Eval，不实现 Docker Live、恢复动作、Agent 有/无 RAG 对照或聚合看板。

## 2. 资料复用与来源规则

PostgreSQL 与 Redis 的故障机制来自现有审核知识卡引用的官方文档、公开事故复盘和开源项目资料。知识卡是 AgentPy 的原创摘要；具体告警、数值、工具响应、干扰信号和答案由本项目独立合成。

四个新增场景统一在 `provenance.yaml` 中标记：

```yaml
type: agentpy-original
origin:
  - 参考资料中的公开故障机制
  - AgentPy 项目独立构造的冻结场景数据
transformation: 仅参考故障机制；告警、观测、干扰项和答案为项目合成
```

`provenance.yaml` 同时记录精确 URL、访问日期和适用许可证。除非实际转换某个 OpenSRE 具体场景，否则不得标记为 `OpenSRE-derived`。

## 3. 知识与答案的物理隔离

数据边界如下：

```text
公开资料
  -> 人工提炼综合差分知识卡
  -> 审核
  -> docs/knowledge-candidates
  -> 现有批量导入 API
  -> PostgreSQL 文档状态 + Milvus chunks

公开资料中的故障机制
  -> AgentPy 合成 scenario.yaml + Snapshot 工具响应
  -> 独立 ground_truth.yaml + provenance.yaml
  -> Snapshot Runner + 确定性评分
```

允许进入 RAG 的只有通用排查知识卡。以下文件不得导入 Milvus，也不得进入 Agent Prompt：

- `scenario.yaml`
- `snapshot/tool_responses.yaml`
- `ground_truth.yaml`
- `provenance.yaml`
- Retrieval Eval 标注和评分规则

知识卡不得包含场景编号、场景专属名称或数值、Snapshot 原句、oracle mechanism、trigger、evidence milestone ID 或固定答案规则。

## 4. 综合差分排查知识卡

重构现有两张知识卡，不新增重复的单根因答案卡：

- `docs/knowledge-candidates/postgres-pool-exhaustion.md`
- `docs/knowledge-candidates/redis-unavailable.md`

每张卡统一包含：适用现象、候选原因、建议证据、候选区分、安全恢复边界、恢复后验证、来源与许可证。

综合卡负责教 Agent 比较多个候选原因，而不是陈述某个场景的答案。每张卡至少包含三个合理候选原因：

- PostgreSQL：慢事务/锁等待、应用连接生命周期异常、容量与并发失配。
- Redis：服务端不可用、客户端连接池或重连异常、网络路径异常。

`APY-011` 与 `APY-012` 采用不完全知识覆盖。知识卡要求检查连接借出/归还和客户端重连状态，但不提供与场景证据一一对应的完整判定组合。

## 5. 新增 Snapshot 场景合同

### 5.1 公共输入

同一故障族的两个场景必须使用相同的中性标题、告警结构和候选假设。公共候选为：

```text
PostgreSQL
- slow_transaction_pool_exhaustion
- application_connection_lifecycle_failure
- traffic_capacity_mismatch

Redis
- redis_service_unavailable
- client_pool_recovery_failure
- network_path_failure
```

公共标题只描述可观察现象：

- PostgreSQL：应用获取数据库连接持续超时。
- Redis：应用 Redis 请求持续失败。

公共输入不能出现 ground truth 的 mechanism、trigger、必要 evidence ID 或场景专属答案提示。

### 5.2 证据与干扰合同

| 场景 | 必要支持证据 | 必须排除的强干扰 | 弱干扰信号 |
|---|---|---|---|
| APY-002 | PostgreSQL 长事务/锁等待；连接池连接正在等待数据库操作 | 应用连接生命周期异常 | 流量轻微上涨 |
| APY-011 | 借出/归还计数持续背离；请求路径存在未释放连接 | 慢事务/锁等待 | 数据库连接数接近上限 |
| APY-007 | Redis 进程停止且端口无监听；客户端收到 connection refused | 客户端连接池异常 | 无关应用近期发布 |
| APY-012 | Redis 健康且 PING 成功；客户端池仍存在失效连接和等待请求 | Redis 服务端仍不可用 | Redis 内存使用略高 |

每个场景至少需要两条来自不同工具的支持证据。强干扰原因必须在结构化假设状态中被反驳；弱干扰信号不得进入主要因果链。

### 5.3 冻结工具

PostgreSQL 场景允许：

- `InspectPostgres`
- `InspectDatabasePool`
- `GetServiceMetrics`
- `GetDeploymentChanges`

Redis 场景允许：

- `InspectRedis`
- `InspectRedisClientPool`
- `GetServiceMetrics`
- `GetDeploymentChanges`

工具继续使用现有 Snapshot MCP 的精确参数匹配和失败关闭行为。冻结结果描述可观察事实，不包含答案标签、scenario ID 或 oracle 字段。

## 6. Ground Truth 与评分

每个新增 `ground_truth.yaml` 包含一个主要原因、至少两项必要证据里程碑、一个必须排除的强干扰原因、禁止结论和因果链。

评分继续复用现有确定性评分器，不因 Agent 引用知识卡而直接加根因分。正确通过要求：

- component 和主要 mechanism 正确；
- 两项必要证据均来自已持久化 Snapshot 观测；
- 强干扰假设被已存在证据明确反驳；
- 弱干扰信号未被误判为主因；
- 决策只引用已持久化 evidence ID。

单看告警、知识卡或一个工具结果不能满足全部必要证据。

## 7. Retrieval Eval

Retrieval Eval 与 Diagnosis Eval 分离。前者只评价是否找到合适的排查知识，后者只依据 Snapshot 运行证据判断根因。

第一版为 PostgreSQL 和 Redis 各建立三类查询，共六条：

1. 明确症状查询。
2. 模糊用户描述。
3. 反事实或强干扰查询。

标注记录至少包含：

```yaml
id: RET-PG-001
query: 应用获取 PostgreSQL 连接持续超时，应该检查哪些证据？
relevant_documents:
  - postgres-pool-exhaustion.md
acceptable_top_k: 3
forbidden_top_one:
  - redis-unavailable.md
```

查询不得包含 scenario ID、ground truth mechanism、trigger 或证据 ID。

第一版指标：

- `Recall@1`
- `Recall@3`
- `MRR`
- 禁止文档进入 Top 1 的比例
- 引用字段完整率
- tenant 与 knowledge-base 隔离正确性

真实 Embedding 和 Rerank 运行是手动测试，不进入普通 CI。普通 CI 使用受控假实现验证查询加载、评分公式、双分数引用合同和租户隔离。

## 8. 导入与幂等边界

两张知识卡通过现有批量导入器更新。操作顺序为 dry-run、真实导入、PostgreSQL 状态核验、Milvus scoped chunks 核验。

批量导入器继续使用现有上传 API 的 `overwrite=true`。实施时必须先验证同名更新的真实 API 语义，确保不会静默创建重复文档；若现有 API 只按内容哈希或其他标识处理覆盖，则应先修正或记录可审计导入清单，再执行真实导入。

不重复导入其他五张未修改知识卡，不删除历史文档，不绕过 owner/tenant 范围。

## 9. 实施顺序

1. 在现有场景加载和答案隔离测试上增加四个新场景的失败测试。
2. 手工编写四组 Scenario、Snapshot、Ground Truth 和 Provenance。
3. 重构两张综合差分知识卡并增加内容安全检查。
4. 建立六条 Retrieval Eval 标注、评分合同和离线测试。
5. 验证同名 overwrite 行为后，dry-run 并更新两张知识卡。
6. 核验 PostgreSQL 文档/任务状态和 Milvus scoped chunks。
7. 运行普通离线回归、Ruff、Pyright 与 OpenSpec。

本阶段不运行四次真实 Agent RAG 对照，不创建 Live 故障，不修改恢复流程。

## 10. 验收标准

- `APY-002`、`APY-007`、`APY-011`、`APY-012` 可由现有 loader 与 Snapshot MCP 加载。
- 同一故障族的标题、告警和候选假设保持一致，主要机制不同。
- 每个新场景至少有两项必要证据和一个必须排除的强干扰原因。
- 每个场景包含一个弱干扰信号，且该信号不属于正确因果链。
- 公开输入不包含 oracle mechanism、trigger 或必要 evidence milestone ID。
- 四个 provenance 均为 `agentpy-original`，含精确参考 URL、访问日期、许可证和合成说明。
- 六条 Retrieval Eval 查询均不包含评测答案，目标知识卡进入 Top 3。
- 每个检索引用包含 chunk、文档、知识库、向量分和精排分。
- tenant/knowledge-base 隔离测试通过。
- 两张修改后的知识卡成功更新到当前授权知识库，PostgreSQL 和 Milvus 证据一致。
- 普通离线测试、Ruff、Pyright 与 OpenSpec 全部通过。

## 11. 风险与控制

- **知识卡过度贴合答案：** 使用综合差分卡、不完全覆盖和内容安全测试；评分不读取知识卡关键词。
- **合成场景缺乏可信度：** provenance 明确区分参考机制与合成数据；工具事实保持内部一致并包含反事实证据。
- **检索评测样本过少：** 第一版只验证合同和基础可用性，不宣称总体检索质量；后续随知识库扩展增加查询。
- **同名导入产生重复：** 真实导入前验证 overwrite 语义，导入后同时核验 PostgreSQL 与 Milvus。
- **真实模型波动：** CI 使用确定性假实现；真实检索结果保存模型配置、分数和运行时间，后续才能形成版本对比。
