# HTTP 限流与重试风暴差分排查

## 适用现象
429、503、请求量和尾延迟同时上升。合法限流可能保护系统，也可能因客户端同步重试演化为放大故障。

## 候选原因
- 网关按租户/IP/路由达到既定限额。
- 下游容量不足主动返回 429 或 503。
- 客户端忽略 `Retry-After`，无退避或多层重复重试。
- 误配置限额导致正常基线流量被拒绝。

## 建议证据
收集各层状态码、限流 key 与计数器、`Retry-After`、原始请求率、尝试次数、唯一请求 ID、并发和下游容量；核对限额与最近配置变化。

## 如何区分
原始流量真实越界且尝试倍数稳定是预期限流；429 后总尝试量继续陡升偏向重试风暴；下游先饱和再出现限流是容量保护；低于基线即稳定被拒绝偏向配置错误。

## 安全恢复边界
指标和 header 审计可自动执行。改限额、禁重试、降级或扩容需审批；不得取消所有边界让流量直接压向依赖。

## 恢复后验证
确认尝试/原始请求比、429/503、并发和尾延迟恢复，客户端遵守带抖动退避，公平性和关键租户可用性未受损。

## 来源
- RFC 9110 HTTP Semantics：https://www.rfc-editor.org/rfc/rfc9110
- AWS Timeouts and Backoff：https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/
RFC 可公开引用，AWS 页面仅作机制参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
