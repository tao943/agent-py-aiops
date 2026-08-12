# Redis不可用：区分服务故障、网络问题与客户端恢复缺陷

检查Redis健康、端口、连接拒绝或超时类型、客户端连接池、socket timeout、重试、keepalive、服务端日志、慢查询、内存和连接数，以及应用是否正确降级到PostgreSQL。

Redis进程停止表示服务可用性问题；Redis健康但客户端超时需调查网络或连接池；Redis恢复后应用仍不可用需调查客户端重建和健康检查。测试环境可恢复单个Redis并验证重连，禁止无审批FLUSH或修改生产拓扑。

来源：https://repost.aws/knowledge-center/elasticache-redis-client-error-messages
来源：https://www.honeycomb.io/blog/using-honeycomb-to-investigate-a-redis-connection-leak
来源：https://github.com/tracer-cloud/opensre/blob/fbec9fe6f3b51f2b845fa8868856d65908b66ccd/docs/redis.mdx
本卡片为AgentPy原创摘要，访问日期：2026-08-12
