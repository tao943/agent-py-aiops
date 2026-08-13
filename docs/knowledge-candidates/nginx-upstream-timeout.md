# Nginx upstream 超时与 504 差分排查

## 适用现象
入口返回 504，Nginx 日志显示等待 upstream 响应超时。上游处理慢、连接路径慢和代理超时预算不合理均可产生该现象。

## 候选原因
- 上游 CPU、线程池、数据库或依赖阻塞，未在预算内响应。
- 连接建立或网络传输耗时异常。
- `proxy_connect_timeout`、`proxy_read_timeout` 与端到端 deadline 不协调。
- 重试把一次慢请求扩增为多次上游调用。

## 建议证据
关联 `$upstream_connect_time`、`$upstream_header_time`、`$upstream_response_time`、上游 trace 与资源指标；核对各层 timeout、重试次数、实际 upstream 地址和同时间窗下游延迟。

## 如何区分
connect time 高偏向网络或监听积压；header/response time 高且上游 span 同步变慢偏向业务处理；上游在稍晚时正常完成但代理提前断开偏向预算配置；调用次数随错误上升说明重试放大。连接拒绝应转查 502。

## 安全恢复边界
日志、trace 和配置读取可自动执行。调 timeout、限流、熔断、扩容或回滚需审批；不得只扩大超时掩盖持续阻塞。

## 恢复后验证
确认 504、各阶段耗时和重试量持续回落，上游资源恢复，端到端 deadline 保持层级一致，并验证慢请求取消后资源确实释放。

## 来源
- Nginx Proxy Module：https://nginx.org/en/docs/http/ngx_http_proxy_module.html
- Nginx Log Module：https://nginx.org/en/docs/http/ngx_http_log_module.html
许可按 Nginx 官方页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
