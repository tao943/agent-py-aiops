# Redis 故障转移与客户端重连差分排查

## 适用现象
主节点切换后短暂错误持续，服务端已恢复但部分应用仍超时或写向旧节点。故障转移完成不代表客户端恢复闭环完成。

## 候选原因
- Sentinel/Cluster 尚未完成选主或槽位收敛。
- DNS、拓扑缓存或代理仍指向旧地址。
- 客户端池保留失效连接，退避或重试策略阻碍重连。
- 新主节点可达但尚未承受恢复后的请求风暴。

## 建议证据
采集角色、复制和集群状态，故障转移时间线与端点变化；从应用实例记录解析结果、连接目标、错误类别、连接代际、重连/重试次数以及新主节点延迟和负载。

## 如何区分
集群本身无稳定主节点或槽位异常是服务端收敛问题；新建连接成功而池内旧连接持续失败是客户端生命周期问题；不同实例解析到不同地址偏向发现缓存；全部连接成功但新主过载是恢复流量问题。

## 安全恢复边界
拓扑和连接状态读取可自动执行。强制切主、清空连接池、改 DNS TTL 或重启应用需审批和分批执行；不得在角色未确认时双向写入。

## 恢复后验证
确认所有应用实例连接到正确角色，旧连接消失，错误率和重试量稳定回落，新主复制与资源健康，并进行读写语义验证。

## 来源
- Redis Sentinel：https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/
- Redis Client Handling：https://redis.io/docs/latest/develop/reference/clients/
许可按 Redis 官方文档页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
