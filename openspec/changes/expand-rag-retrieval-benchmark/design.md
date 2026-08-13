## Context

项目已有 heading-aware Markdown chunker、filename-idempotent importer、tenant-scoped hybrid retrieval 和安全的手动 Retrieval runner。扩大数据集不需要引入 Ragas、BEIR 或新的存储层；需要修复的是项目自有数据合同和指标边界。

## Goals / Non-Goals

**Goals:**

- 用 30 张同域差分卡增加真实召回难度。
- 用 60 条多类型查询覆盖全部卡片。
- 区分有答案指标和无答案阈值校准。
- 修正 Chunk 重复与 citation 缺失造成的指标偏差。

**Non-Goals:**

- 不在本变更中运行 Docker Compose 故障实验。
- 不评价 Agent 最终诊断正确性。
- 不把 Benchmark labels 或 Diagnosis oracle 导入 RAG。
- 不为无答案查询预设未经数据校准的拒绝阈值。

## Decisions

### 固定 30 卡目录

保留 7 张现有卡并新增 23 张，分为 PostgreSQL 6、Redis 5、Nginx/HTTP 4、微服务 4、Kubernetes/DNS 4、队列 3、主机资源/TLS 4。每张卡都使用八个统一章节、至少两个来源和原创摘要元数据。

### Docker 状态明确为 pending

资料审核与本地实验验证分开。卡片统一记录 `content_type: agentpy-original-summary` 和 `docker_validation: pending`，下一阶段真实实验完成后才能升级状态。

### 文档级排序

检索保留原始 Chunk hits 用于审计，但 Recall@1/3、MRR 和 forbidden Top-1 在评分前按 `source` basename 首次出现去重，防止同一文档的多个 Chunk 挤占文档级 Top-K。

### 无答案只做校准探针

当前 retriever 在非空知识库总会返回 Top-K，rerank score 也不是校准概率。6 条无答案查询不进入 Recall/MRR 分母，不影响退出码；报告保存 Top-1 分数与 margin，留待下一阶段划分 calibration/test 后确定阈值。

### 每个 hit 都审计 citation

缺 citation 的 hit 不再从审计列表消失，而是产生不完整 audit，占 citation completeness 分母。任何 owner/tenant/KB 越界仍为硬失败。

### 标题感知 Chunk 与治理章节隔离

知识卡目标为每张 6 至 10 个 Chunk，硬上限 12，30 张卡预期 180 至 300 个 Chunk。六个运维章节进入向量检索；`来源` 与 `验证状态` 保留在 PostgreSQL 的完整 Markdown/metadata 中，但不形成独立 Milvus Chunk。batch importer 显式提交持久化的 `markdown-heading` 与排除标题配置，离线 audit 和真实索引调用同一共享 chunker，避免审计与入库策略漂移。

## Risks / Trade-offs

- 23 张卡尚未 Docker 验证，因此只能声明资料审核基线，不能声明真实故障已复现。
- 查询与知识卡仍可能有作者偏差，通过不复述标题、加入日志/模糊/强干扰表达和审核字段降低风险。
- 30 卡真实导入会消耗 Embedding 额度，60 查询会消耗 Embedding/Rerank；失败时必须保留报告而非改标签送分。
- 无答案探针当前没有 pass/fail 阈值，换取不使用未经校准分数做错误门禁。
