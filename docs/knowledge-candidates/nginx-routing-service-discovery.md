# Nginx 路由与服务发现端点漂移差分排查

## 适用现象
特定路径、实例或发布批次出现 404、502 或流量不均；后端整体健康但 Nginx 访问了错误位置。

## 候选原因
- location 匹配、rewrite 或路径前缀配置错误。
- upstream 地址、端口或 DNS 缓存仍指向旧实例。
- 服务发现包含未就绪端点或遗漏健康实例。
- reload 未生效或不同网关实例配置漂移。

## 建议证据
保存生效配置与 location 匹配结果、请求 URI/Host、实际 upstream 地址、DNS/服务发现记录、端点就绪状态、各网关配置摘要和 reload 时间线。

## 如何区分
请求未进入预期 location 偏向路由；location 正确但 upstream 地址过期偏向发现；只命中未就绪实例偏向端点发布；同请求在不同网关结果不同偏向配置漂移。上游地址正确且处理超时应转查性能。

## 安全恢复边界
配置 dump、解析和端点读取可自动执行。rewrite、resolver、upstream 或 reload 需审批并先通过配置检查；不得绕过鉴权路径临时导流。

## 恢复后验证
用代表性 Host/URI 覆盖各路由，确认所有网关配置一致、流量仅进入就绪端点、错误率恢复且旧端点不再接收连接。

## 来源
- Nginx Request Processing：https://nginx.org/en/docs/http/request_processing.html
- Nginx Upstream Module：https://nginx.org/en/docs/http/ngx_http_upstream_module.html
许可按 Nginx 官方页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
