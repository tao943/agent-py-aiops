# document-indexing-jobs Specification

## Purpose

定义经过身份验证、tenant 范围、非阻塞的文档索引任务：将上传文档分块，调用配置的嵌入模型，把范围向量写入 Milvus，在 PostgreSQL 中持久化任务状态，并支持手动重试。
## Requirements
### Requirement: Manual document index task creation
系统 SHALL 允许经过身份验证的 user 为其拥有的文档手动创建文档索引任务。

#### Scenario: User starts indexing own document
- **WHEN** 已认证的 user 请求对其知识库中的文档进行索引
- **THEN** 系统 MUST 创建一个持久化的索引任务，状态为 `pending` 或 `running`，并通过统一的成功封装返回，状态码为 HTTP 202。

#### Scenario: 用户开始对另一个 user 的文档进行索引
- **WHEN** 一个经过身份验证的 user 请求对其 tenant 范围外的文档进行索引
- **THEN** 系统 MUST 以统一的授权错误拒绝该请求，并且 MUST NOT 创建一个索引任务。

### Requirement: Non-blocking indexing execution
文档索引 SHALL 作为持久化 `document_index` 后台任务执行，请求 MUST 立即返回，服务重启后 queued 或租约过期的索引 MUST 恢复。

#### Scenario: Indexing task is accepted
- **WHEN** 用户创建或重试文档索引任务
- **THEN** API MUST 同时创建业务索引记录和 durable background job，并返回 202。

### Requirement: Document chunking
系统 SHALL 在嵌入之前将可索引文档文本拆分为确定性 chunk。

#### Scenario: Document text is split into chunks
- **WHEN** 一个索引任务针对包含可索引文本的文档运行
- **THEN** 索引器 MUST 使用一个或多个 chunk 创建具有稳定 chunk ID、按 chunk 排序的索引、内容文本以及包含 chunk 边界或排序的元数据。

#### Scenario: Empty document text fails indexing
- **WHEN** 一个索引任务对没有可索引文本的文档运行  
- **THEN** 该任务 MUST 被标记为 `failed` ，并带有安全失败原因，且文档索引状态 MUST 被标记为 `failed` 。

### Requirement: Embedding generation
系统 SHALL 调用配置的 OpenAI-compatible 嵌入模型，为每个 chunk 生成一个向量。

#### Scenario: Embeddings are generated for chunks
- **WHEN** 索引器已创建文档 chunks
- **THEN** 它 MUST 使用 chunk 内容调用嵌入服务提供商，并在写入 Milvus 之前为每个 chunk 接收一个向量。

#### Scenario: Embedding failure is recorded
- **WHEN** 嵌入提供方失败  
- **THEN** 该任务 MUST 应使用安全的失败原因标记 `failed`，并且 MUST 应可用于手动重试。

### Requirement: Milvus vector insertion
系统在确保目标集合和索引存在后，通过向量存储边界将 SHALL 写入 Milvus、元数据和向量。

#### Scenario: Vector store is initialized before writes
- **WHEN** 为文档索引任务生成嵌入成功
- **THEN** 索引器 MUST 在删除现有的 chunk 或插入新的 chunk 之前，初始化向量存储集合和索引。

#### Scenario: Chunks are inserted with vectors
- **WHEN** 嵌入生成成功
- **THEN** 索引器 MUST 将所有 chunk 记录插入到 Milvus 中，包含 chunk 内容、向量、来源、创建时间戳和元数据。

#### Scenario: Milvus insertion failure is recorded
- **WHEN** Milvus 插入失败
- **THEN** 该任务 MUST 应使用安全的失败原因标记 `failed`，并且文档索引状态 MUST 应标记 `failed`。

### Requirement: Index task state persistence
系统 SHALL 会保留 `pending`、`running`、`succeeded` 和 `failed` 的索引任务状态转换。

#### Scenario: Successful indexing updates task and document
- **WHEN** 生成、嵌入和 Milvus 插入全部成功
- **THEN** 任务必须被标记为 `succeeded`，文档索引状态必须被标记为 `indexed`，完成时间戳必须被持久化。

#### Scenario: Failed indexing updates task and document
- **WHEN** 任何索引步骤失败
- **THEN** 任务 MUST 应被标记为 `failed`，文档索引状态 MUST 应被标记为 `failed`，并且应保存一个安全的失败原因 MUST。

### Requirement: Manual retry
系统 SHALL 允许一个 user 手动重试失败的文档索引任务，通过为同一拥有文档创建新的尝试。

#### Scenario: Failed task is retried
- **WHEN** 一个经过身份验证的 user 会重试具有失败任务的文档的索引
- **THEN** 系统 MUST 会创建一个与之前失败任务相关联的新索引任务并安排其进行处理。

#### Scenario: Retry is tenant scoped
- **WHEN** 已认证的 user 尝试在其 tenant 范围外重试任务
- **THEN** 系统 MUST 以统一的授权错误拒绝该请求。

### Requirement: Strategy-aware document indexing
异步索引服务 SHALL 读取每个文档的持久化 chunking 配置，并在嵌入和 Milvus 插入之前使用共享的 chunking 服务。

#### Scenario: Index task uses document strategy
- **WHEN** 一个索引任务在具有存储的 chunk 配置的文档上运行
- **THEN** 服务 MUST 从该配置生成的 chunk 中生成向量，而不是使用全局固定的策略。

#### Scenario: Vector metadata records chunking strategy
- **WHEN** 服务将 chunk 向量插入到 Milvus 中
- **THEN** 每个 chunk 元数据记录 MUST 包括所选的 chunk 策略和参数，以及现有的 owner 和 tenant 元数据。

### Requirement: Changed reviewed documents overwrite by scoped filename

知识文档上传在 `overwrite=true` 时 SHALL 在相同 owner、knowledge base 和 filename 范围内替换活动文档，即使内容哈希已经变化。替换 MUST 先删除旧文档 scoped vectors、软删除旧元数据，再创建并返回新文档；`overwrite=false` 的现有相同内容冲突行为保持不变。

#### Scenario: Reviewed card content changes under the same filename

- **WHEN** owner 使用 `overwrite=true` 上传一个与活动文档同名但内容不同的 Markdown
- **THEN** 系统 MUST 返回新 document ID、标记旧文档已删除，并 MUST 只保留一个该文件名的活动文档

#### Scenario: Different filename has changed content

- **WHEN** owner 使用 `overwrite=true` 上传内容不同且文件名不同的 Markdown
- **THEN** 系统 MUST 创建独立文档且 MUST NOT 替换无关文件名的活动文档

#### Scenario: Legacy duplicate filenames are detected

- **WHEN** 真实导入前发现同一范围内已经有多个活动同名文档
- **THEN** 运维流程 MUST 停止并报告重复项，且 MUST NOT 静默批量删除历史文档

### Requirement: Reliable index task polling client

系统 SHALL 提供一个复用现有索引任务查询 API 的客户端轮询器。轮询器 MUST 从查询响应的 `data.status` 读取状态，MUST 对传输瞬时失败执行受总截止时间约束的有限重试，并 MUST 明确区分成功、终止失败、协议错误和超时。

#### Scenario: Index task advances to success

- **WHEN** 查询 API 依次返回 `pending`、`running` 和 `succeeded`
- **THEN** 轮询器 MUST 返回最终任务，且 MUST NOT 从 `data.task.status` 读取查询状态

#### Scenario: Query response violates the contract

- **WHEN** HTTP 成功响应缺少非空字符串 `data.status`
- **THEN** 轮询器 MUST 立即报告协议错误，且 MUST NOT 把该响应视为仍在运行

#### Scenario: Query transport fails transiently

- **WHEN** 查询在总截止时间内发生 HTTP timeout 或 network error 后恢复
- **THEN** 轮询器 MUST 在有限重试预算内继续，并 MUST 在获得 `succeeded` 后返回最终任务

#### Scenario: Index task reaches terminal failure

- **WHEN** 查询 API 返回 `failed` 或 `cancelled`
- **THEN** 轮询器 MUST 终止并报告任务 ID、终止状态和可用的安全失败原因

#### Scenario: Index polling reaches its deadline

- **WHEN** 任务在总截止时间前没有进入终止状态
- **THEN** 轮询器 MUST 报告超时和最后一个有效状态，且 MUST NOT 声明索引成功

### Requirement: Reviewed Markdown batch import

系统 SHALL 提供顺序批量导入命令，通过现有认证、上传和索引任务 API 导入显式目录中的 Markdown 文件，并 SHALL 输出不含凭据和正文的逐项及汇总结果。

#### Scenario: A reviewed batch is imported

- **WHEN** 操作者指定一个受限目录，其中包含经过审核的 Markdown 知识卡
- **THEN** 命令 MUST 按确定性文件顺序上传、创建任务、等待 `succeeded`，并 MUST 报告文件名、文档 ID、任务 ID 和最终状态

#### Scenario: Operator previews a batch

- **WHEN** 操作者使用 dry-run 检查导入目录
- **THEN** 命令 MUST 只输出符合条件的相对 Markdown 文件名和数量，且 MUST NOT 认证或发出 HTTP 请求

#### Scenario: A file fails in fail-fast mode

- **WHEN** 默认批量导入中的一个文件上传、建任务或索引失败
- **THEN** 命令 MUST 停止后续文件并以非零状态输出准确的成功/失败汇总

#### Scenario: A file fails in continue mode

- **WHEN** 操作者显式启用 continue-on-error 且一个文件失败
- **THEN** 命令 MUST 继续处理后续文件，最终 MUST 以非零状态输出每项结果和准确计数

### Requirement: Batch import safety boundaries

批量导入 SHALL 只接受选定根目录内的非 symlink Markdown 文件，SHALL NOT 导入 Benchmark ground truth、隐藏证据、评分答案或秘密，并 SHALL NOT 将认证信息或文档正文写入运行汇总。

#### Scenario: Candidate escapes the selected source directory

- **WHEN** 候选路径是 symlink、非 Markdown 文件或解析后不在所选根目录内
- **THEN** 命令 MUST 忽略或拒绝该候选，且 MUST NOT 上传其内容
