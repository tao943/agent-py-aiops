# Redis maxclients 与连接风暴差分排查

## 适用现象
新连接被拒绝、客户端池等待或 connected_clients 接近上限。配置上限、应用连接泄漏和短连接风暴可能产生相同告警。

## 候选原因
- connected_clients 达到 maxclients，服务端拒绝新连接。
- 应用实例连接未释放，连接数随时间单调增长。
- 发布、扩缩容或错误重试产生大量短连接。
- 主机文件描述符上限低于 Redis 配置的有效容量。

## 建议证据
收集 connected/rejected_connections、maxclients、clients list 的年龄与来源、每实例池指标、连接建立速率、发布/扩缩容时间线，以及 Redis 进程和主机 FD limit/使用量。

## 如何区分
连接稳定贴顶且老连接集中于少数实例偏向泄漏；连接建立率呈尖峰并随发布或重试同步偏向连接风暴；Redis 配置较高但日志提示无法提高文件上限偏向 FD；服务可达且连接数不高则转查网络或池内部等待。

## 安全恢复边界
客户端统计和 FD 读取可自动执行。断开连接、扩大 maxclients/FD 或滚动应用需审批；不得批量关闭来源不明的生产连接。

## 恢复后验证
确认拒绝连接归零、连接数与实例/池预算匹配、建立速率恢复，FD 保持余量，且连接复用没有引入新延迟。

## 来源
- Redis Client Handling and Limits：https://redis.io/docs/latest/develop/reference/clients/
- Redis CONFIG maxclients：https://redis.io/docs/latest/commands/config-set/
许可按 Redis 官方文档页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: verified
docker_validation_date: 2026-08-14
docker_validation_scope: isolated_live_eval_fixture
已在隔离容器中验证客户端槽位耗尽会拒绝新连接，同时既有连接仍可用，并可通过作用域客户端计数与独立 PING 检查恢复结果。
reviewed_on: 2026-08-13
