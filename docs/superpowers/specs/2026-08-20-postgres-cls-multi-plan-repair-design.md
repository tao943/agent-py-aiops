# PostgreSQL CLS Multi 计划修复设计

**日期：** 2026-08-20

**状态：** 已确认

**范围：** 修复 PostgreSQL Live 场景在启用 CLS 时只形成 Runtime 调查计划、无法进入有效 Multi-Agent 路由的问题，并补充不调用真实外部服务的场景测试。

## 1. 问题

PostgreSQL Live 的通用 fallback 会生成 `VerifyServiceHealth`、`InspectPostgresSessions` 和
`InspectPostgresLockGraph` 等 Runtime 步骤。当前实现只有在通用计划完全为空时才生成
`SearchLog`，因此 Runtime 计划非空时，即使 Live 证据源为 CLS 且 `SearchLog` 已发现，Router 仍只看到
一个未完成数据域。

同时，这些 PostgreSQL Live 工具由项目内受控 server `docker-live-postgres` 声明，但 Runtime 能力的
server allowlist 尚未包含该名称。即使计划中存在 Runtime 步骤，能力注册表也会按 fail-closed 规则拒绝
将其映射为可信 Runtime 数据域。

结果是 Benchmark 请求 `strategy=multi` 时触发 `insufficient_parallel_sources`，有效策略保持
`single_agent`。这不是 Router 门槛问题，而是 Router 上游的公开计划没有表达已经配置的日志调查来源。

## 2. 目标与非目标

### 2.1 目标

1. Live 证据源明确为 CLS、`SearchLog` 已发现且计划尚无 Log 步骤时，补充一个作用域受限的日志步骤。
2. 保留已有 Runtime 计划和执行顺序，不依赖模型是否主动生成日志步骤。
3. 让能力注册表从实际工具重新推导 `runtime` 和 `log` 数据域，使 Benchmark 强制 Multi 可以形成两个可信 Dispatch。
4. 仅将项目内既有的 `docker-live-postgres` 加入 Runtime server allowlist，不信任用户自定义 server。
5. 用离线测试证明 PostgreSQL Lock + CLS 的公开输入可以选择真正的 Multi-Agent 路由。

### 2.2 非目标

- 不读取 Scenario Oracle 或 `ground_truth.yaml` 决定是否查询日志。
- 不修改 Router 阈值、硬门禁、Validator、评分规则或恢复权限。
- 不让生产 API 强制 Multi；强制策略仍只属于内部 Benchmark。
- 不调用真实 LLM、CLS、PostgreSQL Live fixture 或 Docker。
- 不为已有 Log 步骤追加重复查询。

## 3. 方案

采用“计划形成后、合同规范化前合并 CLS 步骤”。

```text
公开 Live 配置 + 已发现工具
        |
受控 server allowlist 验证 Runtime 工具
        |
通用/模型计划形成
        |
若 source=CLS 且 SearchLog 可用且计划无 Log 步骤
        +-- 追加绑定当前 incident/run/time scope 的 SearchLog
        |
工具合同规范化
        |
能力注册表重算 sourceDomain
        |
Strategy Router -> Runtime + Log -> Multi-Agent
```

选择该边界的原因：

- CLS 查询参数已经由 Live adapter 绑定，计划不能扩大 region、topic、incident 或时间范围。
- PostgreSQL Runtime 工具继续受工具名与 server 名双重白名单保护；只补登记项目已有 server 名称。
- Router 继续只消费实际可执行 Plan Step，不需要根据环境凭空推断数据域。
- 模型仍可主动规划 `SearchLog`；代码只在缺失时补充一次，避免重复查询。
- Knowledge Investigator、Runtime/Log Investigator、Aggregator 和后续闭环保持不变。

## 4. 安全与失败处理

- 是否补充日志步骤仅由公开执行上下文决定：发现的 `SearchLog` 来自精确 `cls` server、存在受信
  incident/run/time 参数绑定，并且绑定结果通过实际工具 JSON Schema。
- 日志 Step 的参数必须继续通过现有 MCP JSON Schema 和 trusted argument binding 校验。
- 已存在 `SearchLog` 或 `SearchLogs` 步骤时不追加。
- CLS 不可用、使用 local evidence 或缺少可信 scope 时保持 Runtime-only，Router 应继续安全降级为 Single。
- 来自 `docker-live-postgres` 以外未知 server 的同名 Runtime 工具仍必须被拒绝。
- 已有计划达到四步上限时保持原计划并安全降级，不为加入 Log 而静默删除 Runtime 步骤。
- 任何计划合同错误仍走现有 normalization/fallback，不通过降低安全门禁换取 Multi。

## 5. 测试设计

按 TDD 增加以下目标测试：

1. **计划合并回归**：给定非空 PostgreSQL Runtime 计划和受信 CLS `SearchLog`，结果同时包含 Runtime 与一个 Log 步骤。
2. **去重**：计划已含日志步骤时不追加第二个日志步骤。
3. **禁用边界**：CLS 不可用时不追加日志步骤。
4. **Runtime server 边界**：真实 PostgreSQL Live 工具声明可以映射为 Runtime；未知 server 的同名工具仍被拒绝。
5. **场景路由**：使用 PostgreSQL Lock 场景的公开 hypotheses、真实 Runtime 工具声明和 CLS scope，完成计划规范化后，强制 Multi 选择 `runtime`、`log`，且不含 `insufficient_parallel_sources`。
6. **答案隔离**：测试数据不加载或向 Agent/Router 传入 `ground_truth.yaml`。

场景路由测试只证明“该类公开输入可以真正执行 Multi”，不宣称 Multi 已产生准确率提升。生产默认启用仍需完整真实 A/B 与能力增益门禁。

## 6. 验收标准

- 新回归测试在实现前因缺少 CLS 合并行为而失败，实现后通过。
- 相关 Live adapter、Router、fan-out/fan-in 和答案隔离测试通过。
- Ruff 与 Pyright 通过。
- 不运行全量 pytest，不产生真实模型或 CLS 调用。
- 不新增依赖，不提交配置、密钥、`var/` 或 Benchmark Archive。
