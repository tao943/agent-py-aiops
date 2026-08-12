# Nginx upstream 502：连接拒绝不等于唯一根因

用于网关返回502并出现upstream connection refused。必须同时检查Nginx实际upstream主机和端口、DNS解析、后端容器状态、监听端口、直连健康检查和最近配置变更。

后端退出且无监听端口表示上游进程不可用；后端健康监听8080而Nginx指向8081表示端口不匹配；主机名无法解析表示DNS问题；连接成功但超时则调查上游处理慢或依赖阻塞。

不要仅凭502决定根因。至少需要网关证据和上游状态证据，并排除最接近的替代原因。恢复前执行配置检查，恢复后验证入口200、健康检查和错误率。

来源：https://github.com/kubernetes/ingress-nginx/issues/3639
来源：https://developer.cloud.tencent.cn/article/2717750
本卡片为AgentPy原创摘要，访问日期：2026-08-12
