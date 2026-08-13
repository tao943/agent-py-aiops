# Kubernetes Pod CrashLoopBackOff 差分排查

## 适用现象
Pod 重启次数增长并进入退避。应用退出、探针终止、配置错误和资源限制都可能形成 CrashLoopBackOff。

## 候选原因
- 应用启动或运行时异常主动退出。
- liveness/startup probe 失败触发 kubelet 重启。
- ConfigMap、Secret、挂载或依赖配置错误。
- OOM、权限、文件系统或节点问题终止容器。

## 建议证据
保存 current/last termination reason、exit code、前一实例日志、Pod 事件、探针配置与结果、资源状态、挂载、调度节点和最近 workload/config revision。

## 如何区分
应用 stack trace 后非零退出偏向程序；事件显示 probe failed 后 killed 偏向探针；创建即因挂载/配置失败偏向依赖对象；OOMKilled 或节点事件指向资源。BackOff 只是 kubelet 重试状态。

## 安全恢复边界
describe、日志和对象读取可自动执行。回滚、改探针/资源、重建 Pod 或修配置需审批；不得只删除 Pod 反复掩盖确定性启动失败。

## 恢复后验证
确认新 Pod 跨完整启动窗口保持 Ready、重启计数不再增加、实际请求成功，并验证重新调度或再次启动仍可恢复。

## 来源
- Kubernetes Pod Lifecycle：https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- Kubernetes Debug Running Pods：https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/
许可证：CC BY 4.0；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
