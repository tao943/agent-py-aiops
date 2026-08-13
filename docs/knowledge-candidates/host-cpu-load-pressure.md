# 主机 CPU 与 load average 压力差分排查

## 适用现象
CPU、load average、延迟或调度等待上升。高 load 可能来自可运行任务，也可能来自不可中断 IO 等待，不能等同 CPU 饱和。

## 候选原因
- 用户态计算或 busy loop 消耗 CPU。
- 容器配额导致 throttling，即使主机仍有空闲。
- IO 阻塞任务抬高 load，但 CPU idle 仍较高。
- steal time、内核或中断压力影响可用算力。

## 建议证据
保存 user/system/iowait/steal、run queue、load、容器 throttled time、进程/线程 CPU、调度与上下文切换、磁盘延迟和 profile/stack 摘要。

## 如何区分
CPU busy 与 run queue 同升偏向计算饱和；容器 throttling 高而主机空闲偏向 quota；load 高但 CPU idle/iowait 高偏向阻塞；steal 高偏向虚拟化争用。单个 load 数值不能判断根因。

## 安全恢复边界
指标和有界 profile 可自动执行。调 quota、限流、迁移、kill 进程或回滚需审批；不得终止归属不明的系统进程。

## 恢复后验证
确认 run queue、throttling、CPU 分项和延迟恢复，吞吐没有因粗暴限流下降，并在代表性负载下验证无 busy loop 复发。

## 来源
- Linux proc loadavg：https://man7.org/linux/man-pages/man5/proc_loadavg.5.html
- Kubernetes CPU Limits：https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
Kubernetes 文档 CC BY 4.0，man-pages 许可按项目声明；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
