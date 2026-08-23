# Conversation Agent 与自动报警闭环集成验收

日期：2026-08-23

## 集成基线

- 自动报警/自动闭环：`main` `efb0fe2199d171231d4f7566f7d6bede5e542be0`
- Conversation Agent / Adaptive Query Rewrite：`c5a3aff`
- 集成分支：`integration/conversation-auto-alert`
- 未新增依赖或外部服务。

## 架构结果

- Direct Live 保持原 AIOps 诊断入口。
- Chat Live 继续由 `ChatEntryLiveDiagnosticAdapter` 进入同一个诊断 runtime，并保留
  `conversationMetrics`。
- Auto-Closure 保持 Direct AIOps Single-Agent，不经过 Conversation Query Rewrite。
- `chat + auto-closure` 被明确拒绝为参数无效，且在 repository、RAG、Chat adapter 或
  Auto-Closure runner 创建前结束。
- 告警 SQLAlchemy repository 同时保留 owner-scoped active incident 查询/调度，以及
  scenario/run correlation、lifecycle lookup、independent verification 和报告关联。
- Evaluation artifact v2 同时保留 Conversation、Retrieval Rewrite 与 Auto-Closure 指标，
  并继续读取 legacy v1。

## Alembic Revision 映射

自动闭环拥有并保持不变：

```text
202608220001 alert ingestion
→ 202608220002 live alert verification
→ 202608220003 live auto closure state
```

Conversation revision 在集成链中重排为：

```text
历史 202608220002 chat_agent_runs          → 集成 202608220004
历史 202608220003 pending_chat_actions     → 集成 202608220005
历史 202608220004 structured_chat_memory   → 集成 202608220006
历史 202608220005 memory_compaction_status → 集成 202608220007
```

验证结果：`alembic heads` 只有 `202608220007 (head)`；全新 PostgreSQL 数据库可升级到
007；停在新版 main 003 的数据库可升级到 007。

当前默认测试配置连接的数据库在集成前报告旧 Conversation revision `202608220005`。
为避免 Alembic 把旧编号误认成新 revision，本次未 stamp、未升级、未重建该数据库。
旧 Conversation 002–005 数据库如需保留数据升级，必须另行设计并测试数据迁移桥接。

## 验证结果

- 冲突边界、Evaluation history、Alert service 聚焦测试：通过。
- PostgreSQL migration、alert ingestion、verification schema、API/app 组合测试：通过。
- Auto-Closure 与 Conversation/Query Rewrite 聚焦回归：通过。
- Ruff：通过。
- 自动闭环 CI 修复 `c4f1424` 合入后，完整 Pyright：0 errors / 0 warnings。
- API contracts：typecheck 通过；27/27 tests 通过。
- Frontend：typecheck 与 production build 通过；25/25 files、88/88 tests 通过。
- Docker Compose：`docker compose -f infra/compose.yaml config --quiet` 通过。
- 未运行真实 LLM、CLS 或 Docker fault-injection Live acceptance。

## 自动闭环 CI 修复集成

自动闭环的 CI 修复在本轮完成期间正式提交为 `c4f1424`。集成分支通过一次独立 merge
保留该提交历史；唯一重叠点是 Live CLI 的 stage timing 类型收窄，语义合并后仍保留
Conversation 入口、自动闭环 orchestration 和完整静态类型约束。
