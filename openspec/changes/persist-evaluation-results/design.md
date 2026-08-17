## Context

PostgreSQL 已保存 15 个 Snapshot Run/Result，但 Retrieval 和部分 Live/CLS 结果只存在不同 worktree
的 `var/benchmarks`。现有三个 CLI 使用不同的保存路径，且 Snapshot runner 自己管理数据库生命周期，
无法形成统一的失败语义、归档合同和历史总览。

## Goals / Non-Goals

**Goals:**

- 所有正式测评运行都先建立安全运行身份，并最终保存真实结果或明确失败类别。
- PostgreSQL 负责查询聚合，worktree 外本地目录负责可迁移 Artifact 和数据库故障兜底。
- 通过稳定 run ID、规范 JSON checksum 和幂等写入完成双向对账。
- 历史迁移只恢复可证明数据，并阻止答案、凭据和私有内容进入归档。

**Non-Goals:**

- 不修改 Benchmark 场景、评分权重、RAG 卡片、Agent Workflow 或恢复授权。
- 不新增 Web Dashboard、远端 Artifact Store、外部评测平台或 GitHub 运行产物。
- 不恢复从未入库、从未落盘或已经删除的数据。

## Decisions

### 1. 版本化安全 Envelope

三类测评统一为 `EvaluationRunEnvelope v1`，包含 kind、场景/套件、状态、时间、脱敏配置、指标、
结果摘要、provenance 和可选诊断任务 ID。metadata、metrics 和 result payload 各自使用字段允许列表；
递归字段名规范化后拒绝 secret、Prompt、Ground Truth、oracle、primary cause 和私有推理语义。

### 2. Artifact-first 与数据库待同步

开始阶段先原子写 `running` Artifact，再尝试 PostgreSQL。数据库不可用不会阻止真实测评；终态先
写本地真实结果，再幂等补写数据库。数据库仍不可用时 CLI 返回基础设施退出码 2，而 Artifact 保留
真实 Benchmark 结果，后续 reconcile 补写。身份冲突不是可忽略的可用性故障，必须 fail closed。

### 3. 不可变终态和 stale running

Artifact 使用同目录临时文件、flush/fsync 和原子替换。`running` 只允许前进到一个终态，终态不能
被另一结果覆盖。reconcile 只把超过六小时的 stale running 标为 interrupted，新运行不受影响。

### 4. 泛化现有 PostgreSQL 表

复用 `aiops_evaluation_runs` 和 `aiops_evaluation_results`，增加 kind、schema/checksum、provenance、
安全 run metadata、metrics 和 result payload。旧 Snapshot 行回填 kind，并按旧 Result passed 状态迁移终态。旧 checksum
保持 NULL，首次规范导出后在行锁内安全回填。

### 5. 明确来源的历史恢复

导入器只扫描命令显式给出的目录，支持已知 Snapshot、Retrieval 和 Live 安全 JSON。run ID 与 checksum
均相同视为重复；同 run ID 不同内容视为冲突。数据库只有 Live 诊断审计时生成 reconstructed 记录，
保持 passed/metrics 缺失，不把缺失值变成零。

## Risks / Trade-offs

- 本地目录和 PostgreSQL 不具备跨存储事务；Artifact-first 和 reconcile 提供可恢复的最终一致性。
- 数据库不可用时 CLI 仍会消耗 LLM/CLS 资源，但这是保留真实测评结果而非永久 running 的必要选择。
- 绝对本地路径降低可移植性；只在被忽略的用户 JSON 中保存具体路径，仓库模板保持空值。

## Verification

- 单元测试覆盖 Envelope、路径、原子写、字段隔离、状态转换和 checksum。
- PostgreSQL 测试覆盖迁移回填、并发幂等、唯一冲突和 NULL checksum 回填。
- 三类 CLI 覆盖 pass、valid fail、Agent/infra failure、取消和 DB unavailable。
- 历史导入重复执行必须 `imported=0` 且无新冲突。
- Ruff、strict Pyright、完整 pytest、OpenSpec strict/all 和 VitePress build 全部通过。
