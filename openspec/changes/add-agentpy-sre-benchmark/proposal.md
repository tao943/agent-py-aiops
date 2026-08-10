## Why

当前 AIOps 工作流能够保存工具证据和报告，但缺少可重复的故障输入、隔离的标准答案、差异化根因判定以及可解释评分。因此无法证明 Agent 面对相同现象的不同根因时是否真的根据证据完成诊断，也无法量化调优效果。

## What Changes

- 新增 AgentPy DomainBench，第一阶段提供两个同为 Nginx 502、根因不同的 Snapshot 案例。
- 将公开场景、冻结工具观测与 evaluator-only 标准答案物理分离。
- 为 LangGraph 诊断加入结构化假设、证据支持/反驳关系和有证据引用的最终根因决策。
- 新增透明的确定性 100 分评分、硬门槛和逐项得分理由。
- 使用 PostgreSQL 保存评测运行、版本配置与结果，不保存 API key。
- 提供默认离线的 runner 和 CLI；真实外部 API、CLS、Milvus、Alertmanager 与 Docker 不参与默认测试。

## Capabilities

### New Capabilities

- `agentpy-sre-benchmark`: 定义 Snapshot 场景隔离、差异化根因评测、确定性评分与运行记录。

### Modified Capabilities

- `aiops-diagnosis-tasks`: 诊断执行保存结构化假设更新和有证据依据的根因决策。
- `memory-repositories`: PostgreSQL Repository 保存评测运行与结果。

## Impact

- 新增 `benchmarks/agentpy/` 场景目录和后端 evaluation 模块。
- 扩展现有 LangGraph 节点与诊断证据记录，不公开私有 Chain-of-Thought。
- 新增 Alembic revision、Repository 记录与本地 benchmark CLI。
- 第一阶段不包含自动恢复、Live 故障注入、LLM Judge、前端评测页面或完整十案例目录。
