# 微服务端到端超时差分排查

## 适用现象
入口 p95/p99、504 或客户端 deadline exceeded 上升。超时点可能位于本服务、下游、网络，也可能由重试放大。

## 候选原因
- 本服务 CPU、线程/协程或内部锁饱和。
- 下游服务或数据库占据主要调用时间。
- DNS、连接建立或传输路径变慢。
- 不一致的超时预算与重复重试形成级联。

## 建议证据
保存端到端 trace 各 span、入口与服务处理耗时、连接/DNS 时间、下游指标、资源池等待、deadline/timeout 配置、重试次数及调用量放大倍数。

## 如何区分
本服务 self time 上升且下游正常偏向本地资源；某下游 span 占主导偏向依赖；connect/DNS time 上升偏向网络；单次调用时延变化不大但调用次数激增偏向重试风暴。入口 504 不能定位根因。

## 安全恢复边界
trace 与配置读取可自动执行。调超时、关闭重试、熔断、扩容或回滚需审批并按调用链评估；不得简单把所有 timeout 同时调大。

## 恢复后验证
确认端到端分位延迟、错误率、重试放大和相关资源池恢复，超时预算从入口到下游保持递减，并验证降级路径。

## 来源
- OpenTelemetry Traces：https://opentelemetry.io/docs/concepts/signals/traces/
- AWS Builders' Library Timeouts and Retries：https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/
许可按各官方页面，仅作机制参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
