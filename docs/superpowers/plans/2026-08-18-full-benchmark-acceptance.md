# AgentPy Full Benchmark Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在真实 DashScope、30 卡 RAG、Docker 和腾讯云 CLS 条件下，使 10 个 Snapshot 与 4 个 Live 场景全部达到正式通过口径，并保存每一次运行结果。

**Architecture:** 直接复用现有 Snapshot/Live runner 和 Evaluation Recorder，按持久化、依赖、Snapshot、Live 四级门禁顺序执行。每个场景首次只运行一次；失败后停止批次，使用已保存的安全失败分类完成局部 TDD 修复，再以新 Run 重跑，不覆盖历史。

**Tech Stack:** Python 3.10+、uv、pytest、Ruff、Pyright、LangGraph、LangChain OpenAI-compatible Qwen、PostgreSQL 16、Milvus、Redis 7、Docker Compose、腾讯云 CLS MCP。

## Global Constraints

- 主 Agent 固定为 `qwen3.7-max`，独立 Validator 固定为 `qwen3.8-max`。
- Snapshot 固定真实 `application` adapter、RAG on；Live 固定真实 Docker、RAG on、CLS evidence。
- 不修改 Ground Truth、Oracle、答案、评分权重、阈值、Validator 标准或恢复权限。
- 不打印、提交或持久化 API Key、云凭据、Prompt、模型原始响应、私有推理或原始 CLS 日志。
- 每个 Run 使用唯一 ID；`passed`、`failed`、`infra_invalid`、`interrupted` 全部保存。
- 不运行全量真实批次来收集一组不可归因失败；场景失败后立即停止当前批次。
- 不增加 Promptfoo、DeepEval、Chaos Mesh 或其他新依赖。

---

### Task 1: 建立隔离工作区并迁移本机安全配置

**Files:**
- Modify locally only: `config/user.project.json`（Git ignored，不提交）
- Reference: `config/user.project.template.json`
- Test: `apps/backend/tests/test_environment_examples.py`

**Interfaces:**
- Consumes: 当前本机 API Key、CLS 凭据、Embedding/Rerank 配置。
- Produces: `qwen3.7-max` 主模型、`qwen3.8-max` Validator、外部 Archive 路径。

- [ ] **Step 1: 在主仓库更新被忽略的本机配置且不触碰密钥字段**

将 `config/user.project.json` 的安全非密钥字段设为：

```json
{
  "evaluation": {
    "archiveDir": "D:\\桌面\\后端\\agent_py-evaluation-archive"
  },
  "llm": {
    "chatModel": "qwen3.7-max",
    "validatorModel": "qwen3.8-max",
    "modelCapabilities": {
      "qwen3.7-max": {
        "contextWindowTokens": 1000000,
        "structuredOutputMethod": "json_mode"
      },
      "qwen3.8-max": {
        "contextWindowTokens": 1000000,
        "structuredOutputMethod": "json_mode"
      }
    }
  }
}
```

保留现有 `apiKey`、Embedding、Rerank、CLS Secret、Region、Logset、Topic 与轮询配置。

- [ ] **Step 2: 使用 `using-git-worktrees` 创建 `feat/full-benchmark-acceptance` 隔离 worktree**

确认 feature branch 无未提交修改后，在主仓库执行 `git switch main`，再把该已有分支添加到
`.worktrees/full-benchmark-acceptance`。不得复用已删除或有残留的旧 worktree。隔离 worktree
不复制私有配置；所有正式命令显式使用以下主仓库配置：

```powershell
$sharedProjectConfig = 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\config\project.json'
```

验证 worktree 内 `config/project.json`、`config/user.project.json` 均不存在或仍被 Git ignore，
`git status --short` 不得出现任何本机配置。

- [ ] **Step 3: 运行配置合同测试**

Run from `apps/backend`:

```powershell
uv sync --frozen
$taskPytestTemp = Join-Path $env:TEMP ('agentpy-config-' + [guid]::NewGuid().ToString('N'))
uv run pytest tests/test_environment_examples.py tests/test_llm_provider.py -q -p no:cacheprovider --basetemp $taskPytestTemp
```

Expected: 全部通过；输出不包含 API Key 或 CLS Secret。

- [ ] **Step 4: 安全读取配置结果**

通过 `$sharedProjectConfig` 加载配置，只输出 `chatModel`、`validatorModel`、Archive 是否配置
和各凭据是否非空的布尔值。

Expected: 主模型 `qwen3.7-max`、Validator `qwen3.8-max`、Archive/LLM/CLS readiness 布尔值为真。

---

### Task 2: 通过持久化、数据库和 RAG readiness

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/knowledge_scope_audit.py`
- Create: `apps/backend/scripts/audit_knowledge_index_scope.py`
- Create: `apps/backend/tests/test_knowledge_scope_audit.py`
- Reference: `apps/backend/src/super_ai/evaluation/archive.py`
- Reference: `apps/backend/src/super_ai/evaluation/recording.py`
- Reference: `apps/backend/src/super_ai/evaluation/persistence.py`
- Reference: `apps/backend/scripts/manage_evaluation_history.py`
- Reference: `apps/backend/scripts/run_retrieval_benchmark.py`
- Test: `apps/backend/tests/test_evaluation_archive.py`
- Test: `apps/backend/tests/test_evaluation_recording.py`
- Test: `apps/backend/tests/test_evaluation_persistence.py`

**Interfaces:**
- Consumes: Task 1 配置、现有 PostgreSQL/Milvus 数据。
- Produces: 0 conflict/0 pending 的 Recorder、唯一 30 卡 owner/KB scope、可用 RAG。

- [ ] **Step 1: 检查 Compose 服务健康**

Run from repository root:

```powershell
docker compose -f infra/compose.yaml config
docker compose -f infra/compose.yaml --profile live-eval up -d postgres redis etcd minio milvus nginx live-eval-redis live-eval-upstream live-eval-nginx
docker compose -f infra/compose.yaml --profile live-eval ps
```

Expected: PostgreSQL、Redis、etcd、MinIO、Milvus、Nginx 以及三个隔离 `live-eval-*` 服务均为 running；定义了 healthcheck 的服务为 healthy。

- [ ] **Step 2: 升级 PostgreSQL schema 并确认 migration head**

Run from `apps/backend`:

```powershell
uv run alembic -x project_config=$sharedProjectConfig upgrade head
uv run alembic -x project_config=$sharedProjectConfig current
```

Expected: current revision 等于唯一 head；命令退出 0。

- [ ] **Step 3: 运行持久化专项回归**

```powershell
$taskPytestTemp = Join-Path $env:TEMP ('agentpy-history-' + [guid]::NewGuid().ToString('N'))
uv run pytest tests/test_evaluation_archive.py tests/test_evaluation_recording.py tests/test_evaluation_persistence.py tests/test_evaluation_history.py -q -p no:cacheprovider --basetemp $taskPytestTemp
```

Expected: 全部通过。

- [ ] **Step 4: 审计现有 Archive 并对账 PostgreSQL**

```powershell
uv run python scripts/manage_evaluation_history.py audit --config $sharedProjectConfig
uv run python scripts/manage_evaluation_history.py reconcile --config $sharedProjectConfig
uv run python scripts/manage_evaluation_history.py summarize --config $sharedProjectConfig
```

Expected: audit 退出 0；reconcile `conflicts=0`、`database_pending=0`；summary `conflicts=0`。

- [ ] **Step 5: 动态确定唯一 30 卡授权范围**

使用只读 SQL：

```powershell
$benchmarkScope = docker exec agent-py-postgres-1 psql -U agent_py -d agent_py -X -At -v ON_ERROR_STOP=1 -c "SELECT owner_user_id || '|' || knowledge_base_id FROM knowledge_documents WHERE status='ready' AND index_status='indexed' GROUP BY owner_user_id, knowledge_base_id HAVING COUNT(*)=30 ORDER BY owner_user_id;"
$scopeRows = @($benchmarkScope | Where-Object { $_ -match '^[^|]+\|[^|]+$' })
if ($scopeRows.Count -ne 1) { throw "Expected exactly one 30-card benchmark scope." }
$benchmarkOwnerId, $benchmarkKnowledgeBaseId = $scopeRows[0].Split('|', 2)
```

Expected: 恰好一组 owner/KB；值只保留在当前进程变量，不写入提交文档或日志汇总。

- [ ] **Step 6: 用 TDD 增加 PostgreSQL–Milvus scope audit**

在 `test_knowledge_scope_audit.py` 先覆盖：30 个 ready/indexed 文档且每个文档有 scoped Chunk
时通过；缺文档、缺 Chunk、orphan document ID、错误 owner/tenant/KB 或重复 active filename 时
分别失败。实现：

```python
@dataclass(frozen=True, slots=True)
class KnowledgeScopeAudit:
    document_count: int
    chunk_count: int
    missing_document_ids: tuple[str, ...]
    orphan_document_ids: tuple[str, ...]
    document_scope_mismatch_count: int
    chunk_scope_mismatch_count: int
    duplicate_filename_count: int
    passed: bool

def audit_knowledge_scope(
    *,
    documents: Sequence[KnowledgeDocumentRecord],
    chunks: Sequence[StoredVectorChunk],
    owner_user_id: str,
    knowledge_base_id: str,
) -> KnowledgeScopeAudit:
    ready_indexed = tuple(
        item
        for item in documents
        if item.status == "ready" and item.index_status == "indexed"
    )
    document_scope_mismatch_count = sum(
        1
        for item in ready_indexed
        if item.owner_user_id != owner_user_id
        or item.knowledge_base_id != knowledge_base_id
    )
    active = tuple(
        item
        for item in ready_indexed
        if item.owner_user_id == owner_user_id
        and item.knowledge_base_id == knowledge_base_id
    )
    expected_ids = {item.id for item in active}
    actual_ids = {item.document_id for item in chunks}
    chunk_scope_mismatch_count = sum(
        1
        for item in chunks
        if item.owner_user_id != owner_user_id
        or item.tenant_id != owner_user_id
        or item.knowledge_base_id != knowledge_base_id
    )
    filenames = [item.filename for item in active]
    duplicate_filename_count = len(filenames) - len(set(filenames))
    missing = tuple(sorted(expected_ids - actual_ids))
    orphan = tuple(sorted(actual_ids - expected_ids))
    return KnowledgeScopeAudit(
        document_count=len(active),
        chunk_count=len(chunks),
        missing_document_ids=missing,
        orphan_document_ids=orphan,
        document_scope_mismatch_count=document_scope_mismatch_count,
        chunk_scope_mismatch_count=chunk_scope_mismatch_count,
        duplicate_filename_count=duplicate_filename_count,
        passed=(
            len(active) == 30
            and not missing
            and not orphan
            and document_scope_mismatch_count == 0
            and chunk_scope_mismatch_count == 0
            and duplicate_filename_count == 0
        ),
    )
```

CLI 使用 `KnowledgeDocumentRepository.list_documents()` 和
`MilvusVectorStore.list_chunks(tenant_id=owner_user_id, knowledge_base_ids=(knowledge_base_id,))`，
只输出计数、布尔值和缺失/orphan ID，不输出 Chunk 正文。目标测试 RED 后实现最小代码，再运行：

```powershell
uv run pytest tests/test_knowledge_scope_audit.py tests/test_milvus_vector_store.py -q -p no:cacheprovider
uv run ruff check src/super_ai/evaluation/knowledge_scope_audit.py scripts/audit_knowledge_index_scope.py tests/test_knowledge_scope_audit.py
uv run pyright
```

- [ ] **Step 7: 验证 30 文档和 Milvus scoped Chunk**

```powershell
uv run python scripts/audit_knowledge_index_scope.py --owner-user-id $benchmarkOwnerId --knowledge-base-id $benchmarkKnowledgeBaseId --config $sharedProjectConfig
```

Expected: document count 30、missing/orphan/scope mismatch 均为 0、exit 0。

- [ ] **Step 8: 运行 64-query Retrieval 正式基线**

复用现有 Retrieval runner 对 64-query 数据集执行一次正式基线；它会验证 owner、tenant、KB、document 和 citation scope：

```powershell
uv run python scripts/run_retrieval_benchmark.py --owner-user-id $benchmarkOwnerId --knowledge-base-id $benchmarkKnowledgeBaseId --queries ../../benchmarks/agentpy/retrieval/queries.yaml --config $sharedProjectConfig --output var/benchmarks/full-acceptance-retrieval.json
```

Expected: exit 0，Recall/Citation 门禁通过，结果保存到 Archive、PostgreSQL 和 ignored output。

---

### Task 3: 增加正式 campaign 与 Live cleanup 持久化合同

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Modify: `apps/backend/scripts/run_snapshot_benchmark.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Test: `apps/backend/tests/test_snapshot_benchmark_cli.py`
- Test: `apps/backend/tests/test_live_benchmark_cli.py`

**Interfaces:**
- Produces: Snapshot/Live `--campaign-id`；metadata `acceptanceCampaignId`；所有 Live 终态的 `cleanupSucceeded`。

- [ ] **Step 1: 写 campaign metadata RED 测试**

断言 Snapshot 和 Live parser 接受 `--campaign-id full-acceptance-20260818`，运行 Envelope metadata
包含 `acceptanceCampaignId`；空值、路径字符和超长值被 `validate_run_id` 拒绝；历史未提供参数
的调用保持兼容。

- [ ] **Step 2: 实现 campaign ID**

在 Snapshot/Live parser 增加可选 `--campaign-id`，通过现有 `validate_run_id` 校验后写入
metadata；`history.py` 的 Snapshot/Live metadata allowlist 增加
`acceptanceCampaignId`。不把 campaign 写进 Agent input、Prompt 或 Oracle。

- [ ] **Step 3: 写 Live 失败与中断 cleanup RED 测试**

覆盖：有效失败后 cleanup 成功、CLS infra failure 后 cleanup 成功、cleanup 自身失败、
`asyncio.CancelledError` 后 cleanup 成功。断言 Archive/PostgreSQL terminal Envelope 的
`metrics.cleanupSucceeded` 分别为 true/false，原 failure category 和取消语义保持不变。

- [ ] **Step 4: 实现 cleanup 结果传递**

`LiveBenchmarkError` 增加 `cleanup_succeeded: bool | None`。`LiveBenchmarkRunner.run()` 的
`finally` 在 re-raise 前把 scoped cleanup 结果附加到安全异常；取消异常只附加布尔审计属性并
继续按 `CancelledError` 传播。`_run_live_once()` 在 passed、failed、infra_invalid、interrupted
终态都把已知的 `cleanupSucceeded` 写入允许列表 metrics；未知时省略，不伪造 true。

- [ ] **Step 5: 运行专项回归**

```powershell
uv run pytest tests/test_snapshot_benchmark_cli.py tests/test_live_benchmark_cli.py tests/test_evaluation_history.py -q -p no:cacheprovider
uv run ruff check src/super_ai/evaluation/history.py src/super_ai/evaluation/live/runner.py src/super_ai/evaluation/live/cli.py scripts/run_snapshot_benchmark.py tests/test_snapshot_benchmark_cli.py tests/test_live_benchmark_cli.py
uv run pyright
```

- [ ] **Step 6: 固定本轮 campaign**

```powershell
$acceptanceCampaignId = 'full-acceptance-20260818'
```

后续每个正式 Snapshot/Live Run 都必须传入该值；最终只按该 metadata 精确汇总。

---

### Task 4: 通过真实模型与 CLS readiness

**Files:**
- Reference: `apps/backend/tests/test_live_llm.py`
- Reference: `apps/backend/src/super_ai/evaluation/live/cls_evidence.py`
- Reference: `apps/backend/src/super_ai/evaluation/live/evidence_client.py`
- Test: `apps/backend/tests/test_live_cls_evidence.py`

**Interfaces:**
- Consumes: Task 1 模型配置和 Task 2 基础设施。
- Produces: 可调用的 Chat/Validator/Embedding/Rerank 与 run-scoped CLS。

- [ ] **Step 1: 运行离线 provider 与 CLS 合同测试**

```powershell
$taskPytestTemp = Join-Path $env:TEMP ('agentpy-provider-' + [guid]::NewGuid().ToString('N'))
uv run pytest tests/test_llm_provider.py tests/test_live_cls_evidence.py tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider --basetemp $taskPytestTemp
```

Expected: 全部通过。

- [ ] **Step 2: 运行一次真实模型 readiness**

```powershell
uv run pytest -m live_llm tests/test_live_llm.py -q -p no:cacheprovider
```

Expected: Chat、独立 Validator、Embedding 和 Rerank 对应 readiness 全部通过；任何 provider/network 错误先分类为基础设施问题，不进入 Benchmark。

- [ ] **Step 3: 检查 CLS MCP readiness**

复用现有本机 CLS MCP 启动与轮询流程，只输出 HTTP 状态、tool 名称和安全错误分类。必须确认恰好一个 `SearchLog`，且查询参数会被当前 run scope 覆盖。

Expected: MCP ready；不输出 Secret 或原始日志。

---

### Task 5: 顺序完成 10 个 Snapshot 正式验收

**Files:**
- Reference: `apps/backend/scripts/run_snapshot_benchmark.py`
- Reference: `benchmarks/agentpy/scenarios/*`
- Modify only after a proven defect: affected `apps/backend/src/super_ai/**`、active OpenSpec/doc files。
- Test: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Test: `apps/backend/tests/test_evaluation_scoring.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Test: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Consumes: Gate 0–1 readiness、`$benchmarkOwnerId`、`$benchmarkKnowledgeBaseId`。
- Produces: campaign 下 10 个目标 Snapshot Run 均 passed。

- [ ] **Step 1: 固定场景顺序和运行命令**

场景顺序：

```text
APY-002 APY-003 APY-006 APY-007 APY-011 APY-012 APY-013 APY-014 APY-015 APY-016
```

每个场景单独执行：

```powershell
uv run python scripts/run_snapshot_benchmark.py --scenario $scenarioId --suite-version v1 --runs 1 --adapter application --rag-mode on --owner-user-id $benchmarkOwnerId --knowledge-base-id $benchmarkKnowledgeBaseId --config $sharedProjectConfig --campaign-id $acceptanceCampaignId --output ("var/benchmarks/full-acceptance-{0}.json" -f $scenarioId)
```

Expected: exit 0，唯一新 Run 为 `validity=valid`、`passed=true`、无 hard gate，Archive 与 PostgreSQL 均存在。

- [ ] **Step 2: 对每个成功场景执行持久化检查后再进入下一个**

安全读取刚生成的 Run ID，并分别从 Archive 与 `aiops_evaluation_runs` 查询状态和 checksum。

Expected: 两侧 Run ID/status/checksum 一致；不读取 result payload 中的隐藏数据。

- [ ] **Step 3: 首个失败立即停止并进入 TDD 修复回路**

按 `infra_invalid`、hard gate、Agent score failure 三类处理：

1. `infra_invalid`：修复配置/外部依赖，不修改评分或 Agent；
2. hard gate：修复隔离、工具权限、跨 run、恢复或 cleanup，先增加对应失败测试；
3. 有效低分：用 ScoreReason、Decision/Validator allowlist error、工具审计和 Evidence Evaluation 定位最小 seam，先运行或增加失败测试，再做单一修复。

每次修复只运行目标 pytest、Ruff、Pyright 和对应 OpenSpec strict；通过后创建新 Run，旧失败 Run 保留。三次不成立的修复假设后停止并重新审视架构，不继续叠加补丁。

- [ ] **Step 4: Snapshot 阶段汇总**

运行 `audit`、`reconcile`、`summarize`，确认 campaign 下 10 个场景目标 Run 全部通过且 0 conflict/0 pending。

---

### Task 6: 顺序完成 4 个 Live Docker + LLM + CLS 正式验收

**Files:**
- Reference: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Reference: `apps/backend/src/super_ai/evaluation/live/*`
- Reference: `benchmarks/agentpy/live/*`
- Reference: `infra/live-eval/*`
- Modify only after a proven defect: affected Live driver/adapter/scorer/recovery module、active OpenSpec/doc files。
- Test: `apps/backend/tests/test_live_benchmark_cli.py`
- Test: `apps/backend/tests/test_live_diagnostic_adapter.py`
- Test: `apps/backend/tests/test_live_evaluation_scoring.py`
- Test: `apps/backend/tests/test_live_semantic_scoring.py`

**Interfaces:**
- Consumes: Snapshot Gate 通过、CLS ready、Docker healthy、授权 owner/KB。
- Produces: campaign 下 4 个目标 Live Run 均 passed，cleanup 均通过。

- [ ] **Step 1: 固定 Live 顺序**

```text
APY-LIVE-PG-LOCK-001
APY-LIVE-PG-DEADLOCK-001
APY-LIVE-REDIS-MAXCLIENTS-001
APY-LIVE-NGINX-TIMEOUT-001
```

- [ ] **Step 2: 按已知旧 Run 做 scoped cleanup，再执行全局只读残留审计**

```powershell
$archiveRoot = 'D:\桌面\后端\agent_py-evaluation-archive\live'
$knownLiveRuns = Get-ChildItem -Recurse -File -LiteralPath $archiveRoot -Filter '*.json' | ForEach-Object {
  $item = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
  [pscustomobject]@{ Scenario = $item.scenarioId; RunId = $item.runId }
} | Sort-Object Scenario,RunId -Unique
foreach ($known in $knownLiveRuns) {
  uv run python -m super_ai.evaluation.live.cli cleanup --scenario $known.Scenario --run-id $known.RunId
}

$postgresResidue = docker exec agent-py-postgres-1 psql -U agent_py -d agent_py_live_eval -X -At -v ON_ERROR_STOP=1 -c "SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname='agent_py_live_eval' AND application_name LIKE 'agentpy-live:%') || '|' || (SELECT count(*) FROM pg_tables WHERE schemaname='live_eval');"
$liveRedisContainer = docker compose -f ../../infra/compose.yaml --profile live-eval ps -q live-eval-redis
$redisClientList = docker exec $liveRedisContainer redis-cli --raw CLIENT LIST
$redisResidue = @($redisClientList | Where-Object { $_ -match 'name=agentpy-live:' }).Count
git diff --exit-code -- ../../infra/live-eval/nginx.conf
if ($postgresResidue.Trim() -ne '0|0' -or $redisResidue -ne 0) { throw "Live residue audit failed closed." }
```

Expected: 所有已知 cleanup 通过；PostgreSQL 输出 `0|0`、Redis count 为 0、Nginx 配置无变化。
任何无法映射到已知 Run 的 orphan 只报告并阻塞正式运行，不扩大删除范围。

- [ ] **Step 3: 只执行一次完整 CLS Live Run**

```powershell
$liveRunId = ('accept-{0}-{1}' -f $scenarioId.ToLowerInvariant().Replace('_','-'), [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
uv run python -m super_ai.evaluation.live.cli run --scenario $scenarioId --run-id $liveRunId --owner-user-id $benchmarkOwnerId --knowledge-base-id $benchmarkKnowledgeBaseId --config $sharedProjectConfig --campaign-id $acceptanceCampaignId --evidence-source cls
```

Expected: exit 0，`validity=valid`、`passed=true`、无 hard gate；fault confirmation、required evidence、differential diagnosis、root cause、citation/tool audit、recovery 和 verification 达标。

- [ ] **Step 4: 独立 verify 与 cleanup**

```powershell
uv run python -m super_ai.evaluation.live.cli verify --scenario $scenarioId --run-id $liveRunId
uv run python -m super_ai.evaluation.live.cli cleanup --scenario $scenarioId --run-id $liveRunId
```

Expected: 原正式 Run Envelope 已记录 `cleanupSucceeded=true`；verify 与补充 cleanup 均退出 0。
即使 Step 3 失败或中断也执行补充 scoped cleanup，但不得覆盖原终态。

- [ ] **Step 5: 首个 Live 失败立即停止并进入 TDD 修复回路**

首先确认 failure stage 是 baseline、inject、confirm、CLS index/query、diagnose、recover、verify、evaluate 还是 cleanup；只修改被证据证明的层。不得把 CLS 缺失记为 Agent 低分，不得把 Nginx `proposal_only` 改成自动写入，不得扩大 PostgreSQL/Redis 恢复范围。

修复后运行对应 driver/adapter/scoring/recovery 目标测试、Ruff、Pyright，再以新 Run 重跑；失败历史保留。

- [ ] **Step 6: Live 阶段汇总**

确认 campaign 下四个场景目标 Run 全部通过，四次内部 cleanup 成功，Archive/PostgreSQL 0 conflict/0 pending。

---

### Task 7: 最终回归、文档和分支交付

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `docs/superpowers/specs/2026-08-18-full-benchmark-acceptance-design.md` only if implementation exposed a documented design correction
- Modify: affected OpenSpec active change/tasks/spec only when behavior changed
- Test: all target suites accumulated during Tasks 1–5

**Interfaces:**
- Consumes: 14 个通过 Run、完整失败历史、修复提交。
- Produces: 可复核的最终验收表和可合并分支。

- [ ] **Step 1: 写最终安全验收表**

为 10 个 Snapshot 和 4 个 Live 记录：Run ID、Git SHA、模型名、总分/维度、有效性、passed、hard gate、Validator origin/error category、恢复模式、execution permitted、verification 和 cleanup。不得复制 Prompt、原始响应、Ground Truth 或原始 CLS 日志。

- [ ] **Step 2: 最终对账**

```powershell
uv run python scripts/manage_evaluation_history.py audit --config $sharedProjectConfig
uv run python scripts/manage_evaluation_history.py reconcile --config $sharedProjectConfig
uv run python scripts/manage_evaluation_history.py summarize --config $sharedProjectConfig
```

Expected: 0 conflict、0 database pending；按 `acceptanceCampaignId=full-acceptance-20260818`
精确查询出的 14 个目标场景 Run 全部通过。

- [ ] **Step 3: 运行风险相称的最终回归**

运行所有本轮实际修改模块对应的 pytest 文件，以及：

```powershell
uv run ruff check .
uv run pyright
```

若本轮改变 OpenSpec 行为，运行 `openspec validate --all`；不以全量真实 Benchmark 重跑替代离线回归。

- [ ] **Step 4: 提交、推送并创建 PR**

只提交代码、测试、模板/规格和安全结果文档；不提交 `config/user.project.json`、Archive、数据库、`var/` 报告或凭据。等待 GitHub Actions 全部通过后再交付合并。
