## 1. 合同与路由模型

- [ ] 1.1 增加 Investigator capability registry、sourceDomain 和显式只读工具信任
- [ ] 1.2 增加版本化 Strategy Router、硬门禁、分数和稳定审计原因
- [ ] 1.3 增加 EvidencePacket schema、所有权校验、去重和时空冲突

## 2. 可恢复执行图

- [ ] 2.1 抽取单次 Knowledge Investigator 并隔离 `aiops-diagnostic-v3` checkpoint
- [ ] 2.2 抽取 single/multi 共用工具执行原语和幂等 Dispatch
- [ ] 2.3 接入 Runtime/Log fan-out、Aggregator 单写和 deterministic fast path
- [ ] 2.4 增加 deadline、模型预算、部分失败、迟到结果和 Single fallback

## 3. 审计、Benchmark 与验收

- [ ] 3.1 稳定 SSE 顺序并投影 route/dispatch/packet/fallback Artifact
- [ ] 3.2 增加 Benchmark-only `auto|single|multi` 和普通 API 隔离
- [ ] 3.3 持久化可重建的定长安全 A/B 指标
- [ ] 3.4 补齐并发恢复、答案隔离、路径安全和未知工具 fail-closed 测试
- [ ] 3.5 执行固定场景 Single/Multi A/B；未达门槛时保持 Benchmark-only
