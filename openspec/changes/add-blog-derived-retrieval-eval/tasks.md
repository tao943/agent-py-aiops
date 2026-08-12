## 1. 场景与知识

- [ ] 1.1 以失败测试定义 PostgreSQL 与 Redis 成对公开输入、证据和答案隔离合同
- [ ] 1.2 添加 APY-002、APY-007、APY-011 与 APY-012 Snapshot 和 provenance
- [ ] 1.3 重构 PostgreSQL 与 Redis 综合差分知识卡并验证无 Benchmark 答案

## 2. Retrieval Eval

- [ ] 2.1 定义六条无答案 Retrieval 查询与严格 loader
- [ ] 2.2 实现 Recall@1/3、MRR、禁止 Top-1 和引用完整性评分
- [ ] 2.3 添加安全的真实检索手动 runner 与离线合同测试

## 3. 幂等知识更新

- [ ] 3.1 以失败测试固定变更内容同名文件的覆盖语义
- [ ] 3.2 实现 scoped filename 查询、向量删除、软删除和新文档创建
- [ ] 3.3 仅更新两张知识卡并核验 PostgreSQL 与 Milvus

## 4. 验证与文档

- [ ] 4.1 更新 DomainBench 与本地运行文档
- [ ] 4.2 运行聚焦 Pytest、Ruff、Pyright 和 OpenSpec
- [ ] 4.3 运行普通后端回归并记录真实 Retrieval Eval 结果
