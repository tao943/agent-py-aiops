# 队列消费者停滞差分排查

## 适用现象
消费者仍显示在线但 ack/commit 停止、消息年龄增长。进程卡死、租约问题、下游阻塞和分区分配异常均可造成停滞。

## 候选原因
- 消费循环、线程或事件循环卡住。
- 下游调用或事务长期阻塞，消息未确认。
- consumer group rebalance、租约或可见性超时异常。
- 分区倾斜，部分消费者空闲而热点分区停滞。

## 建议证据
收集心跳、poll/receive、ack/commit、in-flight、处理耗时、线程栈、下游 trace、group/lease 状态、分区 lag 和重试时间线。

## 如何区分
心跳存在但 poll/ack 均停且栈固定偏向进程卡住；消息处理中下游 span 长偏向依赖；频繁 rebalance 或 lease 丢失偏向协调；仅单分区 lag 增长偏向倾斜或该分区毒消息。

## 安全恢复边界
状态和有界 stack dump 可自动执行。重启消费者、转移分区、延长租约或取消事务需审批；先确认幂等和消息可见性，避免重复副作用。

## 恢复后验证
确认 poll 与成功 ack/commit 恢复、各分区 lag 和最老年龄收敛、无重复业务结果，并验证重新平衡后仍稳定。

## 来源
- Apache Kafka Consumer Configs：https://kafka.apache.org/documentation/#consumerconfigs
- RabbitMQ Consumer Acknowledgements：https://www.rabbitmq.com/docs/confirms
许可按各官方页面，仅作机制参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
