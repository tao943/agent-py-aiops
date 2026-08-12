# PostgreSQL连接池耗尽：区分泄漏、慢事务和容量不足

典型症状是请求等待连接超时、延迟上升、连接数接近上限。检查应用池参数、活跃和空闲连接、pg_stat_activity、长事务、锁等待、慢查询、泄漏迹象以及发布和流量变化。

长事务占用连接时调查事务边界和锁；空闲连接持续增长时调查连接未归还；活跃请求和连接数同步增长时判断容量与并发配置是否失配。不要仅增大max_connections，高风险终止事务和修改池参数必须审批。

来源：https://postmortems.app/postmortem/34b4a47b-a3d6-4bda-acc9-57b621f53468
来源：https://pigsty.io/docs/pgsql/tutorial/failure/
本卡片为AgentPy原创摘要，访问日期：2026-08-12
