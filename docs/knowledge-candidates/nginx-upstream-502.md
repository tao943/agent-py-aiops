# Nginx upstream 502 连接失败差分排查

## 适用现象
网关返回 502，error log 出现 connect failed、connection refused 或 invalid header。502 是代理结果，不等于后端进程停止。

## 候选原因
- 上游进程退出或未监听目标端口。
- upstream 主机、端口、DNS 或服务发现记录错误。
- Nginx 与上游协议不匹配，或上游返回无效响应头。
- 网络策略或容器路径阻断连接。

## 建议证据
保存 Nginx 精确错误类别、实际 upstream 地址、解析结果与生效配置；从 Nginx 网络空间直连上游，核对进程/Pod、监听端口、健康检查、网络策略和最近配置变更。

## 如何区分
connection refused 且无监听偏向进程；后端健康但 Nginx 指向其他端口偏向配置；解析失败偏向 DNS；TCP 成功后出现协议或 header 错误偏向协议。连接成功但等待响应超时应转查 504/上游慢。

## 安全恢复边界
配置检查、解析和只读探测可自动执行。reload、改 upstream、回滚或重启需审批；变更前必须运行 `nginx -t`，不得跳过配置校验。

## 恢复后验证
确认入口与直连请求均成功、502 和连接错误持续归零、生效 upstream 与预期一致，且 reload 未中断已有连接。

## 来源
- Nginx Proxy Module：https://nginx.org/en/docs/http/ngx_http_proxy_module.html
- Nginx Upstream Module：https://nginx.org/en/docs/http/ngx_http_upstream_module.html
许可证/文档条款按 Nginx 官方页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
