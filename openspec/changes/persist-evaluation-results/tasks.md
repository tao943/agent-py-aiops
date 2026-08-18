## 1. 规格与安全归档

- [x] 1.1 同步 active WIKI 并通过 OpenSpec strict 校验
- [x] 1.2 以失败测试定义统一 Envelope、字段允许列表、答案隔离和稳定 checksum
- [x] 1.3 实现只从本地 JSON 配置读取的 worktree 外原子归档

## 2. PostgreSQL 与共享 Recorder

- [x] 2.1 以 migration/Repository 测试覆盖通用 Run/Result、旧行回填和并发幂等
- [x] 2.2 新增 Alembic migration 并泛化现有 EvaluationRepository
- [x] 2.3 实现 Artifact-first Recorder、数据库待同步和中断生命周期

## 3. 三类测评自动保存

- [x] 3.1 将 Snapshot runner 的持久化所有权迁移到统一 CLI 边界
- [x] 3.2 将 Retrieval 通过、阈值失败、运行失败和中断接入 Recorder
- [x] 3.3 将 Live/CLS 通过、有效失败、基础设施失败和中断接入 Recorder

## 4. 历史导入、对账与汇总

- [x] 4.1 以失败测试覆盖显式来源、安全拒绝、重复、冲突和 reconstructed 语义
- [x] 4.2 实现 import-history、reconcile、summarize 和 audit 命令
- [x] 4.3 将全部可恢复旧结果导入共享归档并验证第二次导入幂等

## 5. 验证与文档

- [x] 5.1 更新后端运维文档和本地配置模板
- [ ] 5.2 运行目标测试、全量 pytest、Ruff、strict Pyright 和 PostgreSQL migration 验证
- [x] 5.3 运行 OpenSpec strict/all、wiki-sync 和 VitePress build，并核对真实 summary/index
