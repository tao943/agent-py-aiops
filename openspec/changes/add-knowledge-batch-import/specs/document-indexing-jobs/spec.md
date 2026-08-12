## ADDED Requirements

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
