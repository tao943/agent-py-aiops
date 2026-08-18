## Why

真实 APY-013 已生成正确且有证据支持的根因，但 LLM Validator 不可用被误判为证据缺口，
导致无关 Replan、重复模型调用和根因丢失。当前审计无法区分模型调用失败、格式失败和模型明确
拒绝，也无法在公开证据已经完整闭环时安全保留结论。

## What Changes

- 将 Validator 的模型调用失败、格式失败和明确证据拒绝分开审计。
- 增加严格的公开证据确定性验证，并只在该验证完整通过时允许降级结论。
- 禁止 Validator 基础设施故障进入证据 Replanner。
- 降级结论只能进入人工审批或外部策略恢复路径。
- 将新合同标记为 `evidence-driven-v3`，同时保留历史 v2 Artifact 的可评分性。

## Capabilities

### Modified Capabilities

- `aiops-diagnosis-tasks`
- `agentpy-sre-benchmark`

## Impact

- 新增一个聚焦的 AIOps Decision Validation 领域模块和相邻测试。
- 扩展现有诊断 step/checkpoint JSONB payload，不增加数据库迁移。
- 更新 Snapshot Artifact 的 Workflow 版本兼容规则。
- 不新增依赖、外部服务、前端字段或公共 API 合同。
