# 毒消息、重试与死信队列差分排查

## 适用现象
同一消息反复失败、重试率或 DLQ 增长。确定性坏载荷、暂时依赖失败和处理幂等缺陷需要不同处置。

## 候选原因
- schema、字段或业务约束不兼容，消息每次都确定性失败。
- 下游暂时不可用，正常消息也批量失败。
- 消费者在副作用后未确认，重复处理触发幂等冲突。
- 重试与 DLQ 路由配置错误形成循环。

## 建议证据
保存脱敏消息 schema/版本与稳定指纹、异常类别、尝试历史、不同消费者结果、下游健康、ack/commit 时点、幂等键和 DLQ 路由计数；不记录敏感正文。

## 如何区分
同一指纹跨实例稳定报解析/校验错误偏向毒消息；多类消息同时随依赖故障失败偏向暂时错误；业务已成功但消息重现偏向确认或幂等；消息在主队列和 DLQ 循环偏向路由配置。

## 安全恢复边界
元数据、指纹和错误分类可自动执行。隔离、重放、修 schema 或补偿业务需审批；不得直接删除 DLQ 或重放未验证幂等的消息。

## 恢复后验证
确认目标指纹不再循环、正常吞吐恢复、DLQ 增长停止、重放只产生一次业务结果，并保留不可处理消息的审计记录。

## 来源
- RabbitMQ Dead Letter Exchanges：https://www.rabbitmq.com/docs/dlx
- Apache Kafka Error Handling Design：https://kafka.apache.org/documentation/
许可按各官方页面，仅作机制参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
