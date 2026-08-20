## Context

`APY-LIVE-ORDER-POOL-LEAK-001` 模拟 order-api 异常订单更新在 checkout 后未归还 asyncpg 连接，
最终令固定容量连接池耗尽。Runtime 只证明池饱和、业务获取连接超时、PostgreSQL 可达且无锁等待；
CLS 日志只证明异常请求存在 checkout/error 且没有对应 checkin。只有两类真实证据合并后才能形成完整
因果链。

## Goals / Non-Goals

**Goals:** 可重复注入；真实订单 SQL 与连接生命周期；Single/Multi 公平 A/B；安全恢复、Verify、
Cleanup；完整审计和答案隔离。

**Non-Goals:** 不引入 Kubernetes、Chaos Mesh、外部数据集或 GitHub Change Investigator；不修改聊天
Agent；不在离线和 Docker 门禁前执行付费 3×3 A/B；不因该场景默认启用 Multi-Agent。

## Decisions

### Isolated order-api

服务只接受固定 `agent_py_live_eval` 数据库和内部 control token，一次只允许一个 active run。
`live_eval_orders` 表仅保存 run-scoped 测试订单。正常更新始终在 `try/finally` 中归还连接；故障路径
执行同一参数化更新后触发确定性业务异常并有界持有连接。

### Actual CLS records

order-api 暴露当前 run 的安全事件投影。`OrderPoolClsRecordProvider` 读取 `/events`，经固定字段和事件
枚举校验后复用现有 CLS 上传/轮询。旧场景继续使用原模板，避免兼容性回退。

### Fair strategy routing

Single 与 Multi 获得完全相同的 Runtime/CLS 工具、可信参数和全局预算。Multi 仅并行 Runtime/Log
Investigator；两个分支原子领取同一个预算 ledger，不得复制额度。评分器不读取 strategy 标签。

### Scoped recovery

只有隔离 run 独占 `live-eval-order-api` 且证据闭环时，Recovery Service 才能用稳定 intent 重启固定
Compose service。若副作用后、terminal persistence 前崩溃，重放进入人工复核而非再次重启。生产环境
始终只生成重启、回滚和代码修复提案。

## Risks / Trade-offs

- 重启是隔离实验的缓解动作，不等同于永久修复，因此 Verify 同时检查 generation、旧连接、数据库和
  业务更新，文档必须明确生产需代码修复。
- CLS 索引延迟属于基础设施无效样本，不计入能力分数。
- 如果 Single 仍满分，场景仍是有效 Live 合同，但结果必须标记为无 Multi 能力增益。
