## Why

AgentPy DomainBench 当前只有两个 Nginx 502 场景，无法覆盖 PostgreSQL 与 Redis 的差分诊断；审核知识卡也缺少独立的检索质量评测。现有批量导入使用 `overwrite=true`，但内容变化后不会按同名文件替换旧活动文档，会产生重复知识和 Chunk。

## What Changes

- 新增 PostgreSQL 与 Redis 各两个同症状、不同根因的确定性 Snapshot 场景。
- 将公开知识、冻结观测和 evaluator-only 答案继续物理隔离。
- 重构两张综合差分知识卡，不新增单根因答案卡。
- 新增六条无答案 Retrieval Eval 查询以及 Recall、MRR、禁止 Top-1、引用完整率指标。
- 增加同 owner、知识库、文件名范围内的变更内容覆盖语义，避免批量知识更新产生第二个活动文档。
- 真实 Embedding/Rerank 与知识导入保持手动运行，普通 CI 只执行确定性测试。

## Capabilities

### New Capabilities

- `knowledge-retrieval-eval`: 定义无答案查询标注、确定性检索指标、引用完整性与租户隔离要求。

### Modified Capabilities

- `agentpy-sre-benchmark`: 增加 PostgreSQL 与 Redis 差分 Snapshot 场景合同。
- `document-indexing-jobs`: 增加变更内容同名知识文档的幂等覆盖要求。

## Impact

- `benchmarks/agentpy/scenarios/` 与新的 retrieval 查询目录。
- 后端 evaluation 模块、手动检索 runner 和测试。
- PostgreSQL 文档查询与上传 API 的覆盖选择逻辑，不新增数据库表。
- 两张审核后知识卡及其 PostgreSQL/Milvus 索引。
- 不实现 Live、恢复动作、Judge 或 Agent RAG 前后对照。
