## Why

当前测试知识库只有 7 张文档和 9 个 Chunk，Retrieval Eval 也只有 6 条直接指向 PostgreSQL/Redis 的查询，目标卡很容易被召回，无法形成足够的同域困难负例。现有评分还会让同一文档的多个 Chunk 占据文档级 Top-K，并会跳过缺失 citation 的 hit。

## What Changes

- 将审核知识卡从 7 张扩展为恰好 30 张，覆盖七个相互重叠的故障族。
- 所有卡使用统一差分结构、来源与许可证说明，并标记 Docker 实验仍为 pending。
- 将 Retrieval labels 从 6 条扩展为 60 条：54 条有答案查询和 6 条无答案校准探针。
- 主排名指标按文档首次出现去重；每个返回 hit 都进入 citation 完整率分母。
- 无答案探针只记录分数分布，不使用未经校准的阈值作为通过门槛。
- 真实批量导入和 Embedding/Rerank 继续手动运行，普通 CI 保持离线。

## Capabilities

### New Capabilities

- `knowledge-card-catalog`: 定义 30 张审核知识卡的目录、内容安全、来源和实验验证状态。

### Modified Capabilities

- `knowledge-retrieval-eval`: 扩展查询分类、无答案探针、文档级指标和逐 hit citation 审计。

## Impact

- `docs/knowledge-candidates/` 新增 23 张卡并统一 7 张现有卡的验证状态。
- `benchmarks/agentpy/retrieval/queries.yaml` 扩展到 60 条。
- 后端 retrieval DTO、loader、scorer、runner、安全 catalog audit 和离线测试。
- 当前测试 owner 的 PostgreSQL 文档与 Milvus Chunk 会在离线验证完成后更新。
- 不启动 Docker 故障实验、不运行 Chat/Agent 或 Live Eval。
