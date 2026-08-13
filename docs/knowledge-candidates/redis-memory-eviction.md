# Redis 内存淘汰与缓存击穿差分排查

## 适用现象
缓存命中率下降、evicted_keys 增长、写入报 OOM 或后端数据库流量上升。达到 maxmemory 后的行为取决于策略与键空间，不能只看内存百分比。

## 候选原因
- 工作集超过 maxmemory，策略持续淘汰可淘汰键。
- noeviction 或无合适候选导致写命令被拒绝。
- 大键、内存碎片或过期键清理压力占用内存。
- 淘汰引发回源与重建，形成缓存击穿放大。

## 建议证据
保存 used_memory、maxmemory、mem_fragmentation_ratio、evicted/expired_keys、命中率、策略、键 TTL/大小分布、命令延迟，以及数据库 QPS 与回源并发。

## 如何区分
evicted_keys 与命中率同步恶化偏向容量淘汰；写入明确返回 OOM 且淘汰为零偏向策略拒绝；少数大键或碎片率异常偏向布局问题；Redis 指标变化后数据库负载陡升说明回源放大已发生。

## 安全恢复边界
INFO、慢日志和抽样扫描可自动执行但要限速。改淘汰策略、删除键、调整上限或预热缓存需审批；禁止无范围控制地清库。

## 恢复后验证
确认淘汰/拒绝速率、命中率、延迟和数据库回源恢复，内存水位稳定且关键数据语义符合新策略。

## 来源
- Redis Key Eviction：https://redis.io/docs/latest/develop/reference/eviction/
- Redis Memory Optimization：https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/memory-optimization/
许可按 Redis 官方文档页面；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
