## ADDED Requirements

### Requirement: Reviewed knowledge catalog contains exactly thirty cards

系统 SHALL 维护恰好 30 张 Markdown 差分排障知识卡，其中保留 7 张现有卡并新增批准设计中的 23 张卡。每张卡 SHALL 包含适用现象、候选原因、建议证据、如何区分、安全恢复边界、恢复后验证、来源和验证状态八个章节。

#### Scenario: Catalog is audited before import

- **WHEN** operator 对 `docs/knowledge-candidates` 执行离线 catalog audit
- **THEN** 文件集合 MUST 与批准目录完全相等，且每张卡 MUST 产生 1 至 6 个 heading-aware Chunk

### Requirement: Knowledge cards are sourced summaries with pending Docker validation

每张卡 SHALL 是 `agentpy-original-summary`，SHALL 记录至少两个精确来源 URL、来源许可证或 reference-only 状态、审核日期和 `docker_validation: pending`。系统 MUST NOT 将 pending 描述为已完成本地故障复现。

#### Scenario: A card has not yet been experimentally reproduced

- **WHEN** 卡片只完成资料审核而未运行下一阶段 Docker 实验
- **THEN** 其验证状态 MUST 保持 pending，且恢复建议 MUST 保留审批与禁止动作边界

### Requirement: Benchmark and diagnosis answers never enter the catalog

知识卡 MUST NOT 包含场景编号、ground truth/oracle 字段、Retrieval query ID、相关性标签、评分门槛、真实 owner/KB/document ID 或凭据。

#### Scenario: Catalog contamination is detected

- **WHEN** 内容审核发现 Benchmark label、Diagnosis answer 或敏感配置字段
- **THEN** 离线测试 MUST 失败且真实导入 MUST NOT 开始

### Requirement: Real catalog import stops on unsafe state

真实导入前 SHALL 执行 dry-run、Chunk audit 和 owner/KB/filename active-count 审计。任一 filename 存在多个 active 文档或任一卡 Chunk 数越界时 MUST 停止，且 MUST NOT 自动清理历史记录。

#### Scenario: Legacy duplicate filename exists

- **WHEN** 目标 owner 与知识库内某一批准 filename 存在两个或更多 active 文档
- **THEN** import workflow MUST 报告重复项并停止在 mutation 之前
