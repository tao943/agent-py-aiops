## 1. 公开合同与确定性验证

- [x] 1.1 定义确定性 check codes、验证结果和错误类别
- [x] 1.2 以失败测试覆盖正例、竞争根因、标签、Evidence 归属、正证据绑定和 causal chain
- [x] 1.3 实现只依赖公开证据的确定性 Validator

## 2. Structured Validator 与 Workflow

- [x] 2.1 以失败测试覆盖 LangChain envelope、调用失败、格式纠错、明确拒绝和候选缺失
- [x] 2.2 实现一次格式纠错和安全错误分类，不持久化原始模型内容
- [x] 2.3 集成 v3 validation origin、定向 Replan、fail-closed 和人工恢复限制

## 3. Artifact 与兼容性

- [x] 3.1 以失败测试覆盖 v3 origin allowlist 和历史 v2 兼容
- [x] 3.2 实现 v3 Artifact 资格与 Task/Report/Checkpoint 一致性

## 4. 验证与真实验收

- [x] 4.1 运行目标 pytest、Ruff、strict Pyright 和普通离线全量测试
- [x] 4.2 运行 OpenSpec strict/all 验证并更新 DomainBench 文档
- [x] 4.3 只运行一次真实 APY-013；若失败则停止重试并用结构化审计诊断
