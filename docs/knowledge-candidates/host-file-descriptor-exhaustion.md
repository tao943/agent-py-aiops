# 文件描述符耗尽与连接泄漏差分排查

## 适用现象
应用报 too many open files，新连接或文件打开失败。进程软限制、主机全局限制和资源泄漏均可触发。

## 候选原因
- 进程 `RLIMIT_NOFILE` 过低且正常负载达到上限。
- socket、文件、pipe 或 watch 未关闭，数量持续增长。
- 主机 file table 接近全局上限，多个进程受影响。
- 短连接风暴或积压暂时占用大量 FD。

## 建议证据
采集进程 limits、`/proc/<pid>/fd` 数量与类型、增长趋势、系统 file-nr、连接状态、打开速率、实例分布和发布/流量时间线；正文路径需脱敏。

## 如何区分
单进程贴近软限制而系统有余量偏向进程配置或泄漏；FD 数单调增长且同类 socket/file 集中偏向泄漏；多进程同时失败且 file table 接近上限偏向主机；连接数随流量尖峰后回落偏向暂时风暴。

## 安全恢复边界
limits 与 FD 分类读取可自动执行。提高限制、重启、关闭连接或修复代码需审批；不得批量关闭未知 FD 或仅提上限掩盖泄漏。

## 恢复后验证
确认 FD 水位与流量成比例并可回落，新连接/文件操作恢复，主机保持余量，经过完整负载周期不再单调增长。

## 来源
- Linux getrlimit Manual：https://man7.org/linux/man-pages/man2/getrlimit.2.html
- Linux proc pid fd：https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html
Linux man-pages 许可按项目声明；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
