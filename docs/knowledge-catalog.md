# RAG 知识卡目录

这 30 张通用差分排查卡是项目的可导入知识资产。它们用于检索与引用，不包含 Benchmark 的场景答案、ground truth、oracle 或评分规则。

## PostgreSQL

- [连接与认证](./knowledge-candidates/postgres-connectivity-auth.md)
- [死锁](./knowledge-candidates/postgres-deadlock.md)
- [磁盘与 WAL 压力](./knowledge-candidates/postgres-disk-wal-pressure.md)
- [连接池耗尽](./knowledge-candidates/postgres-pool-exhaustion.md)
- [复制延迟](./knowledge-candidates/postgres-replication-lag.md)
- [慢查询与锁等待](./knowledge-candidates/postgres-slow-query-lock-wait.md)

## Redis

- [故障转移后的重连](./knowledge-candidates/redis-failover-reconnect.md)
- [maxclients 压力](./knowledge-candidates/redis-maxclients-pressure.md)
- [内存淘汰](./knowledge-candidates/redis-memory-eviction.md)
- [慢命令与热键](./knowledge-candidates/redis-slow-command-hot-key.md)
- [服务不可用](./knowledge-candidates/redis-unavailable.md)

## Nginx 与网络

- [限流重试风暴](./knowledge-candidates/http-rate-limit-retry-storm.md)
- [微服务超时](./knowledge-candidates/microservice-timeout.md)
- [路由与服务发现](./knowledge-candidates/nginx-routing-service-discovery.md)
- [Upstream 502](./knowledge-candidates/nginx-upstream-502.md)
- [Upstream Timeout](./knowledge-candidates/nginx-upstream-timeout.md)
- [TLS 证书与握手失败](./knowledge-candidates/tls-certificate-handshake-failure.md)

## Kubernetes

- [DNS 排查](./knowledge-candidates/kubernetes-dns-debugging.md)
- [内存饱和与 OOM](./knowledge-candidates/kubernetes-memory-saturation.md)
- [Pod CrashLoop](./knowledge-candidates/kubernetes-pod-crashloop.md)
- [Service Endpoint 不匹配](./knowledge-candidates/kubernetes-service-endpoint-mismatch.md)

## 主机与服务

- [CPU 与负载压力](./knowledge-candidates/host-cpu-load-pressure.md)
- [磁盘容量压力](./knowledge-candidates/host-disk-capacity-pressure.md)
- [文件描述符耗尽](./knowledge-candidates/host-file-descriptor-exhaustion.md)
- [熔断器降级](./knowledge-candidates/service-circuit-breaker-degradation.md)
- [启动配置失败](./knowledge-candidates/service-startup-config-failure.md)
- [线程池饱和](./knowledge-candidates/service-thread-pool-saturation.md)

## 消息队列

- [队列积压](./knowledge-candidates/queue-backlog.md)
- [消费者停滞](./knowledge-candidates/queue-consumer-stalled.md)
- [毒消息与死信队列](./knowledge-candidates/queue-poison-message-dlq.md)
