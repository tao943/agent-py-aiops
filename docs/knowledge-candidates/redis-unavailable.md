# Redis 请求失败：区分服务端、客户端连接和网络路径

## 适用现象

应用访问 Redis 出现连接拒绝、超时或连接池等待，请求错误率上升，并可能降级到 PostgreSQL。相同表象既可能来自 Redis 服务端，也可能来自网络或客户端恢复问题。

## 候选原因

- Redis 服务端不可用：进程停止、端口未监听、启动失败或服务自身拒绝连接。
- 客户端连接池或重连异常：Redis 已可用，但应用仍复用失效连接、借用者等待或连接池没有重建。
- 网络路径异常：Redis 与客户端各自健康，但 DNS、路由、防火墙或连接追踪路径阻断或高延迟。
- 服务端资源压力：内存、客户端数、慢命令或 CPU 压力使请求变慢，但不一定导致端口不可达。

## 建议证据

1. Redis 进程状态、监听端口、就绪状态和从应用网络位置执行的 PING。
2. 连接拒绝、建立超时、读写超时等错误类别及其发生时间。
3. 客户端池大小、使用中连接、等待者、失效连接、重连次数和连接代际。
4. 服务端连接数、内存、延迟、慢命令、拒绝连接和错误日志。
5. DNS 解析、路由和应用到 Redis 端口的连通性。
6. Redis 重启、故障转移、应用发布、客户端配置和网络变更时间线。
7. 应用是否正确进入降级，以及 Redis 恢复后是否能退出降级。

## 如何区分

- Redis 进程停止或端口无监听，并且客户端收到连接拒绝：优先处理服务端可用性。
- Redis 健康、直接新连接成功，但应用池仍有大量等待者或失效连接：优先调查客户端池重建、健康检查和重连策略。
- 服务端与客户端新连接测试分别正常，但跨网络路径超时或解析失败：调查网络和 DNS。
- Redis 可达但命令延迟、内存或客户端数明显异常：调查服务端资源和工作负载，而不是把所有超时归为网络故障。

单次 PING 只能证明该时刻的一条路径可用；单个内存百分比或近期发布也不能独立确定根因。需要把服务端健康、客户端池状态和网络证据放在同一时间窗口比较。

## 安全恢复边界

先保存服务端状态、客户端错误和网络证据。测试环境可以恢复单个 Redis 服务或重建单个应用实例的连接池，并验证降级行为。禁止无审批执行 `FLUSHDB`、删除未知 Key、改变生产拓扑或扩大范围重启；客户端参数和故障转移配置修改需要评审和回滚方案。

## 恢复后验证

验证从应用侧 PING 和业务命令成功、连接池等待者和失效连接下降、错误率恢复、Redis 延迟和资源安全、降级流量回落，并观察一段时间确认重连稳定。Redis 服务恢复但应用仍失败说明客户端或网络恢复链尚未闭合。

## 来源

- Redis Debugging Guide：https://redis.io/docs/latest/operate/oss_and_stack/management/debugging/
- Redis Client Guidance：https://redis.io/docs/latest/develop/clients/
- AWS re:Post Redis client errors：https://repost.aws/knowledge-center/elasticache-redis-client-error-messages
- Honeycomb Redis connection leak investigation：https://www.honeycomb.io/blog/using-honeycomb-to-investigate-a-redis-connection-leak
- OpenSRE Redis reference：https://github.com/tracer-cloud/opensre/blob/fbec9fe6f3b51f2b845fa8868856d65908b66ccd/docs/redis.mdx

许可证说明：Redis 与 OpenSRE 内容的适用许可证以各源站为准；AWS re:Post 和 Honeycomb 页面仅作来源引用，未复制原文。本卡片为 AgentPy 原创摘要，访问日期：2026-08-12。

## 验证状态

content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
