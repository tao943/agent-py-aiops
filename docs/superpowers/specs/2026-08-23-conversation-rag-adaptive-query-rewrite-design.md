# Conversation RAG 自适应问题重写设计

日期：2026-08-23  
状态：已确认设计，待实施

## 目标

在 Conversation Agent 的 `knowledge_question` 检索路径中增加自适应问题重写路由。
明确、可独立检索的问题继续直接进入现有混合检索；只有依赖上下文的追问、省略、指代或
低信息量查询才调用一次结构化 Rewriter。目标是在不改变权限、检索过滤器和 AIOps 诊断
链路的前提下，提高多轮对话中的 RAG 召回质量。

## 范围

本设计只修改 Conversation Agent 暴露的 `knowledge_retrieval` 调用。以下内容不在范围内：

- AIOps Agent 的 RAG 检索；
- PostgreSQL、Docker Live 或其他运维证据工具；
- 腾讯云 CLS；
- 恢复授权、Validator 和现有 AIOps Benchmark 评分；
- 多查询并行召回、HyDE 或第二轮完整检索重试；
- 新增模型供应商、外部服务或第三方依赖。

## 现状

Conversation Agent 先通过 `ChatIntentRouter` 得到 `knowledge_question`，再由 LangChain Agent
调用 `knowledge_retrieval`。检索工具当前执行：

```text
Query
  → Embedding vector recall + BM25 recall（并行）
  → Reciprocal Rank Fusion
  → Rerank
  → citations
```

`CachedKnowledgeRetrievalTool` 使用 owner、知识库版本、最终查询、topK 和 filters 生成不可逆
Redis 缓存键。检索工具自身负责 owner、tenant、knowledge base 和 document filter 校验。

当前 LangChain Agent 可能自行把问题改写成工具参数，但没有独立、可测试、可审计的重写
边界，也没有保证含指代的追问一定被转换为独立查询。

## 复用评估

在 GitHub 检查了三个活跃项目：

| 候选 | 许可证 | 可复用点 | 不直接采用的原因 |
| --- | --- | --- | --- |
| `langchain-ai/langchain` `MultiQueryRetriever` | MIT | 生成替代查询、并行检索、去重 | 面向多查询，会成倍增加现有 Embedding/BM25/Rerank 成本；项目已自有检索 DTO 与权限边界 |
| `run-llama/llama_index` `QueryTransform` | MIT | 单查询 transform、Identity fallback、HyDE | 引入整个 LlamaIndex 过重，且与现有 LangChain/检索 DTO 重叠 |
| `deepset-ai/haystack` `QueryExpander` | Apache-2.0 | 严格 JSON、保留原查询、异常回退 | 引入 Haystack 过重，默认仍是多查询扩展 |

结论为 **reference only + project-owned thin wrapper**：参考 Haystack 的结构化输出、数量限制
和原查询回退，复用项目已有 `ChatModel`、LangChain、上下文信封、检索工具和 Redis 缓存。
不新增依赖。

## 架构

```text
ChatIntentRouter
  → knowledge_question
  → LangChain Agent creates knowledge_retrieval call
  → request-scoped AdaptiveKnowledgeQueryTransformer
       → AdaptiveQueryRewriteRouter
            ├─ direct
            ├─ rewrite
            └─ direct_without_context
       → StructuredQueryRewriter（rewrite 分支最多一次模型调用）
       → validate or fall back to original query
  → existing LangChain retrieval tool factory
  → CachedKnowledgeRetrievalTool
  → existing hybrid retrieval and citations
```

包装器只在 `LangChainChatAgentRunner` 为 Conversation 请求创建工具时注入。共享的 canonical
`KnowledgeRetrievalTool`、缓存装饰器和 AIOps 构造路径保持不变。

## 组件合同

### AdaptiveQueryRewriteRouter

输入：

- Agent 为 `knowledge_retrieval` 生成的查询；
- 当前请求已经过 `ContextEnvelopeService` 限界的最近消息；
- 当前 Chat route。

输出为不包含隐式推理的公开决策：

```python
@dataclass(frozen=True, slots=True)
class QueryRewriteDecision:
    action: Literal["direct", "rewrite", "direct_without_context"]
    reason: Literal[
        "standalone_query",
        "context_reference",
        "follow_up_expression",
        "low_information",
        "missing_context",
    ]
```

只有 `knowledge_question` 允许 `rewrite`。路由使用确定性、可测试的规则，不调用模型：

- `context_reference`：包含“这个、它、上述、上面、该问题、这种情况”等指代；
- `follow_up_expression`：包含“那怎么办、还有吗、为什么会这样、具体呢”等追问结构；
- `low_information`：规范化后不超过 12 个字符，移除通用疑问/追问词后没有受保护的技术
  token、错误码、资源 ID 或具体故障现象；
- `missing_context`：满足重写特征但没有至少一条更早的用户或助手消息；
- 其他情况为 `standalone_query`。

`missing_context` 不调用 Rewriter，保持原查询，让现有 Agent 根据空召回或信息不足请求澄清，
禁止凭空补全主题。

### StructuredQueryRewriter

Rewriter 只接收：

- 当前查询，最长 512 字符；
- 最近两个完整 user/assistant 轮次，不含 system prompt、工具原始输出或私有推理；
- 一份固定输出合同。

模型必须返回且只返回：

```json
{
  "rewrittenQuery": "PostgreSQL SQLSTATE 40P01 死锁等待环的确认方法",
  "usedContext": true
}
```

校验规则：

- 顶层字段必须严格等于 `rewrittenQuery`、`usedContext`；
- `rewrittenQuery` 去除首尾空白后长度为 1～512；
- `usedContext` 必须为布尔值且在 `rewrite` 分支为 `true`；
- 输出不得包含答案、工具名、权限字段或额外 JSON；
- 必须保留原问题中的明确组件名、错误码、资源 ID 和否定语义；
- guard 提取大小写不敏感的 ASCII 技术 token/错误码/资源 ID、项目已知组件词，以及
  “不、不要、不是、无、未、not、without”等否定标记；重写结果必须包含全部受保护项；
- 语义保留只使用上述确定性 guard，不使用 Ground Truth 或第二个模型裁决。

模型超时、供应商错误、非法 JSON、未知字段、空/超长结果或 guard 失败时，包装器以原查询
继续检索。失败不能改变 owner、knowledge base、document IDs、metadata filters 或 topK。

### AdaptiveKnowledgeQueryTransformer

Transformer 返回 `QueryTransformOutcome`，其中包含只替换 query 的
`KnowledgeRetrievalToolInput` 和一份安全审计。现有
`create_langchain_knowledge_retrieval_tool` 增加可选的 request-scoped transformer 参数：未传时
保持当前行为；传入时先 transform、再调用原 `KnowledgeRetrievalToolRunner`，最后在 LangChain
工具 payload 中附加审计。canonical retrieval DTO、缓存 DTO 和 AIOps 调用接口均不改变。

Transformer 原样传递 topK、filters、owner 和 accessible knowledge bases，只允许修改 query。
Conversation 的 LangChain 工具额外公开：

```json
{
  "queryRewrite": {
    "action": "rewrite",
    "reason": "context_reference",
    "applied": true,
    "modelCallCount": 1,
    "durationMs": 120,
    "safeErrorCode": null
  }
}
```

不记录 Rewriter prompt、原始模型响应、reasoning 或上下文全文。

## 模型预算和时限

`ChatExecutionBudget` 增加 `max_query_rewrite_calls`，默认值为 `0`。只有
`knowledge_question` 使用：

```text
max_model_calls = 3
max_query_rewrite_calls = 1
Agent middleware model-call limit = 2
```

总预算中的一次调用被 Rewriter 预留，Agent 自身仍最多两次，因此不会因为包装器绕过
`ModelCallLimitMiddleware` 而产生第四次调用。direct 分支不消费预留调用。Rewriter 使用独立、
有界 timeout；timeout 不延长整个 Chat deadline，且超时后立刻使用原查询。

真实 A/B 已证明主 Chat model 的 Rewriter 时延不稳定，因此增加独立的
`queryRewriteModel` 配置。主 Agent、Validator、Embedding 和 Rerank 配置保持不变；当前
Rewriter 使用 `qwen3.7-flash`，并复用相同供应商、API Key、Base URL、温度、重试策略和
模型能力表。配置缺失时为兼容旧项目回退到 `chatModel`。

## 缓存

调用顺序固定为 Rewriter → `CachedKnowledgeRetrievalTool`。缓存继续使用最终有效查询：

- 重写成功时使用重写查询；
- direct 或降级时使用原查询；
- owner、知识库版本、accessible KB IDs、topK 和 filters 继续进入现有不可逆缓存键；
- Rewriter 输出本身第一版不单独缓存，避免保存对话派生文本和引入额外失效规则。

## 安全和隐私

- Rewriter 不能读写 CLS、PostgreSQL 运维证据或恢复工具；
- Rewriter 不获得工具列表，也不能改变检索 filters；
- 最近上下文继续服从 Context Envelope 的租户边界和完整轮次裁剪；
- Prompt Injection 仅作为不可信数据进入固定 prompt；
- Artifact、Evaluation Archive 和安全审计禁止保存 Rewriter prompt、reasoning 和原始响应；
- 安全元数据只记录决策枚举、是否应用、调用数、耗时和安全错误码。

## 错误处理

| 失败 | 行为 | 安全错误码 |
| --- | --- | --- |
| 无可用历史上下文 | 不调用模型，使用原查询 | `missing_context` |
| Rewriter timeout | 使用原查询 | `rewrite_timeout` |
| 模型调用失败 | 使用原查询 | `rewrite_model_failed` |
| JSON/Schema 无效 | 使用原查询 | `rewrite_schema_invalid` |
| 必需 token/identifier/否定语义丢失 | 使用原查询 | `rewrite_semantic_guard_failed` |
| canonical retrieval 失败 | 保持现有 Retrieval 错误行为 | 现有错误码 |

Rewriter timeout 固定为 25.0 秒，并受整个 Chat deadline 约束。Rewriter 降级不伪装为重写
成功，也不阻止 canonical retrieval。检索自身失败仍按现有
`KnowledgeRetrievalError` 处理，不能被 Rewriter 吞掉。

## 测试策略

实施采用 TDD，至少覆盖：

1. 明确 PostgreSQL、Redis、Nginx 等独立问题走 direct，Rewriter 调用 0 次；
2. “那这个要怎么处理”结合最近对话生成独立查询，调用 1 次；
3. 首轮“这个怎么处理”走 `direct_without_context`，不猜测主题；
4. timeout、模型错误、非法 JSON、未知字段、空值、超长值回退原查询；
5. 组件名、错误码、资源 ID 或否定词丢失时 guard 拒绝重写；
6. Prompt Injection 不能修改 owner、accessible KB、filters 或 topK；
7. 重写后的最终查询进入现有 owner/version-scoped Redis 缓存；
8. AIOps 的 `KnowledgeRetrievalToolRunner` 不经过 Conversation wrapper；
9. Agent 最多 2 次模型调用、Rewriter 最多 1 次、总数不超过 3；
10. LangChain 工具输出包含安全 `queryRewrite` 元数据，且不包含 prompt、reasoning 或原始响应。

聚焦验证包括 query rewrite 单元测试、Conversation tool integration、预算测试、Retrieval cache
回归、Conversation Eval、Ruff 和 Pyright。不运行全量 pytest，不在实现阶段自动调用真实模型、
Milvus、CLS 或 Docker。

## Eval 边界

离线测试只证明路由、安全回退、预算和工具集成。后续真实批准后，使用现有 Retrieval
Benchmark 增加一组多轮追问派生查询，对比 rewrite off/on 的 Document Recall@1、Recall@3、
MRR、forbidden Top-1、模型调用数和平均/P95 时延。现有 64 条独立检索查询继续作为 direct
回归，不应因启用自适应路由而下降。

10 条多轮小样本的门禁使用 Recall@3 ≥ 0.90，避免 0.95 在离散样本上等价于强制 10/10；
Recall@1 ≥ 0.80、MRR ≥ 0.85、citation completeness = 1.00 保持不变。Forbidden Top-1
仍要求 ≤ 0.05，不能通过修改标签或放宽门禁消除失败；必须保留失败样本并从重写、初召回、
融合排序、Rerank 或知识卡重叠中定位原因。

## 验收标准

- 只对 Conversation `knowledge_question` 生效；
- direct 查询不产生额外模型调用；
- rewrite 查询最多产生一次额外调用并保持总预算不超过 3；
- 所有 Rewriter 失败均安全回退原查询；
- owner 与过滤器在任何路径保持不变；
- AIOps RAG、CLS、恢复和评分行为无变化；
- 聚焦测试、Ruff、Pyright 通过；
- 不新增依赖，不保存禁止内容，不自动消耗真实额度。
