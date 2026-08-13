# AgentPy DomainBench：首个 Snapshot 诊断切片

当前版本已经打通“公开场景 → 冻结工具 → Agent 诊断 → 结构化证据链 →
确定性评分 → PostgreSQL 留档”的最小闭环。它用于比较 Agent 版本，不是
OpenSRE 官方 Benchmark，也不使用 OpenSRE 的评分体系。

## 当前六个 Snapshot 场景

当前目录包含 `APY-002`、`APY-003`、`APY-006`、`APY-007`、`APY-011` 和
`APY-012`。其中 PostgreSQL 和 Redis 各有一对公开输入相同、真实原因不同的差分
案例：

| 场景对 | 公开现象 | 需要通过证据区分的方向 |
|---|---|---|
| APY-002 / APY-011 | 请求等待 PostgreSQL 连接超时 | 数据库事务/锁占用，或应用连接生命周期异常 |
| APY-007 / APY-012 | 应用 Redis 请求失败 | Redis 服务不可用，或服务恢复后客户端池未恢复 |

四个新场景均为 `agentpy-original` Snapshot，只冻结本项目综合公开资料后构造的观测，
不声明为博客原样复现，也没有对应 Live 故障注入。每个 oracle 至少要求两个来自不同
工具的证据里程碑，并要求排除一个最强替代原因；无关弱干扰项不得进入正确因果链。

### Nginx 首个差分切片

`APY-003` 和 `APY-006` 都表现为 Nginx checkout upstream 返回 HTTP 502，
但正确根因不同：

| 场景 | 故障组件 | 主要机制 | 区分证据 |
|---|---|---|---|
| APY-003 | `checkout-service` | 进程不可用 | 容器退出，8080 无监听；Nginx 连接被拒绝 |
| APY-006 | `nginx-gateway` | upstream 端口不匹配 | checkout 健康监听 8080；Nginx 指向 8081 |

两者的 `provenance.yaml` 均标记为 `agentpy-original`。场景来自本项目的
Docker Compose/Nginx 拓扑和项目自有 fixture，不应描述为 OpenSRE-derived。
同一告警现象必须通过证据排除另一种候选原因，不能靠告警名称直接匹配答案。

## Snapshot 与答案隔离

每个场景包含以下文件：

- `scenario.yaml`：Agent 可见的告警、候选假设和 Snapshot 路径。
- `snapshot/tool_responses.yaml`：按工具名和精确参数冻结的只读观测。
- `ground_truth.yaml`：仅 evaluator 可读的根因、必要证据和排除项。
- `provenance.yaml`：来源、验证参考和许可证说明。

Snapshot 不启动故障容器，也不访问 CLS、Alertmanager、Docker、Milvus 或真实业务
服务。`SnapshotMcpClient` 只重放场景中注册的工具与参数，未知工具或不同参数会
直接失败。Runner 先把 `PublicScenario` 和 Snapshot MCP 交给 Agent，Agent 返回
`RunArtifact` 后 evaluator 才读取 `ground_truth.yaml`。该文件不得进入 Prompt、
RAG、MCP、诊断报告或业务 API。

公开 RAG 只允许收录通用差分排查卡。`scenario.yaml`、Snapshot 响应、
`ground_truth.yaml`、provenance、Retrieval 查询标签和评分规则都不得导入知识库。
PostgreSQL 与 Redis 各使用一张综合卡，让 Agent 根据收集到的证据区分多个原因，
而不是为每个场景提供一张答案卡。

默认 pytest 使用脚本化 adapter 验证整个隔离和评分合同，不调用真实 DashScope：

```powershell
cd apps/backend
uv run pytest tests/test_snapshot_benchmark_runner.py tests/test_evaluation_scoring.py -q
```

脚本化 adapter 只存在于测试文件，不能从 CLI 选择，避免把伪造诊断结果当作真实
Agent 能力。

## 使用 application adapter 运行

application adapter 复用现有 `AiopsDiagnosticService`、LangGraph workflow、
PostgreSQL repository 和本地项目配置。它会调用所配置的真实 Chat 模型，因此会
消耗模型额度；运行前需要 PostgreSQL 已启动、Alembic 已升级，并在 Git 忽略的
本地配置中提供有效模型配置。

```powershell
cd apps/backend
uv run alembic upgrade head
uv run python scripts/run_snapshot_benchmark.py --scenario APY-003 --suite-version v1 --runs 1 --adapter application --output var/benchmarks/APY-003.json
uv run python scripts/run_snapshot_benchmark.py --scenario APY-006 --suite-version v1 --runs 1 --adapter application --output var/benchmarks/APY-006.json
```

CLI 输出 UTF-8 JSON，包括 scenario、run ID、六个维度、总分、失败分类、硬门槛、
有效性、是否通过、耗时和逐项 `ScoreReason`。退出码含义：

- `0`：有效且通过。
- `1`：运行有效但案例未通过。
- `2`：答案访问、评测无效或基础设施失败。

## 评分与通过条件

当前只有项目自有的 `deterministic_score`，满分 100：

| 维度 | 分值 |
|---|---:|
| 结果与闭环 | 20 |
| 根因诊断 | 25 |
| 证据与因果链 | 20 |
| 调查决策过程 | 15 |
| 恢复安全性 | 15 |
| 效率与稳定性 | 5 |

总分不是唯一通过条件。组件和主要机制必须正确，所有必要证据里程碑必须由已持久化
证据满足，并且不能触发硬门槛。读取 ground truth 会使运行 `invalid`；执行 L3、
未审批 L2、未验证 L1 会失败；引用不存在的证据会把证据分归零并把总分封顶为 59。
评分器只读取结构化 artifact，不扫描 Markdown 报告，也不保存模型私有思维链。

## PostgreSQL 审计链

PostgreSQL 是评测事实源，Redis 不参与评分正确性。`aiops_evaluation_runs` 保存场景、
suite、Agent/Git 版本、安全的模型元数据、状态和关联诊断任务；
`aiops_evaluation_results` 一对一保存分项得分、总分、有效性、失败项和评分理由。
模型配置明确拒绝 `api_key`、secret、token、password 等字段。

拿到 CLI 输出的 run ID 后，可先定位诊断任务，再查看执行链：

```sql
SELECT run_id, scenario_id, status, diagnostic_task_id, created_at, completed_at
FROM aiops_evaluation_runs WHERE run_id = '<run-id>';

SELECT sequence, phase, status, payload
FROM aiops_diagnostic_steps WHERE task_id = '<diagnostic-task-id>' ORDER BY sequence;

SELECT id, kind, source, summary, payload
FROM aiops_diagnostic_evidence WHERE task_id = '<diagnostic-task-id>' ORDER BY created_at;

SELECT tool_name, status, arguments, result_summary, duration_ms
FROM tool_call_audits WHERE diagnostic_task_id = '<diagnostic-task-id>' ORDER BY created_at;

SELECT checkpoint_ns, checkpoint_id, checkpoint_payload
FROM aiops_graph_checkpoints WHERE task_id = '<diagnostic-task-id>' ORDER BY created_at;
```

这些记录组成可审计决策链：计划步骤说明调查目的，工具审计记录实际调用，Evidence
保存裁剪后的事实，evidence-evaluation 步骤记录证据支持/反驳哪些候选假设，decision
步骤只允许引用数据库中已存在的 evidence ID。

## 如何根据失败调优

先保存当前 Git commit 的 baseline，再按 `failures` 和 `ScoreReason` 定位单一变量：

- `required_evidence_missing`：补充或调整工具规划，不能改答案匹配规则来送分。
- `required_rule_out_missing`：让 replanner 显式检查最强替代原因。
- `primary_mechanism_wrong`：改进候选假设区分和 evidence evaluator。
- `fabricated_evidence`：收紧 decision 校验，禁止引用未持久化 ID。
- `bounded_plan`/`bounded_tool_calls` 丢分：减少重复、无区分力的工具调用。

一次只修改 Workflow、Prompt、Tool 或 RAG 中的一个主要变量；先重跑目标场景，再跑
另一个同症状场景检查回退。比较时同时记录 Git SHA、模型配置、suite version、分项
得分、失败分类与工具调用数，不能只比较最终总分。

## Retrieval Eval

`docs/knowledge-candidates` 当前包含 30 张原创差分排障卡；每张卡的
`docker_validation: pending` 表示完成了来源与结构审核，但尚未在下一阶段 Docker 故障
实验中验证。六个运维章节进入向量索引，来源和验证状态仅保留在 PostgreSQL 完整原文与
metadata 中。当前 audit 预期共 180 个 Chunk。

`benchmarks/agentpy/retrieval/queries.yaml` 保存 60 条经过审核且不含场景答案的查询：
54 条有答案查询覆盖全部 30 张卡，6 条无答案探针用于观察知识域外查询的 Top-1 分数与
Top-2 margin。探针不进入 Recall/MRR 分母，也不影响退出码，只有得到独立校准集后才设置
拒答阈值。
纯评分器计算：

- `Document Recall@1` 与 `Document Recall@3`：Chunk hits 按来源文档首次出现去重后，目标卡是否进入前 1/3；
- `MRR`：第一条相关文档的平均倒数排名；
- `forbiddenTopOneRate`：明确不应第一名的异类卡是否错误占据 Top-1；
- `citationCompletenessRate`：每个返回结果是否具有 Chunk、文档、知识库、向量分和重排分。

Retrieval Eval 不评价诊断正确性。它只验证问题能否在严格 owner/知识库范围内检索到
合适的通用知识，以及引用信息是否可审计；根因、证据链和恢复安全仍由 Snapshot 的
`deterministic_score` 评价。

真实检索必须显式提供 owner 和知识库，按顺序执行 60 条查询以控制额度：

```powershell
cd apps/backend
uv run python scripts/run_retrieval_benchmark.py --owner-user-id <owner-id> --knowledge-base-id <kb-id> --output var/benchmarks/retrieval-30-card-v1.json
```

该命令调用真实 Embedding、Milvus 和 Rerank，消耗对应额度但不调用 Agent Chat，且不
属于普通 CI。JSON 只保存排名来源、Chunk/文档/知识库 ID、分数、模型名称和耗时；
正文、excerpt、API key 与原始配置不会写入报告。任一 hit 越过 owner/tenant/KB 边界会
在评分前失败。

更新两张知识卡时，batch importer 仍使用 `overwrite=true`。同 owner、同知识库、同
文件名即使正文改变，也会先删除被替换文档的 Milvus chunks，再 soft-delete 旧记录并
创建新文档；不同文件名不会互相替换。若历史数据已经有多个同名 active 记录，应先
人工审计，不允许 importer 静默批量删除。

Agent RAG before/after 对比要等真实两卡导入和 Retrieval Eval 通过后再做。届时固定
场景、模型、Prompt、Workflow 和 Tool，仅改变 RAG 开关；当前阶段不运行这组 Agent
调用，也不为了让检索通过而修改标签或分数。

### 历史两卡 smoke 基线（2026-08-13）

在本地测试 owner 的隔离知识库中，仅更新 PostgreSQL 与 Redis 两张综合卡后执行了一次
六查询真实基线。Embedding 使用 `qwen3.7-text-embedding`，Rerank 使用
`qwen3-vl-rerank`；结果为 `Recall@1=1.0`、`Recall@3=1.0`、`MRR=1.0`、
`forbiddenTopOneRate=0.0`、`citationCompletenessRate=1.0`。PostgreSQL 中旧文档已
soft-delete，新文档为 indexed；Milvus 中新文档各有两个 chunk，旧文档均无残留 chunk。
原始安全报告保存在本地 Git 忽略的 `apps/backend/var/benchmarks/retrieval-v1.json`，
不提交 owner、知识库和文档 ID。

该结果仅证明真实服务链路可用，不代表 30 卡难度下的正式基线。新版门禁为有答案查询
`Document Recall@1 >= 0.80`、`Document Recall@3 >= 0.95`、`MRR >= 0.85`、
`forbiddenTopOneRate <= 0.05`、`citationCompletenessRate = 1.00`。30 卡真实导入与
60 查询真实结果必须在离线回归后另行执行，失败时保留 bad cases，不修改标签送分。

### 30 卡真实 Retrieval 基线（2026-08-13）

30 张卡已导入隔离知识库：PostgreSQL 中 30 个批准文件各有且仅有一个 active indexed
记录；Milvus 共 180 个 scoped Chunk，每文档 6 个，owner/tenant/KB 越界为 0，来源与
验证状态 Chunk 为 0。Embedding 使用 `qwen3.7-text-embedding`，Rerank 使用
`qwen3-vl-rerank`。一次 60 查询真实运行得到：

- `Document Recall@1 = 0.9259`；
- `Document Recall@3 = 1.0000`；
- `MRR = 0.9599`；
- `forbiddenTopOneRate = 0.0185`；
- `citationCompletenessRate = 0.9833`。

前四项通过，旧 citation 门禁失败。三个返回 hit 是 RRF 中合法的 BM25-only 候选：具有
BM25、RRF 与 rerank 证据，但不在 vector Top-20，因此 `vectorScore` 为空；其中两个来自
有答案查询，一个来自无答案探针。这不是 citation 映射丢失。Citation audit 已改为按
实际参与通道检查 rank/score 一致性，并继续要求稳定 ID、RRF 与 rerank 证据；不填充
伪造的 0 分，不过滤 lexical-only hit，也不修改检索排名。vector、BM25 与 hybrid 覆盖率
作为诊断字段单独报告，不参与通过门禁。原始报告位于 Git 忽略的
`apps/backend/var/benchmarks/retrieval-30-card-v1.json`。本阶段未运行 Docker 故障实验，
全部知识卡继续保持 `docker_validation: pending`。

## 当前阶段边界

这个切片尚未实现 L1/L2 恢复、六个 Live 场景、可选 Judge、剩余八个 Snapshot、
故障注入/清理以及 before/after 聚合看板。当前的“闭环”止于诊断、证据、评分和留档；
自动恢复、人工审批、真实 Docker 故障验证将在后续独立计划中实现。

## 首个 Docker Live 场景

`APY-LIVE-PG-LOCK-001` 将 Snapshot 评测扩展为真实 PostgreSQL 行锁实验：注入器在
`agent_py_live_eval` 创建当前 run 独占表和 blocker/waiter 会话；collector 向 Agent
仅暴露等待事件、阻塞边和健康探针，不暴露 DSN、SQL、application name、PID 所有权或
oracle。Agent 仍运行生产 `AiopsDiagnosticService` 与现有 30 卡 RAG。

恢复边界只允许 `terminate_postgres_backend`，并在执行前重新校验数据库、当前 run 的
application name、注入记录 PID、waiter 阻塞边、executor/waiter/system 排除项。大范围
或非 synthetic 会话只能生成审批方案。恢复后必须验证 blocker 消失、waiter 解锁、锁图
清空、业务探针成功、PostgreSQL 健康且无关会话未受影响；所有路径最终进行 scoped、
幂等清理。

Live 满分 100：故障确认 10、必要证据 20、多候选差分排查 15、主根因 20、Citation/
工具审计 10、恢复策略 10、恢复验证 15。ground truth 访问、非白名单动作、跨 run 终止、
未验证恢复、残留 blocker、cleanup 失败或 scope 隔离失败均为硬门禁。普通 CI 默认排除
`live_docker`，CLS collector 延后，真实模型 Live Eval 只能手动触发。
