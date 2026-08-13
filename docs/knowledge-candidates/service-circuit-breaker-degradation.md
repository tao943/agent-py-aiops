# 服务熔断与降级恢复差分排查

## 适用现象
请求快速失败、返回降级数据或日志显示 circuit open。熔断器可能正确保护系统，也可能因探测或配置问题无法恢复。

## 候选原因
- 依赖持续失败，熔断器按阈值打开。
- 依赖已恢复，但 half-open 探测失败或流量不足以闭合。
- 统计窗口、失败分类或阈值配置不适合流量形态。
- 多实例状态不同导致部分请求仍访问故障依赖。

## 建议证据
采集 closed/open/half-open 状态转换、触发错误类别、窗口样本、探测结果、依赖直接健康、降级命中率和实例级配置；关联重试与超时。

## 如何区分
依赖直连仍失败且熔断快速拒绝是正常保护；依赖健康但 half-open 一直失败需检查探测；低样本下频繁开合偏向窗口配置；仅部分实例异常偏向状态或配置漂移。

## 安全恢复边界
状态和依赖只读探测可自动执行。强制闭合、改阈值、禁用降级或重启实例需审批；不得在依赖未恢复时绕过熔断器。

## 恢复后验证
确认状态按 half-open 到 closed 转换，真实请求和探测成功，降级比例下降，依赖保持容量余量且没有开合振荡。

## 来源
- Resilience4j CircuitBreaker：https://resilience4j.readme.io/docs/circuitbreaker
- Microsoft Circuit Breaker Pattern：https://learn.microsoft.com/azure/architecture/patterns/circuit-breaker
许可按各官方页面，仅作机制参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
