# Retrieval Citation 通道一致性审计设计

**日期：** 2026-08-13

**状态：** 已确认，待用户审阅与实施计划

**目标：** 修正 Retrieval Benchmark 将合法 BM25-only 命中误判为 Citation 不完整的问题，同时保持向量召回、BM25、RRF 融合、rerank 和最终排名完全不变。

## 1. 问题

当前 `RetrievalCitationAudit.complete` 要求每个返回 hit 都具有 `vectorScore`。混合检索先分别取得向量与 BM25 候选，再经 RRF 合并和 rerank；因此，一个只进入 BM25 Top-K 的候选可以合法地具有 BM25、RRF 和 rerank 证据，但没有 `vectorRank` 和 `vectorScore`。

已有真实 60-query 报告的 180 个 hit 中，有 3 个属于这种 BM25-only 候选。旧规则把它们计为不完整，令 Citation Completeness 从应有的 `1.0` 变为 `0.9833333333`。这属于审计口径错误，不是召回数据丢失。

## 2. 范围边界

本次只修改：

- Citation audit DTO 和完整性判定；
- benchmark runner 从 hit/citation 映射的审计字段；
- content-free JSON 报告中的检索通道与诊断覆盖率；
- 单元测试、CLI 测试、OpenSpec 和相关说明文档。

本次不修改：

- 向量召回和 BM25 的候选数量；
- RRF 公式、权重或排序；
- rerank 模型、分数或排序；
- Benchmark 查询、标签、阈值和通过门槛；
- Citation Completeness 的分母规则；
- 数据库、Milvus 或知识卡内容。

不得用 `0` 伪造缺失分数，不得过滤 BM25-only 或 vector-only 结果，也不得为了让指标通过而改变排名。

## 3. 采用方案

采用“身份完整 + 通道一致 + 融合与重排完整”的审计模型。它记录每个最终 hit 实际参加了哪些召回通道，而不要求每个 hit 同时命中向量和 BM25。

不采用以下方案：

- **继续强制 vector score：** 会持续误判合法 BM25-only 候选。
- **只检查稳定 ID 与最终分数：** 无法发现 rank 存在但对应 score 丢失的溯源损坏。
- **把空分数改为零：** 混淆“未参与通道”和“参与但得分为零”，产生虚假证据。

## 4. Citation Audit 合同

`RetrievalCitationAudit` 增加并审计以下字段：

```python
chunk_id: str
document_id: str
knowledge_base_id: str
vector_rank: int | None
bm25_rank: int | None
rerank_rank: int | None
vector_score: float | None
bm25_score: float | None
rrf_score: float | None
rerank_score: float | None
```

完整性由五项共同决定：

1. `chunk_id`、`document_id`、`knowledge_base_id` 均非空。
2. 向量通道的 rank 与 score 同时存在或同时为空。
3. BM25 通道的 rank 与 score 同时存在或同时为空。
4. 至少一个召回通道参与，即 `vector_rank` 或 `bm25_rank` 非空。
5. `rrf_score`、`rerank_rank`、`rerank_score` 均非空。

因此：

| 命中类型 | 结果 |
|---|---|
| BM25-only，BM25/RRF/rerank 字段齐全 | 完整 |
| vector-only，vector/RRF/rerank 字段齐全 | 完整 |
| hybrid，两个通道及 RRF/rerank 字段齐全 | 完整 |
| rank 存在但相应 score 缺失 | 不完整 |
| score 存在但相应 rank 缺失 | 不完整 |
| 两个召回通道都未参与 | 不完整 |
| 缺 RRF 或 rerank rank/score | 不完整 |

缺 Citation 的 hit 仍必须产生一个不完整 audit 并进入分母。owner、tenant 或 knowledge-base 越界仍是硬失败。

## 5. 报告合同

每条 hit 的 content-free JSON 增加：

```json
{
  "vectorRank": null,
  "bm25Rank": 3,
  "rerankRank": 2,
  "vectorScore": null,
  "bm25Score": 4.72,
  "rrfScore": 0.0161,
  "rerankScore": 0.84,
  "retrievalChannels": ["bm25"]
}
```

`retrievalChannels` 只允许 `vector` 和 `bm25`，由非空 rank 推导，不能根据 score 猜测。

聚合报告增加：

- `vectorChannelCoverageRate`：参与向量召回的 hit 比例；
- `bm25ChannelCoverageRate`：参与 BM25 召回的 hit 比例；
- `hybridChannelCoverageRate`：同时参与两个通道的 hit 比例。

这些覆盖率只用于诊断检索构成，不参与 CLI 通过判定。`citationCompletenessRate == 1.0` 仍是门禁。

## 6. 复用评估

直接复用项目已有的 `vector_rank`、`bm25_rank`、`rerank_rank`、`vector_score`、`bm25_score`、`rrf_score` 和 `rerank_score` 溯源字段，不新增依赖。

GitHub 代码检索发现 `Jamie-Holding/openchambers`、`camel-ai/camel` 和 `topoteretes/cognee` 等实现也将向量/BM25 rank 视为可选字段，作为参考即可，不复制实现。补充检索 Citation completeness 时 GitHub 返回 HTTP 503；这是搜索失败，不能解释为没有候选。当前项目内部合同已足以完成改造。

复用结论：内部字段直接采用；外部项目仅供语义参考；不引入第三方评测框架。

## 7. TDD 与验证

先写并观察以下失败测试：

1. BM25-only Citation 完整。
2. vector-only Citation 完整。
3. hybrid Citation 完整。
4. vector rank/score 不一致时不完整。
5. BM25 rank/score 不一致时不完整。
6. 没有召回通道时不完整。
7. 缺少 RRF 或 rerank rank/score 时不完整。
8. runner 完整映射 rank/score 并输出 `retrievalChannels`。
9. 缺 Citation 的 hit 仍产生不完整的分母项。
10. 三项通道覆盖率按所有返回 hit 计算，且不改变 CLI 门禁。

验证顺序：

1. 聚焦运行 evaluation 与 benchmark CLI 测试，确认 RED 原因是新字段/语义尚未实现。
2. 写最小实现并让聚焦测试转绿。
3. 运行相关 retrieval 回归测试。
4. 运行 Ruff、Pyright 和 OpenSpec 校验。
5. 优先使用不含正文的既有报告或测试 fixture 做离线重算；仅在无法离线验证时才重新执行真实 60-query Eval，以避免不必要地消耗模型额度。

## 8. 验收标准

- 混合检索及最终排名与修改前一致。
- 合法 BM25-only、vector-only 和 hybrid hit 均可判为完整。
- 任一通道的 rank/score 矛盾均判为不完整。
- 所有最终 hit 均要求 RRF 和 rerank 证据。
- 缺 Citation 的 hit 不会从完整率分母消失。
- JSON 报告能明确展示每条 hit 的实际检索通道。
- 通道覆盖率是诊断指标，不影响现有 Recall、MRR、Forbidden Top-1 和 CLI 门禁。
- 旧文档中“缺 vector score 即不完整”的表述被替换为本设计的通道一致性规则。
