# 主机磁盘容量、inode 与只读故障差分排查

## 适用现象
写文件失败、日志/WAL 停止、容器异常或磁盘告警。字节耗尽、inode 耗尽和文件系统只读会返回不同但易混淆的错误。

## 候选原因
- 数据、日志、镜像或临时文件耗尽可用字节。
- 大量小文件耗尽 inode，仍可能显示剩余容量。
- IO/文件系统错误导致 remount read-only。
- 已删除但仍被进程打开的文件占用空间。

## 建议证据
采集各挂载点 bytes/inodes、目录增长率、mount flags、内核与磁盘错误、open-deleted 文件、IO 延迟和应用 errno；关联日志轮转、发布与数据增长。

## 如何区分
available bytes 归零偏向容量；inode free 归零偏向小文件；日志出现 filesystem error 且 mount 为 ro 偏向存储故障；目录统计小但 df 高偏向删除未释放。高 IO 延迟但空间足够属于性能压力。

## 安全恢复边界
容量、inode、mount 和受限目录统计可自动执行。删除、扩容、remount 或重启需审批并锁定精确路径；禁止递归删除未知目录或直接清理数据库文件。

## 恢复后验证
确认字节/inode 恢复安全余量、文件系统可写、错误和 IO 延迟恢复，增长源受控且服务完成真实写入。

## 来源
- Linux statfs Manual：https://man7.org/linux/man-pages/man2/statfs.2.html
- Linux mount Manual：https://man7.org/linux/man-pages/man8/mount.8.html
Linux man-pages 许可按项目声明；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
