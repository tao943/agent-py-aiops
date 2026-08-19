## 1. 行为合同与领域模型

- [ ] 1.1 增加四态 Hypothesis、受信 evidence-rule template 和 typed Fact Adapter
- [ ] 1.2 以确定性 reducer、sufficiency 和一次批量 Adjudicator 替换逐证据模型判断
- [ ] 1.3 增加条件 Validator、调用预算、deadline 和安全降级

## 2. 可恢复执行

- [ ] 2.1 增加 PostgreSQL execution/checkpoint/write schema 和冲突安全仓储
- [ ] 2.2 接入 LangGraph async checkpointer 与节点/模型/工具幂等执行协调器
- [ ] 2.3 稳定化业务审计 ID，补齐 Worker、网络和副作用未知状态恢复

## 3. Artifact 与验收

- [ ] 3.1 发布 evidence-driven-v4 Artifact 并保持 v2/v3 历史评分不变
- [ ] 3.2 补齐答案隔离、路径安全、并发幂等、预算和 deadline 恢复测试
- [ ] 3.3 顺序完成 10 个 Snapshot、4 个 Live 和安全差分报告
