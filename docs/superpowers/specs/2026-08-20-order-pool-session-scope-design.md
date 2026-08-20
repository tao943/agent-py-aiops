# Order Pool PostgreSQL Session Scope 截断修复设计

**日期：** 2026-08-20  
**状态：** 已确认，待实施

## 问题

真实 Run `order-pool-diagnostics-single-20260820-225508` 的连接池、业务超时、数据库健康和无锁等待
检查均通过，只有 `run_scoped_sessions_present` 失败。安全事实显示 `checkedOutConnections=3`，但
`runScopedSessionCount=0`。

order-api 当前把 PostgreSQL `application_name` 设置为
`agentpy-order-api:<完整 run_id>:<完整 generation>`。该 Run 的完整名称为 96 字节，而 PostgreSQL
上限为 63 字节；`agentpy-order-api:<完整 run_id>` 已占 63 字节，后续分隔符与 generation 被截断。
observer 查询要求完整 Run ID 后仍有冒号，因此无法匹配真实会话。

## 设计

session scope 改为：

```text
agentpy-order-api:<run_hash_16>:<generation_prefix_16>
```

其中 `run_hash_16 = SHA-256(run_id).hexdigest()[:16]`，与现有 `LiveRunIdentity.run_token` 算法一致；
`generation_prefix_16 = generation[:16]`。包含首个分隔符的固定前缀为 18 字节，再加两个 16 字节
token 和中间分隔符，合计 51 字节，不超过 PostgreSQL 63 字节限制。

order-api 独立 Docker 服务根据完整 Run ID 计算 session scope；backend observer 使用相同纯函数构造
run-scoped LIKE 与 generation 精确查询。完整 Run ID 继续用于 HTTP 路径、订单记录、事件、CLS 与
Evaluation Artifact，不改变隔离和审计身份。空闲池连接仍使用 `agentpy-order-api:idle`，无关会话排除
规则不变。

## 安全与兼容性

- 不缩短用户可见 Run ID，不改变 Run ID 唯一约束。
- 64-bit run scope 与 64-bit generation scope 仅用于隔离测试会话标签，不作为授权或持久化主键。
- control token、恢复授权、cleanup、评分、Agent、RAG、CLS 和 Artifact 均不修改。
- 不新增依赖、服务、数据库 migration 或配置项。
- 不兼容旧进程中的长格式 active session；实施后必须重建 order-api 镜像，并在新 Run 前确认无旧残留。

## 验收

先写长 Run ID RED 测试，证明旧 `_app_name` 超过 63 字节且 observer 查询使用不可能匹配的完整前缀。
实现后验证：

- 短、最长允许的 64 字符 Run ID 均生成不超过 63 字节的稳定标签；
- order-api 与 observer 对同一 Run ID/generation 生成相同标签；
- 不同 Run ID 或 generation 不同标签；
- run-scoped、lock-wait、generation 查询全部使用短 scope；
- 空闲连接与无关会话排除合同不变；
- 目标 pytest、Ruff、Pyright 和真实 Docker recovery contract 通过；
- 重建 order-api 后，只运行一条新的唯一 Single canary。失败时立即 Verify/Cleanup，不自动重跑。
