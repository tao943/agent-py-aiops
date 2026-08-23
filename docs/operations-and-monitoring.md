# 配置与运维

应用只读取本地 `config/project.json` 和可选的 `config/user.project.json`，不读取本机环境变量。两个文件都被 Git 忽略；仓库只保留不含真实凭据的模板。不要为本项目创建 `.env` 文件，也不要提交本地配置。

首次使用时，在仓库根目录执行：

```bash
cp config/project.template.json config/project.json
cp config/user.project.template.json config/user.project.json
```

## 生产恢复安全默认值

`productionRecovery` 默认关闭且两个目标白名单均为空。只有在本地忽略的
`config/user.project.json` 中显式开启全局开关并配置受控 target，恢复 Worker 才可能排队；
提交的模板不会包含 Compose 绝对路径、数据库连接串或凭据。Compose 自动恢复还要求 target
显式 `automaticRecoveryEnabled=true`，PostgreSQL blocker 终止始终需要当前 Incident owner
在 600 秒内审批。每个 PostgreSQL target 还必须配置 `databaseIdentity` 和非空
`lockResourceMappings`，把诊断使用的逻辑资源（例如 `order_row`）映射到固定的
schema/relation。该映射只保存在被忽略的本地配置中，不进入 API；PID 始终来自执行前 fresh
probe，模型、Prompt、旧 Evidence 和客户端均不能指定。

### 隔离生产恢复验收

生产恢复验收只验证已持久化诊断之后的控制面，不调用 benchmark oracle，也不依赖 LLM、
CLS 或自动报警链路。测试先制造真实故障并据实写入 owner-scoped Task、Evidence、Report 和
active Incident，再只通过正式 Recovery HTTP API 创建或审批 Intent；执行器不能由测试直接
调用。独立监视器只在容器/数据库恢复信号成立后把 Incident 标记为 resolved。

在 `live-eval-order-api` 和测试 PostgreSQL 已启动后，从 `apps/backend` 显式运行：

```bash
uv run pytest tests/live/test_production_compose_recovery.py -q
uv run pytest tests/live/test_production_postgres_recovery.py -q
```

Compose 用例只重启隔离的 `live-eval-order-api`，并验证重复请求收敛、容器身份变化、健康与
业务探针、Incident resolved、单条 recovery execution 和完整审计状态。PostgreSQL 用例只在
`recovery_test.orders` 制造锁等待，验证跨 owner/错误确认被拒绝、审批绑定、唯一 blocker 被
终止、waiter 前进及无关连接存活。两项测试都会改变各自的隔离 fixture，不属于普通 CI。

这两条命令不证明 `Alertmanager → 自动诊断 → RecoveryIntent` 上游链路已经验收；自动报警
闭环应使用独立的端到端场景验证，避免把 LLM/CLS 波动混入恢复执行器的正确性判断。

## 项目交付后的个人配置

在本地 `config/user.project.json` 中填写使用者自己的模型密钥、CLS 凭据和 CLS 日志目标。其他运行参数可在本地 `config/project.json` 中调整：

| 配置字段 | 替换为 |
| --- | --- |
| `llm.apiKey` | 使用者自己的模型服务密钥 |
| `clsMcpServer.secretId` | 使用者自己的 CLS 凭据 ID |
| `clsMcpServer.secretKey` | 使用者自己的 CLS 凭据密钥 |
| `clsLogUpload.region` | 使用者自己的 CLS 地域 |
| `clsLogUpload.logsetId` | 使用者自己的 CLS 日志集 ID |
| `clsLogUpload.topicId` | 使用者自己的 CLS 主题 ID |

模板中的凭据字段必须保持为空。即使仓库是私有仓库，也不要提交真实密钥；已暴露的密钥应在对应云平台立即轮换。

## 真实日志与本地告警样例

真实 CLS 日志上传和本地 active-alert 演示属于显式运维流程，不是常规应用启动的一部分。请遵循[真实 CLS 日志与告警教程](tutorials/real-log-and-alert.md)，分别执行其中的日志上传脚本和本地告警命令。
