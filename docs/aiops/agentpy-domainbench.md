# AgentPy DomainBench：首个 Snapshot 诊断切片

当前版本已经打通“公开场景 → 冻结工具 → Agent 诊断 → 结构化证据链 →
确定性评分 → PostgreSQL 留档”的最小闭环。它用于比较 Agent 版本，不是
OpenSRE 官方 Benchmark，也不使用 OpenSRE 的评分体系。

## 当前两个差分场景

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

## 当前阶段边界

这个切片尚未实现 L1/L2 恢复、六个 Live 场景、可选 Judge、剩余八个 Snapshot、
故障注入/清理以及 before/after 聚合看板。当前的“闭环”止于诊断、证据、评分和留档；
自动恢复、人工审批、真实 Docker 故障验证将在后续独立计划中实现。
