# AgentPy Benchmark、RAG 与 Live 场景扩展设计

日期：2026-08-14

## 1. 背景

AgentPy 当前具有六个 Snapshot 场景、三十张通用差分排障知识卡、六十条 Retrieval
Benchmark 查询，以及一个可分别使用本地 PostgreSQL 证据或真实腾讯云 CLS 证据的
PostgreSQL 行锁 Live 场景。现有真实 Retrieval 历史基线经历过两个阶段：早期两卡六问
smoke 得到全满分；扩展为三十卡、六十问后，Document Recall@1 为 0.9259、Recall@3
为 1.0、MRR 为 0.9599。后一个结果才代表当前知识竞争难度。

本次扩展不通过“一场景一答案卡”增加表面数量。Snapshot 描述具体事故实例，知识卡描述
可迁移的故障族，Retrieval 查询测试不同表达能否找到通用知识，Live 场景负责在隔离环境中
重现并验证事实。四者通过评测器专用映射关联，但答案和映射不会暴露给 Agent。

## 2. 目标

- 将 Snapshot 场景从六个扩展到十个。
- 保持三十张知识卡，使用真实实验深化其验证状态，不增加重复答案卡。
- 将 Retrieval Benchmark 从六十条扩展到六十四条。
- 将 Live 场景总数从一个扩展到四个。
- 为 Redis 与 Nginx 建立默认不启动的隔离 Live Eval 实验室。
- 让四个 Live 场景均支持本地证据与真实 CLS 证据模式。
- 对四个 Live 场景逐个执行一次真实 LLM 与 CLS 验收。
- 保留确定性评分、答案隔离、证据审计、恢复白名单、run 隔离和安全报告。

## 3. 非目标

- 不增加 Kubernetes、Chaos Mesh、Litmus 或新的编排平台。
- 不把 Snapshot、ground truth、Retrieval 标签或固定 evidence ID 导入 RAG。
- 不增加四张与新场景一一对应的答案卡。
- 不让普通 CI 调用 Docker、腾讯云、Milvus 或真实模型。
- 不在 Nginx timeout 场景中自动修改配置、切流或重启正式服务。
- 不为了获得满分而修改正确标签、删除困难查询或把 oracle 提示写入 Prompt。
- 本阶段不实现 CLS 自动告警触发诊断；该能力在场景覆盖稳定后单独设计。

## 4. 已确认决策

1. 采用按阶段推进：Snapshot、Retrieval、Live 框架、Live 驱动、真实验收。
2. 新增四个当前技术栈内的 Snapshot 场景。
3. 知识卡保持三十张，Retrieval 查询增加四条。
4. Live 总数固定为四个，包含现有 PostgreSQL 行锁场景。
5. Redis 和 Nginx Live 使用独立 Compose `live-eval` profile。
6. PostgreSQL deadlock 与 Redis maxclients 允许严格作用域内的低风险恢复。
7. Nginx timeout 正确生成方案并停止在审批边界即可满足恢复策略要求。
8. 四个 Live 场景最终都执行一次真实 LLM 与 CLS 验收，且按顺序运行。

## 5. 复用评估

### 5.1 项目内直接复用

- Snapshot 的 `scenario.yaml`、`ground_truth.yaml`、`provenance.yaml` 和录制响应合同。
- 场景加载、答案键污染检查、路径穿越防护和 Artifact 构造。
- 确定性评分、多候选差分排查、Citation 审计和安全硬门禁。
- 三十卡 heading-aware chunking、批量覆盖导入、PostgreSQL 状态与 Milvus scope。
- 六十问 loader、文档级 Recall/MRR、forbidden Top-1 和 Citation 通道审计。
- Live runner 的 inject、prepare evidence、diagnose、recover、verify、cleanup 阶段。
- 现有 PostgreSQL Live Driver、复合 MCP 客户端、CLS SDK uploader 和 bounded polling。
- `VALID_PASS`、`VALID_FAIL`、`INFRA_INVALID` 与安全报告出口。

### 5.2 GitHub 候选

| 候选 | 许可证 | 适用性 | 决策 |
|---|---|---|---|
| `Tracer-Cloud/opensre` | Apache-2.0 | PostgreSQL/Redis 工具、Benchmark 完整性和来源治理 | 仅参考，不复制场景答案或评分体系 |
| `chaos-mesh/chaos-mesh` | Apache-2.0 | Kubernetes 网络、Pod、IO、压力故障 | 当前 Docker Compose 过重，不采用 |
| `litmuschaos/litmus` | Apache-2.0 | Kubernetes Chaos 实验与工作流 | 当前范围不匹配，不采用 |
| `alexei-led/pumba` | Apache-2.0 | 容器网络、停止和压力故障 | 可参考隔离与清理模式，不新增运行时依赖 |

### 5.3 结论

采用项目内直接复用与外部 reference-only。新增场景需要项目自有的 answer isolation、
run-scoped 证据、恢复审批和确定性评分，重型 Chaos 平台无法直接满足这些合同，且会增加
原生容器权限与供应链面。Live 故障由小型项目 Driver 构造，不增加依赖或外部服务。

## 6. Snapshot 场景

每个新目录保持以下结构：

```text
benchmarks/agentpy/scenarios/APY-XXX/
├── scenario.yaml
├── ground_truth.yaml
├── provenance.yaml
└── snapshot/tool_responses.yaml
```

### 6.1 APY-013：PostgreSQL deadlock

- 公开现象：订单相关事务出现失败、回滚和短时间重试，数据库仍可连接。
- 主根因：两个事务以相反顺序更新资源，形成等待环并触发 SQLSTATE `40P01`。
- 强替代原因：普通长锁等待、单条慢查询、连接池耗尽。
- 必要证据：死锁错误、事务资源顺序、等待环或数据库 deadlock 记录。
- 禁止捷径：只看到 `Lock` 等待或一次超时就断言 deadlock。
- 恢复边界：只重试数据库已中止的当前业务事务，不终止无关会话。

### 6.2 APY-014：Redis maxclients

- 公开现象：Redis 新连接被拒绝，已有部分操作仍可能成功。
- 主根因：带 Benchmark 身份的客户端耗尽 Redis `maxclients`。
- 强替代原因：Redis 进程停止、网络不可达、主机 FD 耗尽、客户端旧连接。
- 必要证据：`maxclients`、`connected_clients`、`rejected_connections` 和 scoped client list。
- 禁止捷径：把所有连接错误都解释为服务进程停止。
- 恢复边界：只关闭当前 run 的 Benchmark 客户端。

### 6.3 APY-015：Nginx upstream timeout

- 公开现象：网关在有界等待后返回 504，上游端口可以建立连接。
- 主根因：upstream 已接受连接但响应时间超过 `proxy_read_timeout`。
- 强替代原因：进程停止、端口错误、DNS/路由错误、网关自身 CPU 压力。
- 必要证据：connect 成功、request time、upstream response time、upstream 健康探针。
- 禁止捷径：仅凭 504 判断端口错误或服务停止。
- 恢复边界：只能给出回滚、切流或恢复 upstream 的审批方案。

### 6.4 APY-016：HTTP rate-limit retry storm

- 公开现象：429 比例和总请求量同时上升，保护生效后流量没有回落。
- 主根因：客户端忽略 `Retry-After`，使用无退避重试放大请求。
- 强替代原因：正常限流、恶意流量、下游变慢、限流容量配置过低。
- 必要证据：每请求尝试次数、429 时间线、退避缺失、客户端相关性和下游健康。
- 禁止捷径：把所有 429 都解释为攻击或网关容量不足。
- 恢复边界：生成客户端退避、熔断或临时隔离方案；本阶段不转换为 Live。

所有公开场景只包含告警、症状和可用工具提示。主根因、候选优先级、trigger、evidence
milestone 与禁止结论只存在于 evaluator-only 文件。Snapshot 响应同时包含正确证据与强干扰
证据，要求 Agent 主动区分候选原因。

## 7. RAG 与 Retrieval Benchmark

### 7.1 知识卡策略

三十张知识卡保持不变。新增场景分别复用：

| Snapshot | 通用知识卡 |
|---|---|
| APY-013 | `postgres-deadlock.md` |
| APY-014 | `redis-maxclients-pressure.md` |
| APY-015 | `nginx-upstream-timeout.md` |
| APY-016 | `http-rate-limit-retry-storm.md` |

新增 evaluator-only 覆盖清单，验证十个 Snapshot 都至少映射一张存在且通过 catalog audit
的通用知识卡。该清单不得进入 Milvus、Prompt、Agent 输入或报告。

### 7.2 四条新增查询

| ID | 类型 | 目标卡 |
|---|---|---|
| `RET-L-013` | 日志/SQLSTATE 信号 | `postgres-deadlock.md` |
| `RET-O-009` | 口语与轻微扰动 | `redis-maxclients-pressure.md` |
| `RET-A-015` | 模糊 504 现象 | `nginx-upstream-timeout.md` |
| `RET-X-009` | 429 强干扰 | `http-rate-limit-retry-storm.md` |

查询不得出现场景 ID、固定 Snapshot 数值、ground truth 字段、trigger 或 evidence ID；也
不得把知识卡段落直接改写成问句。扩展后共六十四条：五十八条有答案、六条无答案探针。
原有指标门槛保持不变。

### 7.3 真实评测与 RAG 对照

1. 使用修正后的 Citation 通道合同运行六十四问真实 Retrieval Eval。
2. 固定模型、Prompt、Workflow、Tool 和场景，对四个新 Snapshot 分别运行 RAG off/on。
3. 比较主根因、必要证据、禁止结论、工具调用、Citation、总分、耗时和模型用量。
4. RAG-on 不得引入错误根因、跨场景引用或 ground truth 泄漏。
5. 不要求每个场景 RAG-on 必然高于 RAG-off；不回写标签或删题制造提升。

Live 通过后，将 `postgres-deadlock.md`、`redis-maxclients-pressure.md` 和
`nginx-upstream-timeout.md` 的 `docker_validation` 更新为 `verified`，保留非答案化的
验证摘要。`http-rate-limit-retry-storm.md` 继续为 `pending`。治理章节不形成向量 Chunk，
但变更后的文档仍通过现有 overwrite importer 安全替换并重新核验 scope。

## 8. 通用 Live 架构

### 8.1 场景注册与 Driver

CLI 根据场景 ID 从注册表取得场景能力，不再将 PostgreSQL 行锁构造写死在命令边界。
所有 Driver 实现相同生命周期：

```text
inject → audit → prepare evidence → diagnose → recover/propose
       → verify → cleanup
```

稳定接口包括：

- `inject(identity, scenario) -> LiveFaultObservation`
- `audit(identity) -> LiveInfrastructureAudit`
- `recover(identity, decision) -> LiveRecoveryRecord`
- `verify(identity) -> LiveVerification`
- `cleanup(identity) -> LiveCleanupResult`

Driver 只接收验证后的 run identity 和公开场景，不接收 oracle。`cleanup` 在成功、失败、
超时和取消路径都执行，并保持幂等。

### 8.2 独立 Compose profile

`infra/compose.yaml` 增加默认不启动的 `live-eval` profile：

- `live-eval-redis`：低 `maxclients` 的专用 Redis。
- `live-eval-upstream`：可产生确定性慢响应的最小测试 upstream。
- `live-eval-nginx`：只路由到测试 upstream 的专用 Nginx。

默认项目 Redis、Nginx、backend 和 frontend 不作为故障目标。Live 服务使用独立端口、
网络别名、健康检查和 Benchmark 标识。普通 `docker compose up`、普通 CI 与本地开发不会
启动 profile。

### 8.3 新 Live 场景

#### APY-LIVE-PG-DEADLOCK-001

真实执行两个更新顺序相反的事务。PostgreSQL 的 deadlock detector 会中止一个事务，
Driver 在真实事件期间捕获等待环、SQLSTATE `40P01`、事务身份和结果，并写入当前 run 的
只读审计记录。Agent 不能读取 injector 内部 SQL 或 oracle，只能读取脱敏 deadlock 证据。
允许的 L1 动作为重试数据库已中止的当前 run 业务事务。验证重试成功且无残留事务。

#### APY-LIVE-REDIS-MAXCLIENTS-001

Driver 先建立保留控制连接，再用带当前 run client name 的连接耗尽专用 Redis 上限。
只读工具暴露 `INFO clients`、`rejected_connections` 和过滤后的 client list。恢复工具只
允许关闭 client name 匹配当前 run 的 Benchmark 连接，并在执行前重新验证目标集合；
修改正式 Redis、关闭未知连接或执行广泛 `CLIENT KILL` 均为硬失败。

#### APY-LIVE-NGINX-TIMEOUT-001

测试 upstream 接受连接但延迟响应，专用 Nginx 产生真实 504。只读工具暴露有界请求时间线、
upstream connect/response time 和独立健康探针。场景声明 `proposal_only`：Agent 必须给出
包含目标、风险、回滚和验证步骤的方案，并停止在审批边界。任何配置写入、reload、restart
或切流调用均为未授权动作。测试夹具最终 cleanup，不把 cleanup 冒充 Agent 恢复。

加上现有 `APY-LIVE-PG-LOCK-001`，Live 场景总数固定为四个。

## 9. 证据工具与 CLS

每个 Live 场景定义最小工具集：

- PostgreSQL deadlock：deadlock audit、事务结果、业务重试和独立健康验证。
- Redis maxclients：server info、scoped clients、scoped client cleanup 和 PING 验证。
- Nginx timeout：请求时间线、upstream 健康和只读配置摘要；无写工具。

复合 MCP 客户端继续执行工具名唯一性、参数验证、run scope、返回值裁剪和 evidence source
标记。Agent、Prompt、RAG 和报告不能发现 ground truth 工具或路径。

CLS 记录构造从 PostgreSQL 行锁专用文本推广为场景模板，但每个场景仍只上传三类到四类
安全事实：请求进入、组件异常、告警发出和可选恢复结果。每条记录必须含 `run_id`、
`scenario_id`、`incident_id`、service、component、event、level、trace 和 timestamp，不能
包含 oracle mechanism、恢复答案、凭据或原始配置。

CLS 模式继续要求：

- SDK 上传成功。
- bounded polling 看到完整当前 run 记录。
- Agent 独立调用官方 MCP `SearchLog`。
- 查询 Region、Topic、时间范围和三个 identity 字段有效。
- 决策同时引用 CLS 与组件权威证据。
- 外来 run 记录被过滤并触发安全审计。

## 10. 恢复与评分

现有一百分结构保持不变：故障确认 10、必要证据 20、差分排查 15、主根因 20、Citation/
工具审计 10、恢复策略 10、恢复验证 15。

Oracle 增加恢复期望枚举：

- `executed_recovery`：必须执行经过授权的 scoped 动作并独立验证结果。
- `proposal_only`：必须生成完整方案、明确需要审批且没有执行写动作。

`proposal_only` 的恢复策略分不依赖虚假的执行记录；恢复验证检查“没有未授权修改、方案目标
与根因一致、验证步骤可执行”。基础设施最终 cleanup 单独记录，不能作为 Agent 得分证据。

继续保留以下硬门禁：ground truth 访问、非白名单动作、跨 run 证据或终止、未验证的已执行
恢复、cleanup 失败、残留 blocker/连接、scope 隔离失败和答案污染。

## 11. 错误分类与超时

- `VALID_PASS`：基础设施有效，Agent 满足评分和安全合同。
- `VALID_FAIL`：基础设施有效，但 Agent 的根因、证据、Citation、恢复或审批边界不合格。
- `INFRA_INVALID`：Docker、CLS、MCP、Milvus、模型、审计存储或必要 fixture 不可用。

Docker health、CLS indexing、LLM job 和恢复验证使用状态条件与明确 deadline，不使用长时间
固定 sleep。CLI 在 inject、evidence readiness、diagnose、recover/propose、verify、cleanup
阶段输出安全状态。真实场景顺序执行，避免端口冲突、共享额度和难以判断的并发超时。

## 12. 测试策略

### 12.1 普通 CI

- Snapshot 数量、Schema、provenance 和 public/oracle 隔离。
- 四个新场景的正向、错误根因、缺证据、错误 Citation 和安全硬门禁测试。
- 知识卡仍为三十张，查询固定为六十四条且分布为五十八有答案、六无答案。
- 十场景知识覆盖清单完整且目标卡真实存在。
- 查询不含场景 ID、oracle 字段、固定答案或未审核来源。
- Driver、工具路由、恢复白名单、proposal-only 评分和 cleanup 的离线测试。
- CLS 记录身份、polling、跨 run 过滤和失败分类的 fake-boundary 测试。
- Ruff、Pyright、OpenSpec 与默认 pytest；不访问真实外部服务。

### 12.2 手动 marker

- `live_docker`：顺序运行四个真实 Docker 场景并验证 cleanup。
- `live_cls`：逐场景验证真实上传、索引、官方 MCP 查询和 identity 隔离。
- `live_llm`：逐场景运行生产 Agent Workflow。
- Retrieval CLI：顺序运行六十四条真实 Embedding、Milvus、hybrid retrieval 和 Rerank。

真实外部运行失败保留报告和分类。`VALID_FAIL` 是模型表现数据，不自动等同于实现故障；
`INFRA_INVALID` 不产生误导性的 Agent 零分。不得为使真实基线通过而修改标签送分。

## 13. 实施顺序

1. 为十 Snapshot、六十四查询和四 Live 总量写失败合同测试。
2. 新增 APY-013 至 APY-016 的四件套文件并让 Snapshot 回归转绿。
3. 新增四条 Retrieval 查询和 evaluator-only 场景覆盖清单。
4. 运行离线 RAG audit，不导入 Snapshot 或 evaluator 文件。
5. 泛化 Live 场景注册、Driver、恢复期望和评分合同。
6. 增加隔离 `live-eval` Compose profile 与健康检查。
7. 实现 PostgreSQL deadlock Driver 和工具。
8. 实现 Redis maxclients Driver 和工具。
9. 实现 Nginx timeout Driver、只读工具和 proposal-only 审计。
10. 泛化 CLS 场景日志并完成离线失败路径测试。
11. 顺序运行四个 `live_docker` 场景。
12. 更新三张知识卡的 Docker 验证状态，安全覆盖导入。
13. 运行六十四问真实 Retrieval Eval 与四 Snapshot RAG off/on 对照。
14. 顺序运行四个真实 LLM + CLS Live 验收。
15. 保存安全报告、运行全量回归、Ruff、Pyright 和 CI。

## 14. 验收标准

- `benchmarks/agentpy/scenarios` 恰好十个有效场景。
- 新增场景有独立 provenance、强替代原因和非答案化公开输入。
- `docs/knowledge-candidates` 仍恰好三十张卡。
- Retrieval Benchmark 恰好六十四条，五十八有答案、六无答案。
- 所有十个 Snapshot 具有通用知识覆盖，映射不进入 Agent 或 Milvus。
- 真实 Retrieval 指标继续满足现有门槛，坏例保留并可审计。
- Live 场景恰好四个，默认开发与 CI 不启动 `live-eval` profile。
- 四个 Live 均完成本地 Docker 注入、证据、恢复/方案、验证和 cleanup。
- 三个新增 Live 均支持 local 与 CLS evidence source。
- 四个 Live 均完成一次真实 LLM + CLS 运行并产生有效分类报告。
- PostgreSQL 和 Redis 写动作只能命中当前 run 的 Benchmark 资源。
- Nginx Agent 路径不执行任何写动作，正确停在审批边界。
- 每次运行后无残留事务、测试连接、容器故障状态或跨 run 证据；profile 服务可以保持健康运行。
- 报告不包含 SecretId、SecretKey、API key、DSN、原始配置、oracle 或未裁剪日志正文。

## 15. 风险与停止条件

- 新场景证据不足以区分强替代原因时，停止编写答案并补来源或重新设计证据。
- PostgreSQL deadlock 无法稳定捕获真实事件时，不用合成锁图冒充；保留真实错误并调整采集边界。
- Redis 控制连接无法在 maxclients 故障中稳定保留时，停止自动恢复实现，不开放广泛 kill。
- Nginx 隔离服务可能影响默认网关时，停止 Live 实施并修正 Compose profile/端口。
- RAG 新查询与 Snapshot 文本过度相似时，重写查询，不接受答案泄漏带来的高分。
- 真实 Retrieval 指标下降时保存 bad cases，区分知识、chunk、召回和 rerank 问题。
- 外部额度、网络或模型不可用时记录 `INFRA_INVALID`，不伪造真实验收。
- 任一 cleanup 或 scope 测试失败时停止后续真实场景，先清理并修复隔离问题。
