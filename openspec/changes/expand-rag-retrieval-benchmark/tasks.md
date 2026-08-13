## 1. 知识卡目录

- [x] 1.1 以失败测试固定恰好 30 张知识卡、统一章节和污染禁令
- [x] 1.2 新增 23 张差分卡并统一 7 张现有卡的验证状态
- [x] 1.3 生成安全 Chunk audit，确认每张卡目标 6 至 10 个、最多 12 个 Chunk，且治理章节不形成独立向量块

## 2. Retrieval 数据与指标

- [x] 2.1 扩展 query DTO、严格 loader 和 54/6 查询合同
- [x] 2.2 实现文档级去重、无答案分母隔离和逐 hit citation 审计
- [x] 2.3 编写恰好 60 条查询并覆盖全部 30 张卡

## 3. 真实导入与检索

- [x] 3.1 dry-run 并审计 30 张卡的同名 active 状态
- [x] 3.2 导入 30 张卡并核验 PostgreSQL/Milvus
- [x] 3.3 运行一次真实 60 查询 Retrieval Eval 并保留安全报告（citation 门禁失败已留档）

## 4. 文档与回归

- [x] 4.1 更新运行文档并明确 Docker validation 仍 pending
- [x] 4.2 运行聚焦 Pytest、Ruff、Pyright 和 OpenSpec
- [ ] 4.3 运行有界普通后端回归或要求 GitHub Actions 门禁
