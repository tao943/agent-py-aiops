## ADDED Requirements

### Requirement: Retrieval evaluation labels are answer-free

系统 SHALL 从独立 YAML 加载检索查询 ID、查询文本、相关文档、可接受 Top-K 和禁止 Top-1 文档，且 SHALL 拒绝重复 ID、空相关文档、越界 Top-K 或包含场景/答案标识的查询。

#### Scenario: Six reviewed queries are loaded

- **WHEN** PostgreSQL 与 Redis 各三条查询被加载
- **THEN** 每条查询 MUST 不包含 scenario ID、oracle mechanism、trigger 或 evidence ID，且 MUST 至少声明一个相关文档

### Requirement: Retrieval metrics are deterministic and diagnosis-independent

系统 SHALL 只根据结构化排名和引用计算 Recall@1、Recall@3、MRR、禁止文档 Top-1 比例与引用完整率，且 MUST NOT 根据知识卡关键词评价最终诊断正确性。

#### Scenario: Relevant document is first with complete citation

- **WHEN** 查询的相关文档排名第一且引用具有 chunk、document、knowledge-base、vector score 和 rerank score
- **THEN** 该查询 MUST 命中 Recall@1 与 Recall@3，reciprocal rank MUST 为 1，引用 MUST 计为完整

#### Scenario: Result crosses a tenant boundary

- **WHEN** 命中或引用不属于明确请求的 owner 与 knowledge base
- **THEN** 手动 runner MUST 拒绝该运行且 MUST NOT 将越权结果计入指标

### Requirement: Real retrieval evaluation remains manual

真实检索 runner SHALL 要求显式 owner 与 knowledge-base ID，SHALL 顺序执行查询，并 SHALL 输出不含文档正文、凭据或原始配置的安全 JSON。真实 Embedding/Rerank MUST NOT 进入普通 CI。

#### Scenario: Operator runs the real retrieval evaluation

- **WHEN** operator 提供授权 owner、knowledge base 和本地模型配置
- **THEN** runner MUST 保存排名标识、双分数、模型名、耗时和聚合指标，且 MUST NOT 输出正文或秘密
