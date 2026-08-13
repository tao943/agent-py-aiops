# Kubernetes 容器内存饱和与 OOM 差分排查

## 适用现象
容器重启、退出码 137、延迟恶化或节点内存告警。137 只表示 SIGKILL，不能单独证明 OOM。

## 候选原因
- 容器达到 memory limit，被 cgroup OOM kill。
- 应用泄漏或工作集增长，limit 只是触发边界。
- 节点级 MemoryPressure 引发驱逐或系统 OOM。
- 人工停止、探针或外部控制器造成 SIGKILL。

## 建议证据
保存 termination reason、OOMKilled、restart count、limit/request、工作集与 RSS 趋势、Pod 事件、kubelet/内核 OOM 日志、节点压力、驱逐记录和应用分配指标。

## 如何区分
OOMKilled 为真且使用量贴近 limit 指向容器边界；工作集跨版本持续增长偏向泄漏；多个命名空间 Pod 同节点受影响并有 MemoryPressure 偏向节点；OOMKilled 为假则保留外部 SIGKILL、探针与人工动作。

## 安全恢复边界
指标、事件和日志读取可自动执行。重启、调 limit、转移节点或生成 heap dump 需审批并评估数据与负载；不得只扩大 limit 掩盖泄漏。

## 恢复后验证
确认重启停止、内存曲线稳定、节点压力解除、端到端请求恢复，并观察足够窗口验证内存不会再次单调增长。

## 来源
- Kubernetes Resource Management：https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- Kubernetes Node-pressure Eviction：https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
许可证：CC BY 4.0；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
