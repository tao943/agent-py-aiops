# PostgreSQL 死锁回滚与普通锁等待差分排查

## 适用现象
事务偶发回滚并出现 deadlock detected，或请求重试后放大负载。死锁、长时间单向阻塞和序列化冲突需要不同处理。

## 候选原因
- 两个事务以相反顺序获取资源，形成等待环。
- 单个长事务造成普通锁队列，但没有环。
- 应用对回滚无限重试，使瞬时冲突演化成拥塞。

## 建议证据
收集 PostgreSQL deadlock 日志中的进程、语句和资源，关联事务调用链与加锁顺序；同时记录 `pg_locks` 等待图、`deadlock_timeout`、回滚 SQLSTATE、重试次数及退避间隔。

## 如何区分
服务端明确检测到等待环并主动回滚一个事务才是死锁；只有 blocker→waiter 单向链是普通锁等待；SQLSTATE 或日志显示序列化失败则属于并发隔离冲突。重试次数在原始冲突后快速上升说明应用策略正在放大故障。

## 安全恢复边界
可自动提取日志和等待图。统一加锁顺序、缩短事务或修改重试策略需要代码评审；终止会话需审批。不得通过关闭死锁检测掩盖问题。

## 恢复后验证
以相同并发路径验证不再出现等待环，回滚率和重试量恢复，普通锁等待没有被转移到其他资源，并确认事务结果幂等。

## 来源
- PostgreSQL Deadlocks：https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-DEADLOCKS
- PostgreSQL Error Codes：https://www.postgresql.org/docs/current/errcodes-appendix.html
许可证：PostgreSQL License；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: verified
docker_validation_date: 2026-08-14
docker_validation_scope: isolated_live_eval_fixture
已在隔离容器中验证相反资源访问顺序可形成等待环、触发数据库回滚，并可通过事务结果与独立健康检查确认恢复结果。
reviewed_on: 2026-08-13
