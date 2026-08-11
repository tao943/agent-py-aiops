## 1. 场景与隔离

- [x] 1.1 以失败测试定义公开场景、标准答案和 bundle 校验契约
- [x] 1.2 添加 APY-003 与 APY-006 成对 Snapshot 场景及真实来源记录
- [x] 1.3 实现严格参数匹配、无答案访问能力的 Snapshot MCP 客户端

## 2. 诊断决策链

- [x] 2.1 以失败测试定义假设状态、观测判断和根因决策校验
- [x] 2.2 将 LangGraph 扩展为 Planner、Executor、Evidence Evaluator、Replanner、Decision、Report
- [x] 2.3 验证每个决策证据 ID 都能解析到已持久化证据

## 3. 评分与持久化

- [x] 3.1 以失败测试覆盖正确诊断、相同症状错误根因、伪造证据和答案泄漏
- [x] 3.2 实现 100 分确定性维度、硬门槛和逐项 ScoreReason
- [x] 3.3 新增评测运行/结果 Alembic revision、Repository 与 PostgreSQL 集成测试

## 4. Runner 与验证

- [x] 4.1 实现 evaluator 隔离的 Snapshot runner 与 application CLI adapter
- [x] 4.2 记录精确离线命令、评分解释与阶段边界
- [x] 4.3 运行目标 pytest、Ruff、Pyright、后端 CI lane 与 OpenSpec validate

## 5. 生产失败路径加固

- [x] 5.1 新增 agent_failed/infra_failed 终态、allowlist category 与 Alembic revision
- [ ] 5.2 实现并发安全 create_run、重复结果幂等与原子 finalize_run
- [ ] 5.3 Runner 对 adapter、artifact、evaluator 和 persistence 失败分类并终止 run
- [ ] 5.4 补齐 CLI 安全错误合同、路径穿越、嵌套答案键与 ReadGroundTruth 测试
- [ ] 5.5 运行全量 pytest、Ruff、Pyright 与 OpenSpec strict validation
