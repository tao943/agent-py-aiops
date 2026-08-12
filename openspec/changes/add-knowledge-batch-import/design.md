## Context

任务创建端点返回 `data.task` 与调度元数据，任务查询端点把任务对象直接放在 `data`。临时 PowerShell 导入器曾按 `data.task.status` 读取查询结果，因此出现空状态；后端、PostgreSQL 与 Milvus 没有丢失任务状态。

现有 `seed_ecommerce_aiops_sop.py` 已通过 HTTPX 调用认证、上传和索引 API，并包含一个仅处理 `succeeded`、`failed` 和超时的本地轮询循环。项目已具备所需依赖，无需新增库或服务。

## Goals / Non-Goals

**Goals:**

- 以单一、可测试的客户端实现解析查询响应和轮询状态机。
- 在截止时间内有限重试网络瞬时错误，明确报告终止失败、协议错误和超时。
- 顺序导入显式目录内经过审核的 Markdown，并输出不含秘密与正文的审计汇总。
- 保持普通 CI 离线确定性，真实导入单独执行并核验 PostgreSQL/Milvus。

**Non-Goals:**

- 不新增后端批量 Job API或数据库轮询回退。
- 不修改持久任务状态机、PostgreSQL/Milvus schema 或 RAG 检索管线。
- 不要求 CLS MCP 或 `/ready` 成功才能索引文档。
- 不导入复制的完整博客、Benchmark oracle、隐藏证据或评分答案。
- 不自动删除先前重复导入的文档。

## Decisions

### 包装复用现有 API

使用项目已有 HTTPX、认证、文档上传和索引任务 API。共享客户端只负责解析创建响应和查询轮询，不访问 PostgreSQL。这样 API 是唯一客户端边界，也避免导入脚本与存储实现耦合。

### 明确区分响应契约

`POST .../index-tasks` 只从 `data.task` 解析创建任务；`GET .../index-tasks/{task_id}` 只从 `data` 解析查询任务。缺少非空字符串 `status`、未知状态或错误 envelope 均立即作为协议错误，不会被当成仍在运行。

### 有界状态机与重试

`pending`、`running` 继续轮询；`succeeded` 返回；`failed`、`cancelled` 终止并带安全失败原因。仅 HTTPX timeout/network 异常进入有限重试，任何重试都服从全局 deadline；有效响应后重置连续瞬时失败计数。

### 顺序批量导入

第一版按确定性文件名顺序逐个上传、建任务、轮询。顺序执行更容易归因失败并控制 embedding 配额。默认 fail-fast，可显式继续并最终以非零退出报告部分失败。dry-run 不认证、不发 HTTP 请求。

### 内容与凭据边界

导入器只处理选定根目录的非 symlink Markdown 文件，不递归穿越目录。汇总只含文件名、文档 ID、任务 ID、状态与安全错误；配置、token、密码和正文不输出。RAG 知识与 Benchmark ground truth 继续物理分离。

## Risks / Trade-offs

- 顺序导入比并发慢，但七份初始知识卡的吞吐不是瓶颈，且降低配额与重复写风险。
- overwrite 语义由现有上传 API 决定；命令不会增加第二套幂等数据库。
- 网络在 deadline 内持续失败会使当前文件失败；汇总保留准确状态，不会声称未确认任务成功。

## Verification

- HTTPX MockTransport 单测覆盖所有轮询状态、瞬时错误、超时和 malformed response。
- API 合约测试固定查询响应为 `data.status` 且无 `data.task`。
- 批量导入单测覆盖路径约束、dry-run、顺序、fail-fast 和 continue-on-error。
- 最终真实执行要求 PostgreSQL 任务/文档状态与 Milvus scoped chunks 同时有证据；CLS 不作为前置条件。

