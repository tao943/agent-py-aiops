# Conversation 三层 Eval 与 Chat→AIOps Live 验收记录

日期：2026-08-22  
分支：`feat/conversation-aiops-copilot`  
验收起点 Git SHA：`24dd19971539c05f8be7bed55c068d42a1527284`

## 已交付切片

| 切片 | Commit | 结果 |
| --- | --- | --- |
| Offline Conversation Eval 硬化 | `0aff9c0` | 12 场景与八类不可补偿安全硬门 |
| Conversation Model Eval 与持久化 | `402cf59` | 六个 fake-provider 场景、Artifact v2、v1 读取兼容 |
| Chat Live 入口 Adapter | `24dd199` | Pending Action 确认、幂等任务复用、报告/证据身份校验 |
| 手动 Chat Live CLI 与文档 | 当前验收提交 | 双显式授权、场景 ID 校验、结果保存和退出码合同 |

## 架构边界

```text
Live fault / scoped CLS preparation
  → Chat Pending Action
  → human confirmation
  → evaluation-only durable worker
  → ApplicationLiveDiagnosticAdapter
  → existing AIOps CLS / RAG / LangGraph / Recovery / Scorer
```

- Conversation Agent 不获得 CLS Tool，也不复制 Live scenario、Ground Truth 或 AIOps scorer。
- Chat 负责路由、确认、任务复用和公开报告读取；AIOps 负责证据采集、诊断、恢复和评分。
- `conversationMetrics` 与 AIOps metrics 并列保存，不能改变 AIOps `total`、`rawTotal` 或 pass/fail。
- 结果先写 Evaluation Archive，再幂等同步 PostgreSQL；数据库待同步时退出码为 `2`。
- Artifact 禁止保存 Prompt、私有推理、原始模型响应、Ground Truth、Oracle 或原始 CLS 日志。

## 验证证据

在 `apps/backend` 执行：

```powershell
uv run pytest tests/test_conversation_eval.py tests/test_conversation_model_eval.py tests/test_chat_live_entry_adapter.py tests/test_chat_aiops_live_cli.py tests/test_live_benchmark_cli.py tests/test_evaluation_persistence.py -q
```

结果：退出码 `0`，聚焦集合全部通过。

```powershell
uv run ruff check src/super_ai/chat src/super_ai/evaluation tests/test_conversation*.py tests/test_chat_live_entry_adapter.py tests/test_chat_aiops_live_cli.py
```

结果：`All checks passed!`

```powershell
uv run pyright
```

结果：`0 errors, 0 warnings, 0 informations`。

额外 CLI/Adapter 聚焦集合：

```powershell
uv run pytest tests/test_chat_aiops_live_cli.py tests/test_chat_live_entry_adapter.py tests/test_live_benchmark_cli.py -q
```

结果：47 个测试通过。

## 未运行项

本次实施没有运行真实 Conversation Model Eval、真实 LLM、腾讯云 CLS 或 Docker Live，也没有
消耗模型额度。以下命令必须由用户再次明确批准真实额度和外部资源后执行：

```powershell
uv run python scripts/run_conversation_model_eval.py --confirm-real-model
uv run python scripts/run_chat_aiops_live_eval.py --scenario APY-LIVE-PG-LOCK-001 --owner-user-id <owner-id> --knowledge-base-id <kb-id> --confirm-real-model --confirm-live-cls
```

未运行全量 pytest；验收范围严格限于本计划指定的聚焦测试。
