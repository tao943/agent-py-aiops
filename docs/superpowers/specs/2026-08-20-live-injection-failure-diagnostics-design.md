# Live 注入失败安全诊断设计

**日期：** 2026-08-20  
**状态：** 已确认，待实施  
**范围：** Docker Live Benchmark 在 `fault_injection_failed` 时的安全可观测性

## 背景

Order Pool Single canary 在 Agent、RAG 和 LLM 执行前以
`fault_injection_failed` 终止。Live driver 已产生六项布尔检查和一组显式
`safe_facts`，但 `LiveBenchmarkRunner` 抛出的 `LiveBenchmarkError` 只携带失败分类与阶段，
导致 CLI、PostgreSQL 和 Evaluation Archive 无法判断具体哪项检查失败。继续重跑只能增加付费调用前的
环境扰动，不能产生可行动证据。

## 目标与非目标

目标是在不泄露答案、凭据、原始日志或异常文本的前提下，持久化已经由 driver 明确声明为安全的注入
检查结果，并让命令行直接显示失败检查名称。新记录必须同时写入现有 PostgreSQL JSONB 与
Evaluation Archive，继续经过统一 Artifact 校验和 checksum 计算。

本次不修改故障注入条件、恢复授权、评分、Agent Workflow、CLS 查询、数据库表结构或 Artifact 顶层
schema；也不根据本次失败猜测某项检查并降低门槛。注入函数在返回 Observation 前直接抛异常时没有
结构化检查可用，仍只保存现有通用失败分类。

## 复用评估

项目已有完整的 `LiveBenchmarkError -> Live CLI -> EvaluationRunEnvelope ->
EvaluationRunRecorder -> Archive/PostgreSQL` 链路，并有递归禁用字段检查与 evaluation-kind 允许列表。
这是最小且安全的扩展点。

GitHub 检索比较了 `numirias/pytest-json-report`（MIT）和
`allure-framework/allure-python`（Apache-2.0）。两者适合通用测试报告，但会引入依赖，且不会自动执行
本项目的 Oracle、Prompt 与凭据隔离合同。选择复用项目内部链路并增加项目自有类型化字段；不采用
外部代码或依赖。

## 数据合同

`LiveBenchmarkError` 增加不可变、类型化的可选注入诊断，不接受任意 Mapping。诊断只由
`LiveFaultObservation` 投影生成，包含：

- `checkResults`：按 driver 原顺序保存 `{name, passed, source}`；
- `failedChecks`：按原顺序保存未通过检查的名称，便于检索和 CLI 展示；
- `safeFacts`：只保存 Observation 已显式标记的标量事实。

所有字段继续经过 Evaluation Artifact 的顶层允许列表和递归禁用键检查。字段值只允许 JSON 标量；
不得包含原始异常消息、事件列表、CLS 日志、Prompt、模型响应、Token、Oracle、Ground Truth 或
Primary Cause。诊断缺失时不写空占位字段，保持旧 Artifact 的稳定形状。

Artifact schema version 保持 `v1`：新增字段是 Live `resultPayload` 的可选允许字段，旧 Artifact 不需要
迁移。PostgreSQL 使用现有 JSONB，无数据库 migration。

## 数据流与失败处理

当 `driver.inject()` 返回未确认的 Observation 时，Runner 从 Observation 构造安全诊断并附加到
`LiveBenchmarkError`。清理流程继续给同一个错误附加 `cleanup_succeeded`，不得丢失诊断。

CLI 捕获错误后：

1. 将 `checkResults`、`failedChecks`、`safeFacts` 写入 terminal envelope；
2. Archive-first 持久化并计算 canonical checksum；
3. PostgreSQL best-effort 同步同一 envelope；
4. 安全 CLI/report 输出只增加 `failedChecks`，不展开全部安全事实。

如果注入方法直接抛异常、诊断结构校验失败或遇到禁用字段，系统 fail closed：不持久化未经验证的诊断
内容，不降级允许任意 Mapping，也不改变原始故障分类和 cleanup 语义。

## 测试与验收

采用定向测试，不运行全量 pytest：

- Runner：未确认 Observation 产生完整、顺序稳定的类型化诊断；cleanup 后诊断不丢失；
- Runner：inject 直接抛异常时不伪造检查结果；
- CLI：失败终态在 Archive 与 repository 接收相同的 `checkResults`、`failedChecks`、`safeFacts`；
- CLI/report：只公开 `failedChecks`，不公开全部事实；
- Artifact：新字段可 round-trip，旧 Artifact 继续可读；
- 安全：嵌套 `oracle`、`ground_truth`、`primary_cause`、凭据或非标量值被拒绝；
- 回归：现有 Live runner、CLI、history、archive 测试通过；Ruff、Pyright 通过。

完成代码验证后先运行不调用 LLM/CLS 的本地失败路径，证明 Artifact 能指出具体检查。只有获得具体
失败证据并另行确认后，才修复对应 harness 并运行新的唯一真实 canary。
