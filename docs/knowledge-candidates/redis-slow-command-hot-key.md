# Redis 慢命令、大键与热键差分排查

## 适用现象
Redis 延迟或 CPU 上升，部分请求超时。高复杂度命令、大键操作和单热键流量都可能占用单线程事件循环。

## 候选原因
- 慢日志中的高复杂度或大范围命令阻塞服务。
- 大键序列化、删除或传输造成单次长停顿。
- 热键产生集中 QPS，即使命令本身很短也压满 CPU。
- 主机调度、fork、存储或网络造成非命令延迟。

## 建议证据
关联 SLOWLOG、commandstats、latency monitor、CPU、事件循环延迟、键大小抽样、按键访问频度和客户端流量；记录 fork/AOF/RDB 与主机延迟事件。

## 如何区分
少数命令执行时间长且复杂度高偏向慢命令；单次响应体或键元素数异常偏向大键；命令短但同一键/分片 QPS 高且 CPU 饱和偏向热键；Redis 内部耗时低而端到端高则排查网络或客户端。

## 安全恢复边界
慢日志和受限采样可自动执行。拆键、改命令、限流或迁移热键需审批；避免在高峰运行全量键扫描或同步删除大键。

## 恢复后验证
确认慢日志频率、事件循环延迟、CPU 和超时下降，流量不再集中，且拆分或异步删除没有破坏数据一致性。

## 来源
- Redis Latency Diagnosis：https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/
- Redis SLOWLOG：https://redis.io/docs/latest/commands/slowlog/
许可按 Redis 官方文档页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
