## Why

当前 Snapshot 运行基本写入 PostgreSQL，但 Retrieval 只在显式 `--output` 时写本地 JSON，Live/CLS
报告仍位于各 Git worktree 的 `var/`。成功与失败结果因此分散，删除 worktree 可能丢失历史，数据库
重建后也无法恢复本地独立测评。项目需要一套统一、答案隔离且可对账的运行历史。

## What Changes

- 为 Snapshot、Retrieval、Live 和 CLS 建立统一 Evaluation Run 生命周期。
- 将安全运行 Artifact 原子保存到 worktree 外本地共享目录，并幂等写入 PostgreSQL。
- 保存通过、评分失败、Agent 失败、基础设施失败和可捕获中断。
- 增加按 run ID 与 checksum 的历史导入、数据库对账、安全审计和汇总命令。
- 将仍可证明的旧结果导入共享归档，对缺失正式结果的 Live 审计只标记 reconstructed，不补造指标。

## Capabilities

### Added Capabilities

- `evaluation-result-history`

## Impact

- 新增一条 Alembic migration，泛化现有 evaluation run/result 表。
- 修改三个测评 CLI 和 Snapshot runner 的持久化所有权。
- 新增本地 JSON 配置字段 `evaluation.archiveDir`，不读取环境变量。
- 新增后端历史管理 CLI；不新增 HTTP API、前端、外部服务或第三方依赖。
- 运行 Artifact、真实日志、凭据和本地用户配置不进入 Git。
