# 容器内存饱和与OOM：不要只凭退出码判断

137表示进程收到SIGKILL，但不能单独证明是OOM。

## 证据

检查退出码、OOMKilled、重启次数、内存limit/request、工作集趋势、kubelet事件、节点压力、驱逐记录和应用日志。

OOMKilled为真且使用量接近limit时，优先判断limit不足或内存泄漏；OOMKilled为假时保留外部SIGKILL、手工停止和节点压力等候选。多个Pod同时受影响时优先调查节点级压力。

恢复前确认对象属于本次运行；只执行可回滚的测试环境重启。恢复后验证健康、内存趋势和用户请求。

来源：https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
来源：https://github.com/MicrosoftDocs/SupportArticles-docs/blob/main/support/azure/azure-kubernetes/availability-performance/identify-memory-saturation-aks.md
访问日期：2026-08-12
