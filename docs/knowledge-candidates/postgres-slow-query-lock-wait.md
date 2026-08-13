# PostgreSQL 查询变慢与锁等待差分排查

## 适用现象
接口数据库耗时升高、活跃会话累积或连接池等待增加。慢 SQL、锁阻塞和主机资源压力都可能形成相同曲线，不能只看单条耗时。

## 候选原因
- 查询计划或数据量变化导致执行变慢。
- 长事务持锁，后续会话排队。
- 存储 IO、CPU 或 checkpoint 压力拖慢大量查询。

## 建议证据
同步保存 `pg_stat_activity` 的 query、state、query_start、wait_event，阻塞链与事务年龄；补充 `pg_stat_statements` 分位耗时、执行计划、数据库 CPU/IO、WAL/checkpoint 和应用连接池等待。

## 如何区分
单类 SQL 无锁事件但执行时间上升，偏向计划或数据问题；大量会话显示 `wait_event_type=Lock` 且指向少数 blocker，偏向锁链；多类 SQL 同时变慢并伴随 IO/CPU 压力，偏向数据库资源瓶颈。连接池耗尽可能只是上述问题的下游结果。

## 安全恢复边界
只读查询和计划采样可自动执行。取消查询、终止会话、建立索引或调整参数需审批，并先确认事务所有者与业务影响；不得自动终止未知生产事务。

## 恢复后验证
确认阻塞链消失、目标 SQL 分位耗时和池等待持续回落，业务错误率恢复且没有新锁链或副作用；仅短暂下降不算闭环。

## 来源
- PostgreSQL Monitoring Statistics：https://www.postgresql.org/docs/current/monitoring-stats.html
- PostgreSQL Explicit Locking：https://www.postgresql.org/docs/current/explicit-locking.html
许可证：PostgreSQL License；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
