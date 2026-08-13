# 服务线程池与执行器饱和差分排查

## 适用现象
请求排队、超时、活跃线程贴顶或事件循环卡顿。线程不足、同步阻塞和下游等待需要不同治理。

## 候选原因
- 工作线程/协程池容量小于有效并发。
- 阻塞 IO 或锁占用执行器，任务无法推进。
- 下游慢使线程长期等待。
- 无界队列掩盖饱和并放大尾延迟。

## 建议证据
保存 active/idle threads、queue depth、等待/执行时间、线程栈或 async task 状态、锁争用、下游 span、CPU 和请求并发；关联池配置与发布变化。

## 如何区分
队列增长且 CPU 低、线程栈集中等待下游，偏向依赖；CPU 高且任务持续运行偏向计算容量；大量线程阻塞同一锁偏向同步争用；线程未满但事件循环延迟高偏向在 async 路径执行阻塞调用。

## 安全恢复边界
指标和有界 stack dump 可自动执行。改池大小、隔离执行器、限流、回滚或重启需审批；不得无界增加线程造成上下文切换或数据库过载。

## 恢复后验证
确认排队时间、深度和尾延迟恢复，线程/协程能持续周转，下游未被额外并发压垮，并验证取消和超时路径释放资源。

## 来源
- Python asyncio Development：https://docs.python.org/3/library/asyncio-dev.html
- Java ThreadPoolExecutor：https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/concurrent/ThreadPoolExecutor.html
许可按官方文档，仅作跨运行时机制参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
