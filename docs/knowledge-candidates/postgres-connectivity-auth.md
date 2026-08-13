# PostgreSQL 连接、认证与 TLS 失败差分排查

## 适用现象
客户端无法建立新连接，表现为连接拒绝、超时、认证失败或握手失败；已有连接可能仍正常。

## 候选原因
- 地址、端口、DNS、路由或监听配置不可达。
- `pg_hba.conf`、角色、数据库名或认证方式不匹配。
- TLS 信任链、主机名、协议或服务端证书异常。

## 建议证据
从应用相同网络位置记录解析结果和 TCP 探测，采集 PostgreSQL 监听地址、服务端认证日志、匹配的 HBA 规则、客户端错误类别与 TLS 握手摘要；对比最近网络、证书和角色配置变更。

## 如何区分
TCP 建立前超时或拒绝指向网络、监听或进程；TCP 成功后服务端明确报告 no pg_hba entry 或 authentication failed 指向认证；证书验证、SNI 或协议告警指向 TLS。连接池等待但服务端已有大量活跃会话应转查池或慢事务。

## 安全恢复边界
解析、端口和配置读取可自动执行。修改 HBA、角色权限、证书或防火墙需审批并保留回滚；禁止为恢复而开放全网访问或关闭证书校验。

## 恢复后验证
从实际应用路径建立加密连接并执行只读查询，确认新旧实例均成功、认证日志无拒绝、连接池恢复，且权限没有扩大。

## 来源
- PostgreSQL Client Authentication：https://www.postgresql.org/docs/current/client-authentication.html
- PostgreSQL SSL Support：https://www.postgresql.org/docs/current/ssl-tcp.html
许可证：PostgreSQL License；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
