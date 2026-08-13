# PostgreSQL 复制延迟差分排查

## 适用现象
只读副本数据陈旧、replay lag 上升或 WAL 持续堆积。主库写入突增、网络传输、备库回放和复制槽滞留可能同时存在。

## 候选原因
- 主库 WAL 生成速率超过链路或备库处理能力。
- 网络抖动或 WAL receiver 中断导致传输延迟。
- 备库 IO/CPU、长查询冲突或恢复暂停导致回放延迟。
- 失活复制槽保留 WAL，但并不代表活跃副本正在落后。

## 建议证据
在同一时间窗保存 sent/write/flush/replay LSN 与 lag、WAL 生成速率、receiver/replay 状态、复制槽 active 与 retained bytes、两端 CPU/IO/网络、备库冲突日志和长查询。

## 如何区分
sent 与 write 差距扩大偏向传输；flush 与 replay 差距扩大且备库资源忙偏向回放；所有 LSN 接近但槽保留量大偏向失活 slot；主库 WAL 突增而各阶段均持续推进说明容量暂时不足，不等同链路中断。

## 安全恢复边界
状态与 LSN 采集可自动执行。删除复制槽、重建副本、取消备库查询或切换主从需审批；不得自动删除用途不明的 slot。

## 恢复后验证
确认 LSN 差距持续收敛、只读数据新鲜度恢复、WAL 保留量下降且 receiver/replay 无再次中断，并验证读流量未压垮副本。

## 来源
- PostgreSQL Warm Standby：https://www.postgresql.org/docs/current/warm-standby.html
- PostgreSQL Replication Statistics：https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-REPLICATION-VIEW
许可证：PostgreSQL License；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
