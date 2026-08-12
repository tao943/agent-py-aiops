## 1. 规格与轮询客户端

- [x] 1.1 定义查询 envelope、轮询状态、错误与截止时间要求
- [x] 1.2 使用 TDD 实现共享索引任务轮询客户端
- [x] 1.3 固定任务查询 API 的 `data.status` 合约

## 2. 现有脚本迁移

- [x] 2.1 让 AIOps SOP seeder 复用共享轮询客户端
- [x] 2.2 覆盖创建响应解析和查询 endpoint 的回归测试

## 3. 批量知识导入

- [x] 3.1 使用 TDD 实现受限 Markdown 发现和 dry-run
- [x] 3.2 实现顺序上传、索引、fail-fast 与 continue-on-error 汇总
- [x] 3.3 审核并纳入首批原创故障排查知识卡

## 4. 验证与运行

- [x] 4.1 运行聚焦 Pytest、Ruff 和 strict Pyright
- [x] 4.2 执行真实批量导入并确认七项 succeeded
- [x] 4.3 使用 PostgreSQL 与 Milvus 只读证据核验每个文档
- [x] 4.4 更新运行文档并验证全部 OpenSpec
