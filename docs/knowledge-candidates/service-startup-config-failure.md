# 服务启动配置与依赖就绪失败差分排查

## 适用现象
新实例反复退出、启动探针失败或长期未就绪。配置解析、依赖不可达和探针窗口过短均可能发生在启动阶段。

## 候选原因
- 必填配置、环境变量、密钥引用或格式错误。
- 数据库、队列或配置中心尚未就绪。
- migration、初始化任务或权限失败。
- startup/readiness probe 路径、端口或时间预算错误。

## 建议证据
保存首次退出码与启动日志、脱敏后的配置 schema 校验、依赖连接错误、初始化阶段时间、探针事件与配置、上一版本差异和依赖健康。

## 如何区分
进程在监听前明确配置解析失败偏向配置；连接错误随依赖恢复消失偏向就绪顺序；应用可直连但探针失败偏向探针；migration/权限日志明确失败属于初始化路径。不要把 CrashLoop 本身当根因。

## 安全恢复边界
日志、schema 和探针读取可自动执行。修配置、回滚、运行 migration 或改探针需审批；不得输出密钥值或跳过必要 migration。

## 恢复后验证
确认冷启动可重复成功、startup/readiness 均通过、依赖连接稳定、配置未泄露且滚动发布不会再次触发顺序问题。

## 来源
- Kubernetes Probes：https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/
- The Twelve-Factor App Config：https://12factor.net/config
Kubernetes 文档 CC BY 4.0，其他页面仅作参考；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
