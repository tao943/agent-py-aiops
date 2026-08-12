# Kubernetes DNS解析失败：证据优先排查

适用：Pod解析Service或外部域名失败、间歇性超时和SERVFAIL。本卡片是基于Kubernetes官方DNS debugging文档的原创摘要。

## 调查顺序

1. 从受影响Pod检查resolv.conf、nameserver、search domain和ndots。
2. 分别查询短Service名、完整FQDN和外部域名，区分搜索域与CoreDNS问题。
3. 检查CoreDNS状态、重启次数、就绪状态和错误日志。
4. 检查Service、EndpointSlice、网络策略和节点连通性。
5. 将失败时间与发布、节点压力和CoreDNS变更对齐。

## 根因区分

- 仅短名称失败、FQDN成功：优先检查search domain或应用拼接。
- 集群内所有名称失败：检查CoreDNS、kube-dns Service和网络路径。
- 只有一个Service失败：检查selector、EndpointSlice和端口。
- 仅外部域名失败：检查上游DNS、出口策略和CoreDNS转发。

不要直接重启所有CoreDNS Pod。先保存配置和事件；生产配置修改必须审批。

来源：https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/
许可证：Kubernetes文档CC BY 4.0；访问日期：2026-08-12
