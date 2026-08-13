# PostgreSQL 磁盘、WAL 与 checkpoint 压力差分排查

## 适用现象
数据库写入变慢、WAL 目录增长、checkpoint 耗时升高或磁盘告警。容量不足、复制保留和 IO 吞吐不足需要分别处理。

## 候选原因
- 数据文件、日志或 WAL 消耗可用字节或 inode。
- 复制槽、归档失败或备库延迟阻止旧 WAL 回收。
- checkpoint 过密、后台写入突增或底层存储延迟造成 IO 压力。

## 建议证据
采集文件系统字节与 inode、WAL 目录变化率、归档成功/失败、复制槽保留量、checkpoint 频率与写入时间、数据库和主机 IO 延迟；关联流量和配置变更。

## 如何区分
空间或 inode 接近耗尽是容量路径；WAL 增长与 inactive slot 或归档失败同步是保留路径；空间充足但 checkpoint/write time 与 IO latency 同升是性能路径。不要把 WAL 多直接解释为业务写流量。

## 安全恢复边界
容量与统计采集可自动执行。扩容、调整 checkpoint、修复归档或处理复制槽需审批；禁止直接删除 `pg_wal` 文件。

## 恢复后验证
确认空间水位与 WAL 增长率稳定、归档/复制重新推进、checkpoint 与提交延迟回落，并检查重启与恢复能力未受损。

## 来源
- PostgreSQL WAL Configuration：https://www.postgresql.org/docs/current/wal-configuration.html
- PostgreSQL Checkpoints：https://www.postgresql.org/docs/current/wal-configuration.html#WAL-CONFIGURATION-CHECKPOINTS
许可证：PostgreSQL License；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
