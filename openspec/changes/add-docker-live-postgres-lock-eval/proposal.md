## Why

Snapshot 能稳定比较 Agent，但不能证明证据来自真实故障、恢复动作安全或服务确实恢复。
需要一个范围可控、可重复、可清理的 PostgreSQL Live 纵切验证完整闭环。

## What Changes

- 新增 `APY-LIVE-PG-LOCK-001`，在隔离数据库中构造真实行锁等待。
- 复用生产诊断 workflow 与 RAG，collector 只提供结构化只读证据。
- 自动恢复仅允许终止当前 run 的 synthetic blocker，并执行恢复验证与幂等清理。
- 新增 100 分 Live 评分、硬门禁、手动 CLI/pytest marker 与安全运维文档。
- 本地 PostgreSQL collector 为默认实现，腾讯云 CLS collector 延后。

## Capabilities

### New Capabilities

- `live-sre-evaluation`: 定义真实故障注入、证据采集、安全恢复、验证和评分闭环。

## Impact

- 新增 Live evaluation 后端包、场景、Docker marker、隔离数据库和运维文档。
- 普通 CI 保持离线，不启动 Docker、不调用真实模型、不依赖 CLS。
