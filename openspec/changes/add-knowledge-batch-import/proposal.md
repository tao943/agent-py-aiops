## Why

现有文档索引 API 与持久任务运行正常，但一次性导入脚本容易混淆创建响应 `data.task` 和查询响应 `data`，导致把已完成任务误判为状态为空。项目还缺少一个可审计、可重跑、不会泄露凭据的审核后 Markdown 批量导入入口。

## What Changes

- 抽取复用现有索引任务查询 API 的同步轮询客户端。
- 明确区分任务创建与任务查询的成功响应 envelope。
- 对瞬时传输错误执行受总截止时间约束的有限重试。
- 为审核后的 Markdown 知识卡提供顺序批量导入、dry-run 和失败汇总。
- 让现有 AIOps SOP seed 命令复用同一轮询客户端。
- 使用确定性测试覆盖成功、终止失败、协议错误、超时和部分批次失败。

不修改 FastAPI 路由、数据库 schema、索引 Worker、Milvus schema 或前端契约；不把 Benchmark ground truth 导入 RAG。

## Capabilities

### Modified Capabilities

- `document-indexing-jobs`: 增加可靠的客户端轮询与审核后 Markdown 批量导入操作要求。

## Impact

- `apps/backend/scripts/` 中的索引客户端、现有 SOP seeder 和新批量导入命令。
- 后端聚焦单元测试与索引 API 合约测试。
- `config/project.template.json` 的无密钥批量知识目录默认值。
- `docs/knowledge-candidates/` 中经过审核的原创故障排查知识卡。

