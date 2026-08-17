# Evaluation Result Persistence and Historical Summary Design

## 目标

统一保存 AgentPy 的 Snapshot、Retrieval、Docker Live 和 CLS Live 测评结果，使每次正式运行无论通过、评分失败、Agent 失败还是基础设施失败都可以追溯。同时把当前仍可恢复的历史测评归并到一份可查询总览中，避免结果继续散落在 PostgreSQL、不同 Git worktree 的 `var/` 目录和终端日志里。

本设计采用用户确认的 **PostgreSQL + 本地共享归档** 方案。运行结果不提交到 GitHub；代码、数据库迁移和归档格式仍通过 Git 管理。

## 当前事实与恢复边界

截至 2026-08-16，当前 PostgreSQL 中可识别：

- 15 个 `aiops_evaluation_runs`；
- 15 个 `aiops_evaluation_results`；
- 27 个 Benchmark 相关诊断任务；
- 309 个 Workflow Steps；
- 157 份 Evidence。

数据库中的 15 个 Snapshot Run 都存在评分结果。Retrieval 结果只存在于本地 JSON；Live/CLS 同时存在数据库诊断审计和零散 JSON，但并非每一次都有独立结果文件。`var/` 被 Git 忽略，删除 worktree 会丢失其中的文件。

历史汇总遵循“只恢复可证明的数据”：

- 数据库 Run/Result 可以直接导出；
- 现存 JSON 可以校验、脱敏、规范化后导入；
- 只有诊断审计而没有正式报告的 Live 运行可以生成 `reconstructed` 记录，但不得补造缺失指标；
- 2026-08-13 以前未入库且本地文件已删除的运行无法恢复；
- 普通 pytest、Ruff、Pyright 和 GitHub Actions 日志不作为 Benchmark Run 导入。

## 约束

- PostgreSQL 是运行态查询和聚合的事实源；本地共享目录是可迁移、可恢复的不可变归档。
- 归档目录必须位于 Git worktree 之外，并通过本地项目 JSON 配置显式指定。
- 不引入 MLflow、Langfuse、Phoenix、W&B 或新的外部服务。
- 不保存 API key、密码、Token、完整 Prompt、原始模型响应、私有推理、`ground_truth.yaml` 内容或未脱敏日志。
- 不改变现有 Benchmark 权重、阈值、Ground Truth、Agent Workflow 或恢复授权策略。
- 运行结果不提交到 GitHub，归档目录必须保持在仓库之外或明确被 Git 忽略。
- 保持 Python 3.10、PostgreSQL-only、strict Pyright、Ruff 和现有异步 SQLAlchemy 架构。

## 复用决策

成熟评测系统通常把 Run 元数据和指标保存在数据库，把完整报告作为独立 Artifact 保存：MLflow Tracking 保存 Run、参数、指标、时间戳和 Artifact；Langfuse Experiment 保存数据集版本、输出、评分和 Trace，并支持按 Run 比较；Phoenix 和 Promptfoo 同样把可重复执行身份与报告 Artifact 分离。

采用 **reference only + wrapped adoption**：参考上述生命周期与 Artifact 模式，不引入它们的服务或 SDK；包装并复用项目现有 `EvaluationRepository`、`aiops_evaluation_runs`、`aiops_evaluation_results`、安全字段过滤和 JSON 序列化。Retrieval 与 Live 不再建立各自的平行保存机制，而是接入统一运行存储。

## 选定架构

```text
Snapshot CLI ─┐
Retrieval CLI ├─> EvaluationRunStore ─┬─> PostgreSQL
Live/CLS CLI ─┘                        └─> Shared Artifact Archive
                                             │
Historical importer ─────────────────────────┤
                                             └─> index.jsonl + summary.md
```

### 统一运行身份

每次正式运行具有稳定 `run_id` 和以下公共元数据：

- `evaluation_kind`：`snapshot`、`retrieval` 或 `live`；
- `scenario_id` 或 Retrieval suite ID；
- `suite_version`、数据集/查询集 checksum；
- Git SHA、Workflow/Agent version；
- 脱敏后的模型配置、RAG 模式和 Live evidence source；
- `created_at`、`started_at`、`completed_at`、`duration_ms`；
- `status`、`validity`、`failure_category`；
- 可选 `diagnostic_task_id`；
- 结果指标和允许列表内的失败原因；
- Artifact schema version 和内容 checksum。

已有 Live CLI 显式传入的 `run_id` 保持兼容。Snapshot 与 Retrieval 在未指定时自动生成；重复使用同一 `run_id` 必须幂等，身份元数据不一致时拒绝覆盖。

### 运行生命周期

统一状态机为：

```text
created -> running -> passed
                  -> failed
                  -> agent_failed
                  -> infra_invalid
                  -> interrupted
```

CLI 在调用 LLM、Milvus、CLS 或故障驱动器之前先建立 `running` 记录和最小安全 Artifact。最终结果在统一 `finally` 边界封装并保存，不能再只在成功路径或提供 `--output` 时写文件。

- 正常通过：保存评分、指标和 `passed`。
- 合法运行但未达阈值：保存完整评分和 `failed`，CLI 保持退出码 1。
- Agent/Workflow 失败：保存安全错误分类和 `agent_failed`。
- 数据库、模型、Milvus、Docker 或 CLS 等基础设施失败：保存 `infra_invalid`。
- `KeyboardInterrupt`、进程终止等可捕获中断：保存 `interrupted`。
- 无法捕获的硬崩溃会留下 `running`；后续汇总或新运行通过 stale-run reconciliation 标记为 `interrupted`，同时保留原始时间戳。

### 双写顺序与失败语义

本地归档承担数据库不可用时的灾备，因此每个生命周期更新采用以下顺序：

1. 构造安全、版本化的 Run envelope；
2. 使用同目录临时文件和原子重命名写入本地归档；
3. 幂等写入 PostgreSQL；
4. 数据库失败时保留不可变 Artifact，由后续 reconcile 对账识别并补写数据库；
5. 本地归档本身无法写入时以基础设施错误退出，不声称运行已可靠保存。

该顺序不提供跨文件系统与 PostgreSQL 的分布式事务，但确保至少存在一份可重放的安全记录。`reconcile` 根据 `run_id` 和 checksum 将缺失记录补回 PostgreSQL。

### PostgreSQL 模型

保留现有 Snapshot 表和 Repository 合同，并把它们扩展为通用 Evaluation Run：

- `aiops_evaluation_runs` 增加测评类型、Artifact schema/checksum 等通用身份字段；
- `aiops_evaluation_results` 保留 Snapshot 固定维度，同时允许 Retrieval 和 Live 保存类型化 `metrics`/`result_payload`；
- 数据库迁移为现有 15 个 Run 回填 `evaluation_kind=snapshot`；
- Run ID 和一对一 Result 唯一约束继续承担幂等保护；
- 所有动态 JSON 入库前经过递归 secret-key 检查和允许列表过滤。

不为三类测评分别创建三套 Run 表，避免汇总和生命周期语义再次分裂。

### 本地共享归档

归档根目录只从本地 JSON 的 `evaluation.archiveDir` 读取，不使用环境变量。配置值必须解析为 worktree 外的绝对路径。可提交模板保留空值，当前机器的 `config/user.project.json` 建议配置为：

```text
D:\桌面\后端\agent_py-evaluation-archive
```

目录布局：

```text
agent_py-evaluation-archive/
  snapshot/YYYY/MM/<run-id>.json
  retrieval/YYYY/MM/<run-id>.json
  live/YYYY/MM/<run-id>.json
  index.jsonl
  summary.md
```

每个 `<run-id>.json` 是当前 Run 的唯一规范 Artifact。生命周期更新只允许从早期状态前进到终态；终态 Artifact 不可覆盖为另一个结果。`index.jsonl` 和 `summary.md` 是可重建视图，不是事实源。

### 汇总与历史导入

新增只读汇总/幂等导入命令，分两阶段工作：

1. `import-history` 扫描明确传入的历史目录与当前 PostgreSQL；
2. `summarize` 从 PostgreSQL 和共享归档生成索引与 Markdown 总览。

导入器不自动遍历整个磁盘。候选文件必须通过 schema、路径、secret 扫描和身份校验；使用 `run_id + checksum` 去重。相同 Run ID、不同内容视为冲突并进入报告，不静默覆盖。原文件始终保留。

`summary.md` 至少展示：

- 总运行数及 Snapshot/Retrieval/Live-CLS 分类；
- pass、valid fail、agent fail、infra invalid、interrupted 数量；
- 场景覆盖、最近运行、Git SHA 与 suite version；
- Snapshot 平均分和失败原因；
- Retrieval Recall@1/3、MRR、citation completeness；
- Live verification、cleanup、evidence source 和 failure stage；
- `reconstructed`、数据库待同步和冲突记录。

## 安全与答案隔离

归档使用明确允许列表，不直接序列化任意异常、对象或 Workflow state。字段名递归标准化后，`api_key`、`secret`、`password`、`token` 等命中即拒绝。以下内容不得进入 Artifact：

- Ground Truth、oracle、primary cause 或隐藏评分 milestones；
- 完整 Prompt、Chain-of-Thought、原始模型响应；
- CLS 原始日志正文、数据库连接串、用户私有文档正文；
- 环境变量、异常堆栈和未经分类的第三方响应。

诊断证据只保存当前 Benchmark 已允许公开的 Evidence ID、类型、来源和摘要字段。历史导入发现禁止字段时隔离文件并报告，不进行“尽量导入”。

## 测试设计

### 单元测试

- 三类 payload 规范化为统一 envelope；
- secret、ground truth、路径穿越和未知字段被拒绝；
- 原子文件写入、checksum、终态不可覆盖；
- 相同 Run ID 相同内容幂等，不同内容冲突；
- Snapshot、Retrieval、Live/CLS 的退出码和状态映射正确；
- summary 对缺失指标和 `reconstructed` 记录不伪造数值。

### PostgreSQL 集成测试

- 新迁移回填已有 Snapshot Run；
- running 到各终态的合法转换；
- 并发创建相同 Run ID、重复保存 Result 和唯一约束冲突安全恢复；
- Archive 已写但数据库失败后可以 reconcile；
- 数据库已有而 Archive 缺失时可以安全导出；
- owner/diagnostic task 关联和删除行为保持现有合同。

### CLI 与失败路径测试

- 不提供 `--output` 也自动保存；保留该参数时仅作为额外导出，不影响规范归档；
- Snapshot 未达分、Retrieval 未达阈值、Live recovery denied 都保存；
- LLM、Milvus、PostgreSQL、Docker、CLS 超时和中断均产生安全终态；
- 本地归档失败时返回退出码 2；数据库失败但归档成功时结果可重放；
- `report`、`summarize` 和历史导入不读取 Ground Truth。

### 历史验收

- 15 个已入库 Snapshot Run 全部出现在共享归档和总览；
- 所有现存 Retrieval JSON 均被识别、去重并标明来源；
- Live/CLS 独立报告与仅有数据库审计的运行被明确区分；
- 最新 APY-013 `eval-52e52567c646499abac4790d51df4906` 的 89 分结果可查询；
- 汇总明确报告无法恢复的数据边界，不把 CI 日志计入 Benchmark。

## 非目标

- 本阶段不修复暂停中的 Qwen thinking-mode Validator 问题；
- 不新增 Web Dashboard、对象存储、远端 Artifact 服务或 GitHub Actions 上传；
- 不修改 Benchmark 场景、RAG 卡片、评分规则或 Agent 决策链；
- 不删除历史 worktree 或原始 `var/` 文件；
- 不保证恢复从未落盘、从未入库或已经被删除的历史运行。

## 验收标准

1. Snapshot、Retrieval、Live 和 CLS 的成功与所有受支持失败路径都会自动保存。
2. PostgreSQL 与共享归档可通过 `run_id` 对账，缺失一侧可以幂等修复。
3. 所有可恢复历史结果得到一份来源明确、无伪造指标的汇总。
4. 删除任一 Git worktree 不会删除共享归档。
5. 归档中不存在密钥、Ground Truth、原始 Prompt、私有推理或未脱敏日志。
6. 相关单元、PostgreSQL 集成、CLI 回归、Ruff 和 strict Pyright 全部通过。
