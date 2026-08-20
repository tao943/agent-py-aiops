# AgentPy 全量 Benchmark 正式验收设计

## 1. 目标

在不降低评分阈值、安全门禁、Validator 标准或恢复权限的前提下，完成以下正式验收：

- 10 个 Snapshot 场景全部使用真实 DashScope Chat、独立 Validator 和 30 卡 RAG，且
  `validity=valid`、`passed=true`、无 `hard_gate`；
- 4 个 Live 场景全部使用真实 Docker 故障、真实 DashScope Chat、30 卡 RAG 和腾讯云
  CLS，且 `validity=valid`、`passed=true`、无 `hard_gate`；
- 每次初次运行、失败、基础设施无效、中断和修复后重跑都使用独立 `run_id`，同时保存到
  worktree 外 Archive 和 PostgreSQL，不覆盖旧记录。

本轮不把离线 pytest、固定 Artifact 评分或仅 Docker driver 验证冒充真实 Benchmark 结果。

## 2. 当前基线

- Snapshot fixture 已有 10 个：`APY-002`、`APY-003`、`APY-006`、`APY-007`、
  `APY-011` 至 `APY-016`。
- 4 个 Live driver 已有：PostgreSQL 行锁、PostgreSQL 死锁、Redis maxclients、Nginx
  upstream timeout。
- 持久化历史中，Snapshot 只有 `APY-013` 曾真实通过，最新保存结果为 97 分；其余场景
  尚未通过正式口径。
- 4 个 Live driver 的故障注入、独立验证和清理曾通过，但现有完整 Agent Eval 均未通过。
- 当前主工作区私有配置仍使用 `qwen3.7-plus`、缺少独立 `validatorModel`，且
  `evaluation.archiveDir` 为空；可提交模板已经是目标配置，但 Git 不会覆盖被忽略的本机配置。

## 3. 配置与数据安全

正式运行前只修改被 Git 忽略的 `config/user.project.json`，保留现有 API Key、CLS
Secret、Logset 和 Topic，不打印或复制其值：

- `chatModel = qwen3.7-max`；
- `validatorModel = qwen3.8-max`；
- 两个模型均配置 `contextWindowTokens = 1000000` 和
  `structuredOutputMethod = json_mode`；
- `evaluation.archiveDir = D:\\桌面\\后端\\agent_py-evaluation-archive`；
- 保留当前 Embedding 和 Rerank 模型，避免在本轮改变已索引知识向量的模型口径。

隔离 worktree 不复制私有配置；所有正式命令显式读取主仓库的 `config/project.json`，由
配置加载器合并同目录的 ignored `user.project.json`。目标 worktree 必须保持这两个本机文件
不存在且未 staged，避免产生凭据副本。

任何日志、CLI 汇总、设计文档和提交均不得包含 API Key、云凭据、Prompt、模型原始响应、
Ground Truth、Oracle、私有推理或原始 CLS 日志。

## 4. 复用评估

### 4.1 项目内部直接复用

- Snapshot：`apps/backend/scripts/run_snapshot_benchmark.py`；
- Live：`super_ai.evaluation.live.cli` 与现有四个 scenario driver；
- RAG：现有 Milvus + BM25L + RRF + rerank 管线；
- 结果：`EvaluationRecorder`、`EvaluationArchive`、`EvaluationRepository`；
- 故障注入：`infra/live-eval` 和现有 PostgreSQL、Redis、Nginx Compose 资产。

### 4.2 GitHub 对照

- `promptfoo/promptfoo`（MIT）可执行通用 Prompt/Agent Eval，但不能直接复用当前私有
  Oracle、证据链和恢复安全评分；
- `confident-ai/deepeval`（Apache-2.0）提供通用 LLM Eval，但会引入第二套评分与 Judge
  抽象；
- `chaos-mesh/chaos-mesh`（Apache-2.0）适合 Kubernetes Chaos，本项目当前正式环境是
  Docker Compose。

结论采用“内部直接复用”；本轮不增加第三方依赖、原生二进制或外部服务。

## 5. 分阶段门禁

### 5.1 Gate 0：持久化与配置

1. 安全迁移本机模型和 Archive 配置；
2. 验证配置加载结果只输出模型名和布尔 readiness；
3. 验证 Archive 可创建、终结和读取临时验收记录；
4. 验证 PostgreSQL migration 为 head，Recorder 能同步并读取结果；
5. 删除专用临时 readiness 记录只允许使用测试隔离路径和事务回滚，不删除历史结果。

未通过 Gate 0 时不调用真实模型。

### 5.2 Gate 1：依赖 readiness

1. PostgreSQL、Redis、Milvus、etcd、MinIO 和 Nginx 健康；
2. 目标 owner/knowledge base 在 PostgreSQL 中有 30 个 `ready/indexed` 文档；
3. 使用项目内只读 audit 核对 PostgreSQL 的 30 个 document ID 与 Milvus scoped Chunk 的
   owner、tenant、knowledge base、document 集合完全对齐，每个文档至少一个 Chunk，且无
   orphan/unscoped Chunk；
4. Chat、Validator、Embedding 和 Rerank 分别通过最小真实 readiness；
5. CLS MCP ready，上传与检索使用配置的 Region、Logset 和 Topic；
6. Live driver cleanup 预检无上次运行残留。

任一外部依赖失败都记录为 readiness 失败，不产生误导性 Agent 零分。

### 5.3 Gate 2：10 个 Snapshot

按场景 ID 顺序执行，每个场景初次只运行一次，固定：

- `adapter=application`；
- `rag-mode=on`；
- 同一 owner、knowledge base、suite version 和 Git commit；
- 主模型 `qwen3.7-max`，Validator `qwen3.8-max`。

某场景失败时立即停止后续场景，使用已保存的安全失败分类定位单一原因。只有完成局部修复、
目标离线回归、Ruff 和 Pyright 后，才为该场景创建新 `run_id` 重跑。不得修改 Ground Truth、
答案、评分权重、阈值或隐藏信息以获得通过。

### 5.4 Gate 3：4 个 Live

按以下顺序执行：

1. `APY-LIVE-PG-LOCK-001`；
2. `APY-LIVE-PG-DEADLOCK-001`；
3. `APY-LIVE-REDIS-MAXCLIENTS-001`；
4. `APY-LIVE-NGINX-TIMEOUT-001`。

每个场景先执行不调用 Agent 的 cleanup、baseline、inject、fault confirmation 和 cleanup
预检。预检先从 Archive 和安全 Live report 枚举已知旧 Run ID，逐个执行 scoped cleanup，
再只读确认 PostgreSQL Live session/fixture、Redis benchmark client 和 Nginx 配置均无全局
残留；不得用新 Run ID 的空 cleanup 代替残留审计。通过后才执行一次
`evidence-source=cls` 的完整 Agent run。每次运行必须绑定唯一
`run_id`、`scenario_id`、`incident_id` 和 CLS 时间窗。失败后立即停止，保留现场安全审计并
执行 scoped、幂等 cleanup；完成局部修复和离线回归后再创建新 Run。

PostgreSQL/Redis 小范围白名单恢复必须重新验证资源归属；Nginx 保持 `proposal_only`，不得为
获得自动执行分而放宽人工审批边界。

## 6. 结果持久化与汇总

正式 runner 在外部调用前创建 `running` Envelope，终态统一保存为：

- `passed`；
- `failed`；
- `infra_invalid`；
- `interrupted`。

Archive 是数据库临时不可用时的保底真源；PostgreSQL 同步失败时保留
`database_pending`，基础设施恢复后使用现有 `reconcile` 补写。每个阶段结束后执行 Archive
schema/checksum audit、PostgreSQL 对账和安全汇总。旧 Run 不覆盖、不重标、不删除。

所有 14 个正式 Run 使用同一个安全 `acceptanceCampaignId`，该字段与 Git SHA 一起写入
Archive/PostgreSQL metadata。最终验收按 campaign 精确选择 Run，不使用易受后续普通重跑影响
的“全局 latest”口径。

Live runner 必须把内部 `finally` cleanup 结果写入同一 Run 的 `cleanupSucceeded` 指标；有效
失败、基础设施失败和中断也必须保存该指标。独立 cleanup CLI 只作补救，不得覆盖原终态；
若补救发生，则以同 campaign 的安全 cleanup audit 关联原 Run。

最终汇总只包含允许列表字段：Run ID、场景、Git commit、模型名、状态、有效性、分数、维度、
失败分类、Validator 来源/错误分类、工具审计、恢复模式、验证和 cleanup 状态。

## 7. 失败处理

- 模型、网络、CLS、Docker 或数据库不可用：`infra_invalid`，不计为 Agent 能力失败；
- Agent 有有效输出但未达阈值：`failed`，按 ScoreReason/安全错误分类修复；
- hard gate：立即停止同阶段后续场景，先修复隔离、权限或恢复边界；
- LLM Validator 不可用：只允许现有确定性 grounded fallback，保持人工复核和
  `executionPermitted=false`；
- 人工中断：保存 `interrupted` 并执行 Live scoped cleanup。

不得使用无限重试、批量掩盖失败、删除失败结果、降低阈值或只挑选最高分作为最终结论。

## 8. 验收标准

- 同一 `acceptanceCampaignId` 下 10/10 Snapshot 的目标 Run 为 `validity=valid`、
  `passed=true`、无 hard gate；
- 同一 `acceptanceCampaignId` 下 4/4 Live 的目标 Run 为 `validity=valid`、`passed=true`、
  无 hard gate；
- 每个 Live 的 fault confirmation、独立证据、恢复策略、恢复验证和 cleanup 均通过；
- Archive 与 PostgreSQL 对账为 0 conflict、0 pending；
- 目标 pytest、Ruff、Pyright 和相关 OpenSpec strict 校验通过；
- 最终文档明确列出全部 Run ID 和安全指标，并保留所有失败历史。

## 9. 明确不做

- 不在本轮增加未知告警的动态 Tool Router；
- 不接入 Promptfoo、DeepEval、Chaos Mesh 或第二套持久化系统；
- 不扩展知识卡、Retrieval query 或新增 Benchmark 场景；
- 不改变聊天 Agent、评分总权重、Ground Truth 或恢复授权边界。
