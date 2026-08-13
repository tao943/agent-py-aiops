# Kubernetes Service 与 Endpoint 不匹配差分排查

## 适用现象
Service DNS 可解析但连接拒绝、超时或无后端；Pod 直连可能正常。selector、readiness 和端口映射是主要分支。

## 候选原因
- Service selector 未匹配目标 Pod label。
- Pod 未 Ready，未进入 EndpointSlice。
- port/targetPort、协议或命名端口不匹配。
- EndpointSlice 陈旧或网络策略阻断 Service 路径。

## 建议证据
保存 Service spec、selector 与 Pod labels、EndpointSlice addresses/conditions/ports、Pod readiness、容器监听端口、DNS 结果，以及 Pod IP 与 ClusterIP 的分路径探测。

## 如何区分
EndpointSlice 为空且 labels 不匹配偏向 selector；Pod 存在但 ready=false 偏向 readiness；端点存在但端口与监听不一致偏向 targetPort；Pod IP 成功而 ClusterIP 失败再查 Service 转发或策略。DNS 正确不代表端点正确。

## 安全恢复边界
对象与连通性读取可自动执行。改 selector、port、readiness 或网络策略需审批；不得把未就绪 Pod 强行加入生产流量。

## 恢复后验证
确认 EndpointSlice 仅包含 Ready Pod 且端口一致，Pod IP/ClusterIP/实际入口都成功，滚动发布期间端点能正确增删。

## 来源
- Kubernetes Services：https://kubernetes.io/docs/concepts/services-networking/service/
- Kubernetes EndpointSlices：https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/
许可证：CC BY 4.0；本卡为原创归纳，访问日期 2026-08-13。

## 验证状态
content_type: agentpy-original-summary
docker_validation: pending
reviewed_on: 2026-08-13
