# evaluation-result-history Specification

## Purpose
TBD - created by archiving change persist-evaluation-results. Update Purpose after archive.
## Requirements
### Requirement: Automatic evaluation run persistence

系统 SHALL 在 Snapshot、Retrieval、Live 和 CLS 正式测评开始前建立运行记录，并在通过、评分失败、
Agent 失败、基础设施失败或可捕获中断后保存终态。

#### Scenario: Retrieval threshold failure is retained
- **WHEN** Retrieval 测评完成但任一批准阈值未达标
- **THEN** 系统 MUST 保存完整安全指标、`status=failed` 和退出码 1

#### Scenario: CLS infrastructure timeout is retained
- **WHEN** CLS 日志索引轮询超时
- **THEN** 系统 MUST 保存 `status=infra_invalid`、允许列表内的失败分类和退出码 2

#### Scenario: Database is unavailable at run start
- **WHEN** running Artifact 已成功写入但 PostgreSQL 暂时不可用
- **THEN** 系统 MUST 继续真实测评、保存本地终态并将数据库标记为待对账

#### Scenario: Evaluation is cancelled
- **WHEN** Snapshot、Retrieval 或 Live/CLS 收到可捕获取消或终止信号
- **THEN** 系统 MUST 在传播取消前保存 `status=interrupted`

### Requirement: Local archive and PostgreSQL reconciliation

系统 SHALL 通过稳定 run ID 和内容 checksum 对账 PostgreSQL 与 worktree 外本地归档，且 SHALL NOT
静默覆盖身份冲突。

#### Scenario: PostgreSQL is temporarily unavailable
- **WHEN** 安全终态 Artifact 已写入而 PostgreSQL 写入失败
- **THEN** 后续 reconcile MUST 幂等补写数据库且保持原始运行时间和结果

#### Scenario: Existing run has different content
- **WHEN** 相同 run ID 在数据库或归档中具有不同 checksum
- **THEN** 系统 MUST 报告冲突并 SHALL NOT 覆盖任一终态

#### Scenario: Legacy database row has no checksum
- **WHEN** 旧 Snapshot Run 的 Artifact checksum 为 NULL
- **THEN** reconcile MUST 从数据库规范导出、校验 Artifact 并只回填相同 checksum

### Requirement: Evaluation answer isolation

系统 SHALL 拒绝把 Ground Truth、oracle、primary cause、Prompt、私有推理、凭据或未脱敏日志写入
运行归档。

#### Scenario: Historical file contains forbidden fields
- **WHEN** 导入器发现任一嵌套字段名规范化后命中禁止语义
- **THEN** 文件 MUST 被报告为 rejected 且 SHALL NOT 进入共享归档或 PostgreSQL

#### Scenario: Forbidden field uses a naming variant
- **WHEN** 字段使用 `groundTruth`、`primaryCause` 或其他大小写/分隔符变体
- **THEN** 系统 MUST 在规范化后同样拒绝该字段

### Requirement: Recoverable historical summary

系统 SHALL 汇总所有可证明的历史运行，并明确标记 reconstructed、conflict、database pending 和不可恢复边界。

#### Scenario: Live task has audit but no score artifact
- **WHEN** 数据库存在 Live 诊断审计但没有独立结果
- **THEN** 汇总 MUST 只生成 `provenance=reconstructed` 记录且 SHALL NOT 补造评分指标

#### Scenario: Historical import is repeated
- **WHEN** 同一组历史来源被再次导入
- **THEN** 已接受记录 MUST 计为 duplicate，新增 imported MUST 为零且不得产生新冲突
