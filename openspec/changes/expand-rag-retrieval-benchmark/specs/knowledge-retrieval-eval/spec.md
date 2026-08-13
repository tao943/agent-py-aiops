## MODIFIED Requirements

### Requirement: Retrieval evaluation labels are answer-free and categorized

系统 SHALL 加载恰好 60 条独立 Retrieval labels，分类数量为明确组件 12、模糊现象 14、日志指标 12、口语扰动 8、跨组件强干扰 8、无答案探针 6。54 条有答案查询 SHALL 覆盖全部 30 张卡；6 条探针 SHALL 声明空相关文档和 `expected_no_answer=true`。

#### Scenario: Expanded reviewed labels are loaded

- **WHEN** 60 条 labels 通过 strict loader
- **THEN** ID MUST 唯一、分类分布 MUST 精确匹配、答案查询 MUST 有相关文档、probe MUST 无相关文档，且查询 MUST 不包含场景或答案字段

### Requirement: Retrieval metrics use unique document ranking

Recall@1、Recall@3、MRR 和禁止 Top-1 SHALL 按 source basename 首次出现去重后的文档序列计算。原始 Chunk hits MAY 保留用于审计，但 MUST NOT 重复占用文档级排名。

#### Scenario: Multiple chunks belong to the same document

- **WHEN** 原始排名为 target chunk 0、target chunk 1、alternative chunk 0
- **THEN** 文档级排名 MUST 为 target、alternative，且 Top-K 分母 MUST NOT 把 target 计两次

### Requirement: No-answer probes are diagnostic until calibrated

无答案探针 SHALL 记录 Top-1 分数和排序 margin，但 MUST NOT 进入 Recall/MRR 分母或 CLI pass/fail 门槛。系统 MUST NOT 使用未经 calibration/test 分离验证的固定阈值。

#### Scenario: Retriever returns a document for an out-of-scope probe

- **WHEN** 非空知识库为 probe 返回 Top-K
- **THEN** runner MUST 保存安全的分数诊断，但 MUST NOT 将其当作相关文档命中或直接判定运行失败

### Requirement: Citation completeness audits every hit

每一个返回 hit SHALL 对应一个 citation audit 分母项。citation 缺失、错配或缺少 chunk/document/knowledge-base/vector/rerank 字段 SHALL 计为不完整，且 MUST NOT 被静默跳过。

#### Scenario: One of two hits has no citation

- **WHEN** retriever 返回两个 hit 但只返回一个完整 citation
- **THEN** citation completeness MUST 为 0.5

### Requirement: Real expanded retrieval remains manual and isolated

真实 60 查询运行 SHALL 要求显式 owner/KB，顺序执行并输出不含正文、excerpt、凭据、原始配置或 Diagnosis 答案的 JSON。真实 Embedding/Rerank MUST NOT 进入普通 CI。

#### Scenario: Thirty-card baseline is executed

- **WHEN** 30 张卡的 PostgreSQL/Milvus 状态完成核验
- **THEN** operator MAY 执行一次真实 60 查询 Eval，并 MUST 保存模型、文档级指标、probe diagnostics、耗时和安全 ranked-hit metadata
