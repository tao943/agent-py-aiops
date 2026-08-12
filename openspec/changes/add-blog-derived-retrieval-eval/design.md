## Context

现有 Snapshot MCP、答案隔离、确定性评分、tenant 过滤的混合检索和批量导入均可复用。新场景的数据由 AgentPy 合成，公开资料只提供故障机制参考。检索评测规模仅六条查询，不需要引入 Ragas、BEIR 或新的 Judge 依赖。

## Goals / Non-Goals

**Goals:**

- 用四个新 Snapshot 验证 PostgreSQL/Redis 同症状差分诊断。
- 用综合知识卡提供调查框架而不是场景答案。
- 分离检索质量与最终诊断正确性。
- 在真实知识更新前消除同名活动文档重复。

**Non-Goals:**

- 不运行 Agent 有/无 RAG 对照。
- 不创建 Docker Live 故障或恢复工具。
- 不把 Benchmark 文件或检索标签导入 RAG。
- 不新增外部评测框架、数据库表或模型服务。

## Decisions

### 手工构造差分 Snapshot

第一批四个场景逐项审核工具响应、强干扰和弱干扰。每个场景需要两个独立证据里程碑，并必须排除同族最强替代原因。

### 项目内确定性检索评分

复用 `KnowledgeRetrievalToolResult` 的排名、tenant 范围和双分数引用，计算 Recall@1、Recall@3、MRR、禁止文档 Top-1 比例和引用完整率。普通 CI 使用受控结果，不调用模型或 Milvus。

### 按同名文件安全覆盖

`overwrite=true` 在同 owner、知识库和文件名范围内查找活动文档；内容变化时删除旧 scoped vectors、软删除旧记录，再创建新文档。`overwrite=false` 继续只对相同内容哈希返回冲突。历史上已存在的多条同名活动记录不得被静默批量删除。

### 知识与答案隔离

只有 `docs/knowledge-candidates/` 的综合排查卡进入 RAG。Scenario、Snapshot、Ground Truth、Provenance、Retrieval 标注和评分规则都不进入 Milvus，也不参与知识卡关键词评分。

## Risks / Trade-offs

- 六条查询不足以代表总体检索质量，因此第一版只声明合同与基础可用性。
- 合成 Snapshot 缺少真实时序扰动，后续由独立 Live 计划补充。
- 同名覆盖会创建新 document ID；调用方必须以返回的新 ID 建索引并核验旧 Chunk 被删除。
- 真实 rerank 有模型波动，手动报告必须记录模型与分数，不进入普通 CI 门禁。
