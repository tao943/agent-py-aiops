# Kubernetes DNS 解析失败差分排查

## 适用现象
Pod 解析 Service 或外部域名失败、间歇超时、NXDOMAIN 或 SERVFAIL。短名称、集群域名和外部域名的失败范围是关键区别。

## 候选原因
- Pod `resolv.conf`、search domain 或 ndots 与查询方式不匹配。
- CoreDNS Pod、kube-dns Service 或上游转发异常。
- 仅目标 Service/EndpointSlice 错误，被误认为 DNS 故障。
- NetworkPolicy、节点网络或出口路径阻断 DNS 流量。

## 建议证据
从受影响 Pod 分别查询短名、FQDN 和外部域名，保存 `/etc/resolv.conf`；检查 CoreDNS 状态、日志、配置、Service/EndpointSlice、网络策略和节点分布，并对齐发布与变更时间。

## 如何区分
短名失败而 FQDN 成功偏向搜索域；所有集群名失败偏向 CoreDNS 或其网络；只有一个 Service 失败需核对 Service 与 Endpoint；仅外部名失败偏向上游转发或出口。DNS 返回地址正确但连接失败应转查服务或网络。

## 安全恢复边界
DNS 查询和对象读取可自动执行。修改 CoreDNS、网络策略或 Pod DNS 配置需审批；不得在未保存日志与配置前批量重启 CoreDNS。

## 恢复后验证
从不同节点的实际工作负载验证短名、FQDN、外部名和目标端口，确认错误率持续下降、CoreDNS 健康且没有把问题转移到单节点。

## 来源
- Kubernetes DNS Debugging：https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/
- Kubernetes DNS for Services and Pods：https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/
许可证：CC BY 4.0；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
