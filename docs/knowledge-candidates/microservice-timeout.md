# 微服务延迟与超时：用调用链区分慢依赖和网络问题

先确认用户影响和SLO，再查看入口延迟、服务处理时间、下游span、重试次数和超时预算。入口504不能单独证明根因。

服务处理时间增长且下游正常时调查应用资源；下游span占主要耗时则调查依赖；连接建立或传输耗时增长则调查网络和DNS；重试同步升高时调查超时配置和重试风暴。恢复后验证p95/p99、错误率、重试量和端到端请求。

来源：https://aws.plainenglish.io/resolving-cascading-latency-in-aws-hosted-microservices-performance-incident-post-mortem-1626d5ee1c4b
来源：https://github.com/googlecloudplatform/microservices-demo/issues/3475
本卡片为AgentPy原创摘要，访问日期：2026-08-12
