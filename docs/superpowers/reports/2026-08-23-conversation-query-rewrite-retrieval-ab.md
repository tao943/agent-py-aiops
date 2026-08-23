# Conversation Query Rewrite Retrieval A/B 验收记录

日期：2026-08-23
测评范围：`query-rewrite-retrieval-component`
真实服务：Qwen Chat、Embedding、Milvus、Qwen Rerank
不在范围：CLS、Docker、AIOps 根因评分、端到端 Conversation 回答质量

## 数据与门禁

- 10 条多轮追问，覆盖 PostgreSQL、Redis、Nginx、Kubernetes 和消息队列。
- owner/知识库在运行前显式指定，案例标签必须在选定 Milvus scope 中唯一存在。
- baseline 使用原追问检索；rewrite 使用同一上下文经自适应路由和 Rewriter 后检索。
- 通过门槛为：Recall@1 ≥ 0.80、Recall@3 ≥ 0.90、MRR ≥ 0.85、
  forbidden Top-1 ≤ 0.05、citation completeness = 1.00，且 rewrite 不得相对 baseline 退化。
- 10 条样本使用 Recall@3 ≥ 0.90，允许一个随机漏召回；Forbidden Top-1 安全门槛不放宽。

## 第一次真实运行

Run ID：`conversation-query-rewrite-ab-20260823-1`

| 指标 | Baseline | Rewrite |
| --- | ---: | ---: |
| Recall@1 | 0.30 | 0.50 |
| Recall@3 | 0.30 | 0.50 |
| MRR | 0.30 | 0.50 |
| forbidden Top-1 | 0.20 | 0.10 |
| citation completeness | 1.00 | 1.00 |
| rewrite applied / calls | 0 / 0 | 2 / 5 |

该轮为 `VALID_FAIL`。真实结果暴露：自然追问规则覆盖不足；直接 JSON Prompt 在 5 次模型
调用中出现 2 次 schema invalid 和 1 次 15 秒 timeout；原时延实现还漏算了 Rewriter 耗时。
结果已作为不可变 Artifact 保留，没有覆盖或改写。

## 针对真实失败的修复

- 将“那要先收集什么、那先检查什么、那为什么还没恢复”等以“那/那么”开头的自然追问
  纳入确定性 follow-up 路由。
- Rewriter 复用项目现有 `json_mode + Pydantic + include_raw` 结构化输出合同；Artifact 仍不
  保存 raw response。
- A/B duration 改为从 transform 前计时，包含 Rewriter、Embedding、BM25、RRF 和 Rerank。
- 新增回归测试后再运行 Ruff、Pyright 和聚焦 Pytest。

## 第二次真实运行

Run ID：`conversation-query-rewrite-ab-20260823-2`

| 指标 | Baseline | Rewrite |
| --- | ---: | ---: |
| Recall@1 | 0.30 | 0.90 |
| Recall@3 | 0.30 | 0.90 |
| MRR | 0.30 | 0.90 |
| forbidden Top-1 | 0.10 | 0.10 |
| citation completeness | 1.00 | 1.00 |
| rewrite applied / calls | 0 / 0 | 7 / 10 |
| 平均端到端时延 | 12085.6 ms | 13331.7 ms |
| nearest-rank P95 | 100719 ms | 18035 ms |

第二轮仍为 `VALID_FAIL`：Recall@3 为 9/10，没有达到等价于 10/10 的门槛；
forbidden Top-1 也高于 0.05。3 次未应用分别是 1 次 semantic guard 拒绝和 2 次 15 秒
timeout。`QR-009` 因 timeout 使用原追问并漏召回，构成 Recall 的唯一未命中；另两条降级
仍由原查询命中目标。Baseline 的 P95 被一次 100.719 秒 Rerank/供应商异常值主导，不能据此
宣称 rewrite 更快。

另执行一次不计分的真实 LangChain 工具包装器 smoke：生产工具返回 3 个 hits 与 3 个
citations，并公开 `action=rewrite`、`modelCallCount=1`。该次生成因 topic anchor guard 返回
`rewrite_semantic_guard_failed`，随后按合同使用原查询继续检索，证明真实注入、安全元数据与
降级路径均可达；该 smoke 不计入 Recall，也不证明端到端 Conversation 回答质量。

## 第三次真实运行：独立 Flash Rewriter

Run ID：`conversation-query-rewrite-ab-20260823-5`

固定主 Agent `qwen3.7-plus`、Embedding `qwen3.7-text-embedding`、Rerank
`qwen3-vl-rerank`、10 条案例和知识库，仅将 Rewriter 改为 `qwen3.7-flash`，独立超时从
15 秒调整为 25 秒。两臂使用相同 Corpus 指纹
`ff5d47cc4e38bd0a86871b9e45c3a0461412a43d4a3a0a96959e4f4977e1050c`，范围内包含 42 个
文档、224 个 Chunk。

| 指标 | Baseline | Rewrite |
| --- | ---: | ---: |
| Recall@1 | 0.30 | 0.90 |
| Recall@3 | 0.30 | 1.00 |
| MRR | 0.30 | 0.95 |
| forbidden Top-1 | 0.20 | 0.00 |
| citation completeness | 1.00 | 1.00 |
| rewrite applied / calls | 0 / 0 | 9 / 10 |
| 平均端到端时延 | 758.0 ms | 11508.3 ms |
| nearest-rank P95 | 868 ms | 15777 ms |

Rewrite 为 `VALID_PASS`，Archive 和 PostgreSQL 均保存终态结果。10 次 Rewriter 调用没有
timeout；`QR-008` 因 semantic guard 拒绝改写后安全回退原查询，仍在 Top-1 命中正确文档。
第二轮唯一漏召回且触发 forbidden Top-1 的 `QR-009`，从回退后的
`queue-consumer-stalled.md` 修复为 `queue-backlog.md` Top-1，因此本轮不需要修改知识卡、
Rerank、标签或 Forbidden 门槛。

调试期间 `-3` 是无效 API Key 导致的 `INFRA_INVALID`，`-4` 暴露 Corpus 指纹错误写入稳定
metadata 的生命周期缺陷；后者两条悬挂记录已终止为 `interrupted`。最终实现把安全 Corpus
指纹和计数放入终态 result payload，不改变 running identity。

## 结论

独立 `qwen3.7-flash` Rewriter 已消除本轮 timeout，并将 Recall@3 提升至 1.00、Forbidden
Top-1 降至 0.00，达到组件门禁；scope、citation、语义 guard 和安全回退保持有效。Rewrite
平均增加约 10.75 秒，说明质量已达标但时延仍是下一轮优化重点。该结论仅属于
`query-rewrite-retrieval-component`，不能直接外推为端到端 Conversation 回答质量提升。
