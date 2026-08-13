# AgentPy 30 张知识卡与 60 查询 Retrieval Benchmark 设计

**日期：** 2026-08-13

**状态：** 已确认，待用户审阅与实施计划

**目标：** 在不启动 Docker 故障实验、不把评测标签导入 RAG 的前提下，将当前测试知识库从 7 张扩展到 30 张差分排障知识卡，并把 Retrieval Benchmark 从 6 条扩展到 60 条，使真实召回面对足够多的同域困难负例。

## 1. 本阶段范围

本阶段完成：

- 新增 23 张知识卡，使 `docs/knowledge-candidates/` 总数达到 30。
- 统一审核现有 7 张卡的结构、来源和验证状态，但不无故重写已验证内容。
- 将 Retrieval Benchmark 从 6 条扩展到 60 条。
- 修复文档级排名去重和 citation 缺失计分问题。
- 离线验证后，将 30 张卡批量更新到当前测试 owner 的 PostgreSQL 与 Milvus。
- 运行一次真实 Embedding/Rerank Retrieval Eval，保存安全报告和知识库快照信息。

本阶段不完成：

- Docker Compose 故障注入或 Live Agent Eval。
- Agent 有/无 RAG before/after 对照。
- Chat 模型诊断调用。
- 将博客全文、Benchmark 查询、相关性标签或场景答案导入 RAG。
- 为无答案查询拍脑袋设置生产拒绝阈值。

## 2. 数据隔离

三类数据保持物理隔离：

```text
docs/knowledge-candidates/*.md
  -> 允许导入 RAG 的通用差分排障知识

benchmarks/agentpy/retrieval/queries.yaml
  -> Runner 临时读取的试题和 evaluator-only 相关性标签

benchmarks/agentpy/scenarios/**/{snapshot,ground_truth,provenance}
  -> Diagnosis Eval 的冻结观测、答案和来源
```

只有知识卡进入 PostgreSQL 文档表和 Milvus。`query` 只在一次检索调用中作为输入使用，`relevant_documents`、`forbidden_top_one`、查询类型、无答案标记和评分配置不能进入 Embedding 文档、Milvus、Prompt 或 Agent 报告。

## 3. 来源与原创规则

每张知识卡采用以下来源优先级：

1. 官方产品或协议文档。
2. 有明确兼容许可证的开源项目文档、runbook 或公开 issue。
3. 公开事故复盘和技术博客，仅用于交叉验证故障机制，不复制原文。

每张卡由 AgentPy 重新组织为原创差分摘要，并包含：

- 精确来源 URL；
- 来源类型；
- 已知许可证，或诚实标注 `license: unknown-reference-only`；
- 访问日期；
- `content_type: agentpy-original-summary`；
- `docker_validation: pending`。

`docker_validation: pending` 表示资料审核完成但尚未在本项目 Docker Compose 中实验验证。它不能被描述为“已复现”“已通过 Live 验证”或“生产结论”。下一阶段 Docker 实验完成后才能逐卡改为 `verified` 或 `partially_verified`。

## 4. 30 张知识卡目录

### 4.1 PostgreSQL（6）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `postgres-pool-exhaustion.md` | 现有 | 慢事务占池、连接泄漏、容量不足、不可达 |
| `postgres-slow-query-lock-wait.md` | 新增 | 慢查询、锁阻塞、数据库资源压力 |
| `postgres-deadlock.md` | 新增 | 死锁回滚、普通锁等待、应用重试放大 |
| `postgres-connectivity-auth.md` | 新增 | 网络不可达、认证失败、TLS/pg_hba 配置 |
| `postgres-replication-lag.md` | 新增 | 复制延迟、读副本慢查询、网络/WAL 压力 |
| `postgres-disk-wal-pressure.md` | 新增 | 磁盘容量、WAL 堆积、checkpoint/IO 压力 |

### 4.2 Redis（5）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `redis-unavailable.md` | 现有 | 服务端停止、客户端池恢复、网络路径 |
| `redis-memory-eviction.md` | 新增 | 内存上限、淘汰策略、缓存击穿 |
| `redis-slow-command-hot-key.md` | 新增 | 慢命令、big key、hot key、CPU 压力 |
| `redis-failover-reconnect.md` | 新增 | 故障转移、DNS/拓扑变化、客户端重连 |
| `redis-maxclients-pressure.md` | 新增 | maxclients、连接泄漏、连接风暴 |

### 4.3 Nginx 与 HTTP（4）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `nginx-upstream-502.md` | 现有 | 进程不可用、端口错误、协议不匹配 |
| `nginx-upstream-timeout.md` | 新增 | 上游慢、网络超时、超时预算错误 |
| `nginx-routing-service-discovery.md` | 新增 | 路由、upstream、服务发现和端点漂移 |
| `http-rate-limit-retry-storm.md` | 新增 | 网关限流、下游限流、重试风暴 |

### 4.4 微服务运行时（4）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `microservice-timeout.md` | 现有 | 本服务慢、下游慢、超时预算和级联失败 |
| `service-thread-pool-saturation.md` | 新增 | 线程/协程池耗尽、同步阻塞、下游等待 |
| `service-circuit-breaker-degradation.md` | 新增 | 熔断打开、依赖不可用、恢复探测失败 |
| `service-startup-config-failure.md` | 新增 | 配置错误、依赖未就绪、启动探针失败 |

### 4.5 Kubernetes 与 DNS（4）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `kubernetes-dns-debugging.md` | 现有 | CoreDNS、Service、Pod DNS 配置和网络 |
| `kubernetes-memory-saturation.md` | 现有 | OOM、limit、内存泄漏和节点压力 |
| `kubernetes-pod-crashloop.md` | 新增 | 应用退出、探针失败、配置/依赖错误 |
| `kubernetes-service-endpoint-mismatch.md` | 新增 | selector、Endpoint、targetPort 和 readiness |

### 4.6 队列与异步任务（3）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `queue-backlog.md` | 现有 | 生产突增、消费变慢、消费者不足 |
| `queue-consumer-stalled.md` | 新增 | 消费者停止、锁/租约、下游阻塞 |
| `queue-poison-message-dlq.md` | 新增 | 毒消息、重复重试、DLQ 与幂等失败 |

### 4.7 主机资源与 TLS（4）

| 文件 | 状态 | 主要差分问题 |
|---|---|---|
| `host-disk-capacity-pressure.md` | 新增 | 容量、inode、日志/WAL 增长和只读文件系统 |
| `host-cpu-load-pressure.md` | 新增 | CPU 饱和、run queue、限额和 busy loop |
| `host-file-descriptor-exhaustion.md` | 新增 | FD 上限、连接泄漏、socket/file 使用 |
| `tls-certificate-handshake-failure.md` | 新增 | 证书过期、信任链、SNI、协议/时钟问题 |

总数固定为 30。若实施时发现某张卡缺乏两个可核对的高质量来源，不以低质量内容补位；停止并替换为同故障族、来源更可靠的卡，同时保持分类总数不变。

## 5. 知识卡内容合同

30 张卡都必须包含：

```text
## 适用现象
## 候选原因
## 建议证据
## 如何区分
## 安全恢复边界
## 恢复后验证
## 来源
## 验证状态
```

每张卡至少包含三个候选原因、两类相互独立的证据和一个强替代原因。内容使用不确定性语言，不能把单个字段写成固定答案规则。恢复动作必须区分只读、低风险、需审批和禁止自动执行的边界。

知识卡禁止包含：

- `APY-*` 场景编号；
- `ground_truth`、oracle mechanism、trigger 或 evidence ID；
- Snapshot 专属数值、容器名和原句；
- Benchmark query ID、相关文档标签和分数门槛；
- API key、密码、token、真实 owner/KB/document ID。

## 6. Chunk 预算与导入边界

Markdown 继续复用现有 heading-aware chunker，不引入新向量库或 chunk 框架。目标预算：

- 每张卡目标 6 至 10 个 chunk，硬上限为 12 个；
- 30 张卡总计预期约 180 至 300 个 chunk；
- 单张卡不得因重复来源或模板文本产生大量近重复 chunk。

八个统一章节中，`适用现象`、`候选原因`、`建议证据`、`如何区分`、`安全恢复边界`、`恢复后验证` 六个运维章节进入向量索引；`来源` 与 `验证状态` 属于治理信息，保留在 PostgreSQL 的完整 Markdown/metadata 中，但不得形成独立 Milvus chunk。标题过滤必须由共享 chunking 实现执行，离线 audit 与真实导入必须使用同一份持久化配置，不能在 audit 脚本中另写一套近似逻辑。

批量 importer 必须显式上传 `markdown-heading` 及治理标题排除配置；不得依赖 API 的旧默认 fixed-character 配置。真实导入前必须生成 chunk 预览，记录文档级 chunk 数和标题路径。若任一文档为 0 chunk、超过 12 chunk、未覆盖六个运维章节或出现明显断句/模板重复，则先修卡或 chunk 配置，不直接导入。

批量导入顺序：dry-run、导入前 active/duplicate 审计、一次 `overwrite=true` 批量导入、PostgreSQL 状态核验、Milvus owner/tenant/KB/document scope 核验。历史重复 active 文档仍不允许静默批量清理。

## 7. 60 条 Benchmark 查询

查询总数固定为 60：

| 类型 | 数量 | 目的 |
|---|---:|---|
| 明确组件查询 | 12 | 验证基础组件与机制召回 |
| 模糊现象查询 | 14 | 不直接写组件名，验证症状理解 |
| 日志或指标片段 | 12 | 模拟告警、日志和 dashboard 文本 |
| 口语、缩写或轻微拼写扰动 | 8 | 验证真实操作员表达鲁棒性 |
| 跨组件强干扰 | 8 | 验证相似故障族之间的排序 |
| 无答案/超出知识范围 | 6 | 观察误匹配分数分布，为下一阶段阈值校准 |

54 条有答案查询覆盖全部 30 张卡：每张卡至少作为一次相关文档，其中 24 张卡具有第二种不同表达。查询不能直接复述文档标题；模糊、日志和强干扰查询优先用于第二次覆盖。

每条查询增加显式元数据：

```yaml
id: RET-PG-LOCK-LOG-001
type: log_signal
query: "wait_event_type=Lock; active sessions rising; checkout wait increasing"
relevant_documents:
  - postgres-slow-query-lock-wait.md
acceptable_top_k: 3
forbidden_top_one:
  - postgres-connectivity-auth.md
source_type: project-synthesized
review_status: reviewed
expected_no_answer: false
```

查询来源允许：项目日志字段和指标名的脱敏重写、官方文档中的通用错误类型、公开事故现象的原创改写，以及人工设计的口语/扰动表达。不能复制含用户数据的真实日志，也不能把知识卡中的完整段落改成问句。

## 8. 无答案查询

当前 `KnowledgeRetrievalTool` 固定返回 Top-K，只要知识库非空就不会自然返回“无答案”。Rerank score 也不是跨版本、跨查询已校准的概率。因此第一阶段的 6 条无答案查询：

- `relevant_documents` 为空；
- `expected_no_answer: true`；
- 保存 Top-1 文档、vector/rerank score 和 score margin；
- 计算诊断性 `noAnswerProbeCount` 和分数分布；
- 不纳入 Recall/MRR 分母，也不作为 CLI 退出码门禁；
- 不设未经校准的固定置信度阈值。

下一阶段在获得更多正负查询和重复运行数据后，单独划分 calibration/test 集，确定拒绝阈值或新增显式 abstention/relevance classifier。阈值一旦用 calibration 集确定，不能再用 test 集调参。

## 9. 评分合同修复

### 9.1 文档级去重

真实 tool 返回 chunk 排名，但 Recall/MRR 的目标是文档。评分前按首次出现顺序对 `source` basename 去重：

```text
[pg chunk 0, pg chunk 1, redis chunk 0]
-> [postgres-pool-exhaustion.md, redis-unavailable.md]
```

主指标改为去重后的 Document Recall@1、Document Recall@3 和 Document MRR。报告可保留原始 chunk hits 用于审计，但不能让同一文档的多个 chunk 挤占文档级 Top-K。

### 9.2 Citation 完整率

每一个返回 hit 都是 citation 完整率的一个分母。缺 citation、citation 指向其他 hit、缺 chunk/document/KB ID、缺 vector score 或 rerank score，均计为不完整；不能因为缺 citation 就跳过该 hit。

tenant/knowledge-base 越界仍是硬失败，不进入评分。

### 9.3 有答案查询的第一阶段门槛

在 30 张卡和 54 条有答案查询上，第一版目标是：

```text
Document Recall@1       >= 0.80
Document Recall@3       >= 0.95
Document MRR            >= 0.85
Forbidden Top-1 Rate    <= 0.05
Citation Completeness   == 1.00
```

这些门槛用于发现明显回退，不保证生产泛化。真实结果低于门槛时保留报告和 bad cases，不能修改相关性标签或删掉困难查询强行通过。

## 10. 复用策略

直接复用项目已有：

- heading-aware Markdown chunker；
- filename-idempotent batch importer；
- PostgreSQL 文档/任务状态；
- Milvus tenant-scoped hybrid retrieval；
- BM25、向量召回、RRF 和真实 Rerank；
- `RetrievalQuery` loader、纯评分器和安全 runner；
- pytest、Ruff、Pyright 和 OpenSpec。

不新增 Ragas、BEIR、EvalScope 或其他评测框架依赖。它们可用于核对标准指标定义，但当前需求包含项目自有 tenant 隔离、forbidden Top-1、citation 审计和无答案探针，薄扩展现有 domain contract 的集成成本更低且更可审计。

## 11. 验证顺序

1. OpenSpec 定义 30 卡目录、60 查询分类、答案隔离和指标合同。
2. 先写失败测试：知识卡目录数量/结构/泄漏、查询分布/覆盖、文档去重、citation 缺失。
3. 收集并审核来源，编写 23 张新卡并补齐现有卡验证状态。
4. 编写 54 条有答案查询和 6 条无答案探针。
5. 让 loader、评分器、runner 和离线合同测试转绿。
6. 生成 30 卡 chunk 预览并执行安全审核。
7. dry-run 后真实批量导入 30 卡。
8. 核验 PostgreSQL active/index task 和 Milvus chunks。
9. 运行一次真实 Retrieval Eval；保存安全报告，不运行 Chat/Agent。
10. 运行聚焦测试、Ruff、Pyright、OpenSpec；普通全量测试仍由本地有界运行与 GitHub Actions 共同门禁。

## 12. 验收标准

- 知识候选目录恰好 30 张卡，7 张现有卡保留，23 张新增卡符合分类清单。
- 每张卡具有统一章节、至少两个独立证据维度、来源/许可和 `docker_validation: pending`。
- 所有卡通过答案泄漏、敏感信息和 Benchmark contamination 检查。
- chunk 预览显示每张卡 6 至 10 个目标 chunk、最多 12 个，治理章节未形成独立向量块，总体无明显模板重复。
- `queries.yaml` 恰好 60 条，分类数量与设计一致，54 条有答案查询覆盖全部 30 张卡。
- 6 条无答案探针不进入 Recall/MRR 分母，也不使用未经校准的通过阈值。
- 文档级指标按首次出现去重，缺失 citation 的 hit 明确计为不完整。
- 离线 CI 不调用真实模型或外部网络。
- 真实导入后，每个 filename 在目标 owner/KB 中恰好一个 active indexed 文档。
- Milvus 新文档具有 owner/tenant/KB-scoped chunks，被替换文档无残留 chunks。
- 真实报告不包含正文、excerpt、凭据、原始配置、场景答案或敏感日志。
- 本阶段文档明确声明 Docker 未运行，30 张卡尚未完成本地故障实验验证。

## 13. 风险与停止条件

- **来源许可不明：** 仅引用事实和 URL，不复制；无法交叉验证的机制不写入卡。
- **卡片模板化导致近重复：** chunk 预览和相似内容人工审核未通过则停止导入。
- **Benchmark 作者偏差：** 查询不得复述标题；困难查询优先来自日志/现象，并保留独立审核字段。
- **知识覆盖不足：** 无答案查询只做探针，不以错误文档作为“勉强相关答案”。
- **真实指标下降：** 保存 bad case，不改标签送分；区分知识缺口、查询歧义、chunking、召回和 rerank 问题。
- **历史数据库重复：** 任一 filename 出现多个 active 文档即停止，不自动清理。
- **模型额度或外部服务失败：** 保留离线验证和导入状态，真实 Eval 标为未完成，不伪造结果。
- **Docker 未验证：** 所有卡保持 `pending`；下一阶段实验结果可以修订知识卡和 Benchmark，但不得回写本阶段测试答案到 RAG。
