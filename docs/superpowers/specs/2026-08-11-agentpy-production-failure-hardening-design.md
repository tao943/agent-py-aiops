# AgentPy Benchmark 生产失败路径加固设计

**日期：** 2026-08-11

**状态：** 已批准，待实施

**目标：** 补齐 AgentPy Snapshot Benchmark 的并发、幂等、失败终态、CLI
错误合同和答案隔离测试，并修复测试暴露的生产级一致性问题。

## 1. 背景与现状

当前首个 Benchmark 切片已经覆盖 APY-003/APY-006、Snapshot MCP、结构化证据链、
确定性评分、PostgreSQL evaluation run/result 持久化和 application CLI adapter。
普通 CI 已执行离线 pytest、Ruff 和 Pyright。

现有实现仍有以下生产风险：

- `create_run` 先查询再插入；两个并发请求可能同时查询为空，随后触发唯一键竞争。
- Runner 创建 run 后若 adapter、artifact 校验、oracle、评分或持久化失败，run 可能永久
  停留在 `pending`。
- Runner 先完成 run、再保存 scorecard，两个独立事务可能留下 `completed` 但没有结果的
  半完成状态。
- 数据库约束已阻止一对多 scorecard，但应用层尚未完整验证重复写入和冲突后的安全恢复。
- CLI 已定义退出码，但缺少失败 JSON、脱敏和重复执行合同测试。
- 场景 loader 已递归拒绝部分答案键，Runner 也有路径边界检查，但路径穿越、更多嵌套答案
  伪装、`ReadGroundTruth` 越权和 application 输入边界尚未形成完整回归合同。

## 2. 约束与复用结论

- 保持 Python 3.10+、SQLAlchemy 2、PostgreSQL 16、pytest/pytest-asyncio。
- PostgreSQL 是 evaluation 状态和 scorecard 的唯一事实源；Redis 不参与评分正确性。
- 默认 CI 不调用 DashScope、CLS、Alertmanager、Milvus、Docker API 或真实业务服务。
- 不保存异常原文、API Key、token、password、ground truth 或模型私有思维链。
- 不在本次实现 CLI 自动重试、`retry_of_run_id`、Live fault 或恢复执行。
- 不新增依赖。

复用 SQLAlchemy 官方 PostgreSQL 方言的
`insert().on_conflict_do_nothing()`，在项目仓储边界内封装并发安全的 create-or-read。
SQLAlchemy 和 pytest 均为项目已有 MIT 依赖。OpenAI Evals 仅作为失败分类参考，不引入其
运行时或评分体系。

## 3. 运行状态与失败分类

evaluation run 保留现有 `pending`、`completed`，新增两个终态：

| 状态 | 含义 | CLI 退出码 | Scorecard |
|---|---|---:|---|
| `pending` | 已创建，尚未结束 | 不作为最终输出 | 无 |
| `completed` | Agent artifact 已评分并原子保存 | 0 或 1 | 必须有 |
| `agent_failed` | adapter 失败或返回非法 artifact | 2 | 无 |
| `infra_failed` | 场景、oracle、评分或基础设施持久化失败 | 2 | 无 |

`aiops_evaluation_runs` 新增可空 `failure_category`。它只保存固定 allowlist 分类：

- `adapter_error`
- `artifact_invalid`
- `scenario_error`
- `evaluation_error`
- `persistence_error`

数据库不保存 `str(exception)`、traceback 或外部服务返回体。日志沿用现有安全日志机制，
CLI 只输出固定错误描述和分类。

状态转换规则：

```text
pending -> completed
pending -> agent_failed
pending -> infra_failed
```

终态不可改回 `pending`，失败终态不可被普通 finalize 覆盖。相同失败操作重复执行返回
已有记录；使用不同失败分类覆盖已有终态会返回业务冲突。

## 4. PostgreSQL 并发与原子性

### 4.1 并发安全 create_run

`create_run` 使用一条 PostgreSQL insert：

```text
INSERT ... ON CONFLICT (run_id) DO NOTHING
-> SELECT run_id
-> 比较 scenario、mode、suite、agent version、model configuration
```

结果规则：

- 相同身份并发创建：所有调用返回同一记录，不向调用方暴露 `IntegrityError`。
- 不同身份竞争同一 `run_id`：一个身份成功，另一个得到稳定 `ValueError` 业务冲突。
- 冲突不污染后续 session/connection；同一仓储实例仍能读取和创建其他 run。

### 4.2 原子 finalize

新增应用接口：

```python
finalize_run(
    *,
    run_id: str,
    result_id: str,
    result: EvaluationResult,
    diagnostic_task_id: str | None,
) -> tuple[EvaluationRunRecord, EvaluationResultRecord]
```

它在同一事务中锁定 run、校验状态、写入 scorecard、把 run 更新为 `completed`，然后一次
commit。任一步骤失败都 rollback，因此不存在 `completed` 但没有 scorecard 的状态。

重复 finalize 使用相同 `result_id` 和等价结构化 scorecard 时幂等返回原记录；使用不同
结果身份或不同内容时拒绝覆盖。

现有 `complete_run` 和 `save_result` 暂时保留用于兼容和直接仓储测试，但
`SnapshotBenchmarkRunner` 只使用 `finalize_run`。

## 5. Runner 失败边界

Runner 在成功 `create_run` 后分阶段处理：

```text
load public scenario/snapshot
-> adapter
-> artifact identity validation
-> evaluator loads oracle
-> deterministic scoring
-> atomic finalize
```

分类规则：

- adapter 抛异常：调用 `fail_run(status="agent_failed", category="adapter_error")`。
- artifact 的 scenario/mode 不匹配：`agent_failed/artifact_invalid`。
- 场景、snapshot 或 oracle 解析失败：`infra_failed/scenario_error`。
- scorer 抛异常：`infra_failed/evaluation_error`。
- finalize 失败：尽力记录 `infra_failed/persistence_error`；若 PostgreSQL 整体不可用，
  保留原异常供 CLI 映射，不能伪称已成功持久化。

Runner 不自动重试 Agent。诊断错误不得通过重复调用“刷答案”。

## 6. CLI 错误合同

CLI 保持以下退出码：

- `0`：所有运行有效且通过。
- `1`：运行有效，但至少一个案例未通过。
- `2`：无效评测、agent failure 或 infrastructure failure。

失败输出为稳定 JSON，至少包含：

```json
{
  "validity": "invalid",
  "error": "Snapshot benchmark infrastructure failed.",
  "category": "persistence_error"
}
```

输出不得包含异常原文、API Key、ground-truth 值或本地绝对路径。`--runs 0`、未知场景、
路径穿越和依赖失败均返回 2。CLI 每次正常运行仍生成新 `run_id`；本次不增加自动重试
参数。

为便于测试，纯格式化、退出码和安全错误分类放在可导入模块；脚本只负责 argparse、
依赖装配和 `asyncio.run`。

## 7. 答案隔离边界

### 7.1 路径安全

Scenario ID 只允许单目录名。拒绝：

- `../APY-003`
- `..\\APY-003`
- 绝对 Windows/POSIX 路径
- 包含 `/` 或 `\\` 的值

路径检查在读取任何场景文件之前完成。

### 7.2 递归答案键

公开 `scenario.yaml` 任意深度出现以下规范化键均拒绝：

- `ground_truth`
- `oracle`
- `primary_cause`
- `contributing_causes`
- `causal_chain`
- `required_evidence`
- `required_rule_outs`
- `forbidden_claims`

键名匹配大小写不敏感，并统一 `-`/空格为 `_` 后检查。

### 7.3 Agent/application 边界

Agent adapter 只能收到 `PublicScenario` 和 `RuntimeMcpClient`。application adapter 创建的
diagnostic task payload、RAG 调用参数和报告 prompt 上下文只能来自公开场景、公开候选
假设和运行证据，不包含 oracle 路径或内容。

Snapshot MCP 不发现 `ReadGroundTruth`。直接调用未知工具必须抛 `McpClientError`，且不会
返回任何文件内容。若持久化 tool audit 中出现 `ReadGroundTruth`，scorer 保持现有硬门槛：
`validity=invalid`、`hard_gate=ground_truth_access`、`passed=false`。

## 8. 测试矩阵

### PostgreSQL Integration

- 相同身份并发创建同一 `run_id`。
- 不同身份并发竞争同一 `run_id`。
- 唯一冲突后仓储仍可继续查询和写入。
- 相同 result 重复 finalize 幂等。
- 不同 result/content 拒绝覆盖。
- scorecard 写入失败时 run 不变为 `completed`。

### Runner Contract

- adapter exception -> `agent_failed/adapter_error`。
- wrong scenario/mode -> `agent_failed/artifact_invalid`。
- scorer/finalize exception -> `infra_failed`。
- 所有失败 run 都不保持 `pending`。
- 成功路径使用 atomic finalize 并关联 diagnostic task ID。

### CLI Contract

- pass/fail/invalid 分别为 0/1/2。
- `--runs 0` 和依赖异常返回固定安全 JSON。
- 输出不包含注入异常中的 secret、ground truth 或绝对路径。
- 重复执行产生不同 run ID，不覆盖历史结果。

### Answer Isolation

- POSIX、Windows 和绝对路径穿越。
- 多层嵌套的所有禁用答案键。
- application diagnostic input 和测试用 Prompt/RAG 输入不含答案。
- Snapshot discovery/call 均无法使用 `ReadGroundTruth`。
- 恶意 audit 触发 scorer invalid 硬门槛。

## 9. CI 与验收

新增测试位于 `apps/backend/tests`，由现有 GitHub Actions `backend-tests` 的
`uv run pytest` 自动执行。所有测试只使用本地 PostgreSQL service container 和离线 fake；
不新增 secrets。

验收条件：

- 并发相同身份无原始数据库异常。
- 所有已创建的失败 run 最终为 `agent_failed` 或 `infra_failed`。
- 不存在 `completed` 且缺少 scorecard 的 Runner 路径。
- CLI 失败输出通过脱敏断言。
- ground truth 不进入 Agent、Prompt、RAG、报告或工具结果。
- 全量 pytest、Ruff、Pyright 和 OpenSpec strict validation 通过。

## 10. 明确非目标

- CLI 自动重试和 `retry_of_run_id`。
- 真实 DashScope benchmark。
- Docker Live fault、L1/L2 recovery、Judge。
- 新的队列、事件表或 Redis 状态机。
- 修改确定性评分权重。
