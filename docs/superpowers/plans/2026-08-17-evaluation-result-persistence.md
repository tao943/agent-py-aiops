# Evaluation Result Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 汇总所有仍可恢复的 Snapshot、Retrieval、Live/CLS 测评结果，并让三类后续运行的成功和失败结果自动双写 PostgreSQL 与 worktree 外本地归档。

**Architecture:** 以版本化 `EvaluationRunEnvelope` 作为三类测评的统一安全合同，`EvaluationArchive` 先原子写入本地 Artifact，现有 `EvaluationRepository` 再幂等写入 PostgreSQL。三个 CLI 通过 `EvaluationRunRecorder` 共享生命周期；历史导入器只读取显式来源，通过 `run_id + checksum` 去重，并从归档与数据库生成可重建的 `index.jsonl` 和 `summary.md`。

**Tech Stack:** Python 3.10、dataclasses、async SQLAlchemy 2、PostgreSQL JSONB、Alembic、pytest、Ruff、strict Pyright、OpenSpec CLI。

## Global Constraints

- 只从 `config/project.json` 与 `config/user.project.json` 读取项目配置，不读取 `.env` 或环境变量。
- 归档目录必须是位于当前 Git worktree 外的绝对路径；运行产物不得提交 GitHub。
- 不引入 MLflow、Langfuse、Phoenix、W&B、新外部服务或第三方依赖。
- 不保存密钥、完整 Prompt、原始模型响应、私有推理、Ground Truth、oracle、隐藏评分答案或未脱敏日志。
- 不改变 Benchmark 场景、RAG 知识卡、评分权重、通过阈值、Agent Workflow 或恢复授权策略。
- 保持 Python 3.10、PostgreSQL-only、现有异步 SQLAlchemy、Ruff 与 strict Pyright。
- 所有新行为先进入聚焦 OpenSpec change，并通过 wiki-sync 同步 WIKI。
- 使用 TDD；每个任务先验证测试按预期失败，再实现最小代码并提交。

---

## File Map

**OpenSpec and documentation**

- Create: `openspec/changes/persist-evaluation-results/proposal.md` — 变更动机、范围和影响。
- Create: `openspec/changes/persist-evaluation-results/design.md` — 双写、状态机、安全和恢复决策。
- Create: `openspec/changes/persist-evaluation-results/tasks.md` — 实现检查表。
- Create: `openspec/changes/persist-evaluation-results/specs/evaluation-result-history/spec.md` — 可验证需求。
- Generate: `docs/changes/active/persist-evaluation-results/index.md` — wiki-sync 页面。
- Modify: `docs/changes/index.md`, `docs/.vitepress/config.mts` — wiki-sync 索引。
- Modify: `apps/backend/README.md` — 自动归档、汇总和对账命令。

**Configuration and persistence domain**

- Modify: `config/project.template.json`, `config/user.project.template.json` — 添加空的 `evaluation.archiveDir` 示例。
- Create: `apps/backend/src/super_ai/evaluation/history.py` — envelope、状态、checksum、安全字段合同。
- Create: `apps/backend/src/super_ai/evaluation/archive.py` — 配置解析、路径保护、原子 Artifact 存储。
- Create: `apps/backend/src/super_ai/evaluation/recording.py` — Archive + PostgreSQL 生命周期协调。
- Test: `apps/backend/tests/test_evaluation_history.py`, `test_evaluation_archive.py`, `test_evaluation_recording.py`。

**PostgreSQL**

- Create: `apps/backend/alembic/versions/202608170001_generalize_evaluation_runs.py`。
- Modify: `apps/backend/src/super_ai/memory/models.py`, `repositories.py`, `sqlalchemy.py`。
- Modify: `apps/backend/src/super_ai/evaluation/persistence.py`。
- Modify: `apps/backend/tests/test_evaluation_persistence.py`, `test_postgresql_migrations.py`。

**CLI integration and history tools**

- Modify: `apps/backend/scripts/run_snapshot_benchmark.py`。
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`。
- Modify: `apps/backend/scripts/run_retrieval_benchmark.py`。
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`。
- Create: `apps/backend/scripts/manage_evaluation_history.py`。
- Create: `apps/backend/src/super_ai/evaluation/history_import.py`。
- Create: `apps/backend/src/super_ai/evaluation/summary.py`。
- Modify/Test: adjacent Snapshot, Retrieval and Live CLI test files; create `test_evaluation_history_import.py` and `test_evaluation_summary.py`。

---

### Task 1: OpenSpec Change and Wiki Contract

**Files:**
- Create: `openspec/changes/persist-evaluation-results/proposal.md`
- Create: `openspec/changes/persist-evaluation-results/design.md`
- Create: `openspec/changes/persist-evaluation-results/tasks.md`
- Create: `openspec/changes/persist-evaluation-results/specs/evaluation-result-history/spec.md`
- Generate: `docs/changes/active/persist-evaluation-results/index.md`
- Modify: `docs/changes/index.md`
- Modify: `docs/.vitepress/config.mts`

**Interfaces:**
- Consumes: approved design at `docs/superpowers/specs/2026-08-17-evaluation-result-persistence-design.md`.
- Produces: capability `evaluation-result-history` with requirements for automatic persistence, answer isolation, reconciliation and historical summary.

- [ ] **Step 1: Create the OpenSpec artifacts with explicit scenarios**

The delta spec must contain these requirements and scenarios:

```markdown
## ADDED Requirements

### Requirement: Automatic evaluation run persistence
系统 SHALL 在 Snapshot、Retrieval、Live 和 CLS 正式测评开始前建立运行记录，并在通过、评分失败、Agent 失败、基础设施失败或可捕获中断后保存终态。

#### Scenario: Retrieval threshold failure is retained
- **WHEN** Retrieval 测评完成但任一批准阈值未达标
- **THEN** 系统保存完整安全指标、`status=failed` 和退出码 1

#### Scenario: CLS infrastructure timeout is retained
- **WHEN** CLS 日志索引轮询超时
- **THEN** 系统保存 `status=infra_invalid`、允许列表内的失败分类和退出码 2

### Requirement: Local archive and PostgreSQL reconciliation
系统 SHALL 通过稳定 run ID 和内容 checksum 对账 PostgreSQL 与 worktree 外本地归档，且不得静默覆盖身份冲突。

#### Scenario: PostgreSQL is temporarily unavailable
- **WHEN** 安全 Artifact 已写入而 PostgreSQL 写入失败
- **THEN** 后续 reconcile 能幂等补写数据库且保持原始运行时间和结果

### Requirement: Evaluation answer isolation
系统 SHALL 拒绝把 Ground Truth、oracle、Prompt、私有推理、凭据或未脱敏日志写入运行归档。

#### Scenario: Historical file contains forbidden fields
- **WHEN** 导入器发现递归字段名命中禁止列表
- **THEN** 文件被报告为 rejected 且不进入共享归档或 PostgreSQL

### Requirement: Recoverable historical summary
系统 SHALL 汇总所有可证明的历史运行，并明确标记 reconstructed、conflict、database pending 和不可恢复边界。

#### Scenario: Live task has audit but no score artifact
- **WHEN** 数据库存在 Live 诊断审计但没有独立结果
- **THEN** 汇总仅生成 `provenance=reconstructed` 记录且不补造评分指标
```

- [ ] **Step 2: Validate the focused change and confirm it initially passes structurally**

Run:

```powershell
openspec validate persist-evaluation-results --strict
```

Expected: `persist-evaluation-results` validates successfully.

- [ ] **Step 3: Run repository wiki-sync for the active change**

Run:

```powershell
python .codex/skills/wiki-sync/scripts/sync_wiki.py active persist-evaluation-results
npm run docs:build
```

Expected: active page, change index and sidebar are regenerated; VitePress build exits 0.

- [ ] **Step 4: Commit the specification slice**

```powershell
git add openspec/changes/persist-evaluation-results docs/changes/active/persist-evaluation-results docs/changes/index.md docs/.vitepress/config.mts
git commit -m "spec: define evaluation result history"
```

---

### Task 2: Safe Envelope and Worktree-External Archive

**Files:**
- Modify: `config/project.template.json`
- Modify: `config/user.project.template.json`
- Create: `apps/backend/src/super_ai/evaluation/history.py`
- Create: `apps/backend/src/super_ai/evaluation/archive.py`
- Create: `apps/backend/tests/test_evaluation_history.py`
- Create: `apps/backend/tests/test_evaluation_archive.py`

**Interfaces:**
- Consumes: `load_project_config(config_path)` and Python filesystem primitives.
- Produces: `EvaluationRunEnvelope`, `EvaluationStatus`, `EvaluationKind`, `running_envelope()`, `running_from_terminal()`, `terminal_envelope()`, `interrupted_envelope()`, `artifact_checksum()`, `EvaluationArchive.from_config()`, `start()`, `finalize()`, `load()` and `iter_envelopes()`.

- [ ] **Step 1: Write failing envelope and security tests**

Add tests that construct the public contract and assert canonical serialization:

```python
def test_terminal_envelope_checksum_is_stable() -> None:
    running = running_envelope(
        run_id="eval-1",
        evaluation_kind="retrieval",
        scenario_id="retrieval-64",
        suite_version="v1",
        metadata={"gitSha": "abc123"},
        created_at=FIXED_TIME,
        started_at=FIXED_TIME,
    )
    envelope = terminal_envelope(
        running=running,
        status="failed",
        validity="VALID_FAIL",
        passed=False,
        metrics={"recallAt1": 0.75},
        result_payload={"failures": ["recall_at_1_below_threshold"]},
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=FIXED_TIME,
    )
    assert artifact_checksum(envelope) == artifact_checksum(envelope)
    assert envelope.to_json()["artifactSchemaVersion"] == "v1"


@pytest.mark.parametrize(
    "key",
    [
        "apiKey", "secret_key", "password", "token", "oracle",
        "ground_truth", "groundTruth", "primary_cause", "primaryCause",
    ],
)
@pytest.mark.parametrize("container", ["metadata", "metrics", "result_payload"])
def test_envelope_rejects_forbidden_recursive_keys(key: str, container: str) -> None:
    values = {"metadata": {}, "metrics": {}, "result_payload": {}}
    values[container] = {"nested": {key: "must-not-persist"}}
    with pytest.raises(ValueError, match="forbidden"):
        terminal_envelope(
            running=running_envelope(
                run_id="eval-1",
                evaluation_kind="snapshot",
                scenario_id="APY-013",
                suite_version="v1",
                metadata=values["metadata"],
                created_at=FIXED_TIME,
                started_at=FIXED_TIME,
            ),
            status="failed",
            validity="VALID_FAIL",
            passed=False,
            metrics=values["metrics"],
            result_payload=values["result_payload"],
            diagnostic_task_id=None,
            failure_category=None,
            completed_at=FIXED_TIME,
        )
```

- [ ] **Step 2: Run the envelope tests and verify they fail**

Run: `uv run pytest tests/test_evaluation_history.py -q`

Expected: collection fails because `super_ai.evaluation.history` does not exist.

- [ ] **Step 3: Implement the immutable domain contract**

Define exact public types in `history.py`:

```python
EvaluationKind = Literal["snapshot", "retrieval", "live"]
EvaluationStatus = Literal[
    "running", "passed", "failed", "agent_failed", "infra_invalid", "interrupted"
]
EvaluationProvenance = Literal["native", "imported", "reconstructed"]

@dataclass(frozen=True, slots=True)
class EvaluationRunEnvelope:
    artifact_schema_version: str
    run_id: str
    evaluation_kind: EvaluationKind
    scenario_id: str
    suite_version: str
    status: EvaluationStatus
    validity: str | None
    passed: bool | None
    metrics: JsonDict
    result_payload: JsonDict
    metadata: JsonDict
    provenance: EvaluationProvenance
    diagnostic_task_id: str | None
    failure_category: str | None
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None

```

Add exact constructors `running_envelope(*, run_id, evaluation_kind, scenario_id, suite_version, metadata, created_at, started_at) -> EvaluationRunEnvelope` and `terminal_envelope(*, running, status, validity, passed, metrics, result_payload, diagnostic_task_id, failure_category, completed_at) -> EvaluationRunEnvelope`. Add `EvaluationRunEnvelope.to_json() -> JsonDict` and `EvaluationRunEnvelope.from_json(payload) -> EvaluationRunEnvelope`. Enforce separate allowlists for metadata, metrics and result payload by evaluation kind. Normalize every recursive key with `re.sub(r"[^a-z0-9]", "", key.casefold())`, then reject canonical tokens including `apikey`, `secretkey`, `password`, `token`, `oracle`, `groundtruth`, `primarycause`, `answerkey`, `prompt` and `chainofthought`. Compute SHA-256 over canonical UTF-8 JSON with sorted keys and compact separators; checksum is external metadata and is not included in its own input.

- [ ] **Step 4: Write failing archive path, atomicity and state tests**

```python
def test_archive_requires_absolute_path_outside_repository(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigurationError, match="outside"):
        EvaluationArchive(tmp_path / "repo" / "var", repository_root=tmp_path / "repo")


def test_archive_advances_running_to_terminal_once(tmp_path: Path) -> None:
    archive = EvaluationArchive(tmp_path / "archive", repository_root=tmp_path / "repo")
    archive.start(RUNNING_ENVELOPE)
    archive.finalize(PASSED_ENVELOPE)
    assert archive.load("eval-1") == PASSED_ENVELOPE
    with pytest.raises(ValueError, match="terminal"):
        archive.finalize(FAILED_ENVELOPE)
```

- [ ] **Step 5: Run archive tests and verify they fail**

Run: `uv run pytest tests/test_evaluation_archive.py -q`

Expected: collection fails because `super_ai.evaluation.archive` does not exist.

- [ ] **Step 6: Implement configuration and atomic archive writes**

Add this template section with an empty portable value:

```json
"evaluation": {
  "archiveDir": ""
}
```

Implement exact methods `EvaluationArchive.from_config(*, config_path) -> EvaluationArchive`, `start(envelope) -> Path`, `finalize(envelope) -> Path`, `load(run_id) -> EvaluationRunEnvelope`, and `iter_envelopes() -> Iterator[EvaluationRunEnvelope]`.

Resolve `evaluation.archiveDir` with `Path(value).resolve(strict=False)`, require an absolute path, reject any path under `REPOSITORY_ROOT`, derive `kind/YYYY/MM/run-id.json`, write to a UUID temporary sibling, flush and `os.fsync()`, then use `os.replace()`. Validate run IDs as a basename before resolving paths.

- [ ] **Step 7: Run focused tests and commit**

Run:

```powershell
uv run pytest tests/test_evaluation_history.py tests/test_evaluation_archive.py -q
uv run ruff check src/super_ai/evaluation/history.py src/super_ai/evaluation/archive.py tests/test_evaluation_history.py tests/test_evaluation_archive.py
uv run pyright src/super_ai/evaluation/history.py src/super_ai/evaluation/archive.py
```

Expected: all commands exit 0.

```powershell
git add config/project.template.json config/user.project.template.json apps/backend/src/super_ai/evaluation/history.py apps/backend/src/super_ai/evaluation/archive.py apps/backend/tests/test_evaluation_history.py apps/backend/tests/test_evaluation_archive.py
git commit -m "feat: add safe evaluation artifact archive"
```

---

### Task 3: Generalize PostgreSQL Evaluation Persistence

**Files:**
- Create: `apps/backend/alembic/versions/202608170001_generalize_evaluation_runs.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/evaluation/persistence.py`
- Modify: `apps/backend/tests/test_evaluation_persistence.py`
- Modify: `apps/backend/tests/test_postgresql_migrations.py`

**Interfaces:**
- Consumes: `EvaluationRunEnvelope` and existing async session factory.
- Produces: `EvaluationRepository.start_envelope()`, `finalize_envelope()`, `attach_artifact_checksum()`, `list_runs_with_results()`, `list_benchmark_diagnostic_tasks()` and `get_run_with_result()` with generalized records.

- [ ] **Step 1: Write failing migration and repository tests**

Add migration assertions for these columns and backfill behavior:

```python
expected_columns = {
    ("aiops_evaluation_runs", "evaluation_kind"),
    ("aiops_evaluation_runs", "artifact_schema_version"),
    ("aiops_evaluation_runs", "artifact_checksum"),
    ("aiops_evaluation_runs", "provenance"),
    ("aiops_evaluation_results", "metrics"),
    ("aiops_evaluation_results", "result_payload"),
}
```

Add a repository test that starts and finalizes a Retrieval envelope, reloads it, and asserts `metrics`, `result_payload`, nullable Snapshot-only totals, checksum and status. Add concurrent identical writes that converge, plus same-run/different-checksum writes that raise `ValueError("different evaluation identity")`.

- [ ] **Step 2: Run the persistence tests and verify failure**

Run: `uv run pytest tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py -q`

Expected: failures report missing generalized columns and methods.

- [ ] **Step 3: Add the Alembic revision**

Use revision metadata:

```python
revision = "202608170001"
down_revision = "202608110001"
```

The upgrade must:

1. add `evaluation_kind`, `artifact_schema_version`, `artifact_checksum`, `provenance` to runs;
2. add `metrics` and `result_payload` to results;
3. make `total`, `raw_total`, and `passed` nullable for non-Snapshot/invalid runs;
4. backfill existing rows with `evaluation_kind='snapshot'`, `artifact_schema_version='v1'`, `provenance='native'`, empty JSONB metrics/result payload;
5. map legacy run statuses using joined result data: completed + passed -> passed, completed + not passed -> failed, infra_failed -> infra_invalid, pending -> running;
6. add indexes on `(evaluation_kind, created_at)` and `(status, created_at)`.

Downgrade must refuse when non-Snapshot rows exist rather than silently discard generic metrics, then restore legacy status names and schema.

- [ ] **Step 4: Implement generalized records and repository methods**

Change record fields to:

```python
@dataclass(frozen=True, slots=True)
class EvaluationResultRecord:
    result_id: str
    run_id: str
    dimension_scores: JsonDict
    total: int | None
    raw_total: int | None
    validity: str
    passed: bool | None
    failures: list[str]
    score_reasons: list[JsonDict]
    hard_gate: str | None
    metrics: JsonDict
    result_payload: JsonDict
    created_at: datetime
```

Add exact facade methods `start_envelope(envelope) -> EvaluationRunRecord`, `finalize_envelope(envelope, *, artifact_checksum) -> tuple[EvaluationRunRecord, EvaluationResultRecord | None]`, `attach_artifact_checksum(*, run_id, artifact_checksum) -> EvaluationRunRecord`, `list_runs_with_results() -> list[tuple[EvaluationRunRecord, EvaluationResultRecord | None]]`, and `list_benchmark_diagnostic_tasks() -> list[DiagnosticTaskRecord]`. Add safe `EvaluationDatabaseUnavailable` and translate only SQLAlchemy/asyncpg connectivity or transaction availability failures into it; validation and identity conflicts remain `ValueError`. `attach_artifact_checksum` uses a row lock and only changes NULL to the supplied checksum or accepts the identical checksum; a different existing checksum is a conflict. The administrative diagnostic query is restricted in SQL to rows whose JSONB `input_payload` contains a supported `benchmarkMode`; it is not exposed through HTTP or owner-scoped product APIs.

Keep `create_run()`, `fail_run()` and `finalize_run()` as compatibility wrappers for Snapshot callers until Task 4 migrates them. Use `SELECT ... FOR UPDATE` plus exact identity/result comparison for idempotency.

- [ ] **Step 5: Run focused PostgreSQL tests and commit**

Run:

```powershell
uv run pytest tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py -q
uv run alembic upgrade head
uv run alembic current
```

Expected: tests pass; Alembic reports `202608170001 (head)`.

```powershell
git add apps/backend/alembic/versions/202608170001_generalize_evaluation_runs.py apps/backend/src/super_ai/memory/models.py apps/backend/src/super_ai/memory/repositories.py apps/backend/src/super_ai/memory/sqlalchemy.py apps/backend/src/super_ai/evaluation/persistence.py apps/backend/tests/test_evaluation_persistence.py apps/backend/tests/test_postgresql_migrations.py
git commit -m "feat: generalize evaluation run persistence"
```

---

### Task 4: Shared Recorder and Snapshot Automatic Archive

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/recording.py`
- Create: `apps/backend/tests/test_evaluation_recording.py`
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`
- Modify: `apps/backend/scripts/run_snapshot_benchmark.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_cli.py`
- Modify: `apps/backend/tests/test_benchmark_runner.py`

**Interfaces:**
- Consumes: `EvaluationArchive`, `EvaluationRepository`, `EvaluationRunEnvelope`.
- Produces: `EvaluationRunRecorder.start()`, `finish()`, `fail()` and Snapshot CLI automatic persistence.

- [ ] **Step 1: Write failing recorder tests for archive-first and DB recovery**

```python
@pytest.mark.asyncio
async def test_recorder_keeps_artifact_when_database_finalize_fails() -> None:
    recorder = EvaluationRunRecorder(archive=archive, repository=FailingRepository())
    await recorder.start(RUNNING_ENVELOPE)
    outcome = await recorder.finish(PASSED_ENVELOPE)
    assert outcome.database_pending is True
    assert archive.load("eval-1") == PASSED_ENVELOPE


@pytest.mark.asyncio
async def test_recorder_continues_when_database_start_fails() -> None:
    recorder = EvaluationRunRecorder(archive=archive, repository=FailingStartRepository())
    start_outcome = await recorder.start(RUNNING_ENVELOPE)
    finish_outcome = await recorder.finish(PASSED_ENVELOPE)
    assert start_outcome.database_pending is True
    assert finish_outcome.database_pending is True
    assert archive.load("eval-1") == PASSED_ENVELOPE


@pytest.mark.asyncio
async def test_recorder_archive_failure_is_infrastructure_error() -> None:
    recorder = EvaluationRunRecorder(archive=FailingArchive(), repository=repository)
    with pytest.raises(EvaluationArchiveError):
        await recorder.start(RUNNING_ENVELOPE)
    assert repository.calls == []


def test_keyboard_interrupt_maps_to_safe_interrupted_envelope() -> None:
    envelope = interrupted_envelope(RUNNING_ENVELOPE, completed_at=FIXED_TIME)
    assert envelope.status == "interrupted"
    assert envelope.failure_category == "operator_interrupt"
```

- [ ] **Step 2: Run recorder tests and verify failure**

Run: `uv run pytest tests/test_evaluation_recording.py -q`

Expected: collection fails because `recording.py` does not exist.

- [ ] **Step 3: Implement the coordinator**

```python
class EvaluationRunRecorder:
    def __init__(self, *, archive: EvaluationArchive, repository: EvaluationRepository) -> None:
        self._archive = archive
        self._repository = repository
    async def start(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        self._archive.start(envelope)
        try:
            await self._repository.start_envelope(envelope)
        except EvaluationDatabaseUnavailable:
            return RecordingOutcome(database_pending=True)
        return RecordingOutcome(database_pending=False)
    async def finish(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        self._archive.finalize(envelope)
        try:
            await self._repository.start_envelope(running_from_terminal(envelope))
            await self._repository.finalize_envelope(
                envelope, artifact_checksum=artifact_checksum(envelope)
            )
        except EvaluationDatabaseUnavailable:
            return RecordingOutcome(database_pending=True)
        return RecordingOutcome(database_pending=False)
```

`RecordingOutcome` is a frozen dataclass with `database_pending: bool`. `EvaluationRepository` wraps only connectivity/transaction availability failures as `EvaluationDatabaseUnavailable`; identity conflicts and validation errors still propagate. Start-stage database unavailability never aborts the benchmark: the recorder retains the running Artifact, executes the real evaluation, finalizes the true result locally, and retries idempotent DB start/finalize at finish. If DB remains unavailable, the CLI returns infrastructure exit code 2 while the immutable Artifact retains the actual benchmark outcome; `reconcile` later synchronizes it. Never include raw exception text in the Artifact or stdout JSON.

- [ ] **Step 4: Write failing Snapshot CLI tests**

Tests must prove:

```python
def test_snapshot_output_is_optional_export_not_primary_storage() -> None:
    assert MODULE.build_parser().parse_args(["--scenario", "APY-013"]).output is None

@pytest.mark.asyncio
async def test_snapshot_failure_still_finalizes_safe_archive(tmp_path: Path) -> None:
    exit_code = await run_snapshot_with_fake_adapter(
        tmp_path, raised=BenchmarkRunError("agent_failed", "adapter_error")
    )
    saved = only_envelope(tmp_path)
    assert exit_code == 2
    assert saved.status == "agent_failed"
    assert saved.failure_category == "adapter_error"
```

- [ ] **Step 5: Refactor Snapshot ownership without double persistence**

Move lifecycle ownership to the CLI/recorder boundary. `SnapshotBenchmarkRunner.run()` must return a value carrying both score and `diagnostic_task_id`:

```python
@dataclass(frozen=True, slots=True)
class SnapshotRunOutcome:
    result: EvaluationResult
    diagnostic_task_id: str
```

Remove direct `EvaluationPersistence` calls from the runner; retain its classified `BenchmarkRunError`. In `run_snapshot_benchmark.py`, build one recorder, start before the runner call, and convert every outcome/exception into one terminal envelope in a bounded `try/except/finally`. Catch `KeyboardInterrupt` and `asyncio.CancelledError` separately, finalize `status=interrupted`, then preserve exit code 130 or re-raise cancellation after persistence. Register supported SIGTERM handlers to cancel the active async task so it follows the same path. For `--runs N`, allocate and persist N independent run IDs. `--output` remains an extra combined export only.

- [ ] **Step 6: Run Snapshot and recorder tests, then commit**

Run:

```powershell
uv run pytest tests/test_evaluation_recording.py tests/test_snapshot_benchmark_cli.py tests/test_benchmark_runner.py tests/test_evaluation_persistence.py -q
uv run ruff check src/super_ai/evaluation/recording.py src/super_ai/evaluation/runner.py scripts/run_snapshot_benchmark.py
uv run pyright src/super_ai/evaluation/recording.py src/super_ai/evaluation/runner.py scripts/run_snapshot_benchmark.py
```

Expected: all commands exit 0.

```powershell
git add apps/backend/src/super_ai/evaluation/recording.py apps/backend/src/super_ai/evaluation/runner.py apps/backend/scripts/run_snapshot_benchmark.py apps/backend/tests/test_evaluation_recording.py apps/backend/tests/test_snapshot_benchmark_cli.py apps/backend/tests/test_benchmark_runner.py
git commit -m "feat: automatically persist snapshot evaluations"
```

---

### Task 5: Retrieval Automatic Persistence

**Files:**
- Modify: `apps/backend/scripts/run_retrieval_benchmark.py`
- Modify: `apps/backend/tests/test_retrieval_benchmark_cli.py`

**Interfaces:**
- Consumes: `EvaluationRunRecorder`, Retrieval `metrics` payload and query file.
- Produces: one persisted Retrieval run per CLI invocation with dataset checksum and threshold failures.

- [ ] **Step 1: Write failing Retrieval lifecycle tests**

```python
@pytest.mark.asyncio
async def test_retrieval_threshold_failure_is_persisted(tmp_path: Path) -> None:
    exit_code = await run_command_with_fakes(tmp_path, metrics=FAILING_METRICS)
    envelope = only_envelope(tmp_path)
    assert exit_code == 1
    assert envelope.evaluation_kind == "retrieval"
    assert envelope.status == "failed"
    assert envelope.metrics["recallAt1"] == 0.79


@pytest.mark.asyncio
async def test_retrieval_tool_error_is_persisted_as_infra_invalid(tmp_path: Path) -> None:
    exit_code = await run_command_with_fakes(tmp_path, raised=TimeoutError())
    envelope = only_envelope(tmp_path)
    assert exit_code == 2
    assert envelope.status == "infra_invalid"
    assert envelope.failure_category == "retrieval_runtime_error"


@pytest.mark.asyncio
async def test_retrieval_cancellation_is_persisted_as_interrupted(tmp_path: Path) -> None:
    with pytest.raises(asyncio.CancelledError):
        await run_command_with_fakes(tmp_path, raised=asyncio.CancelledError())
    assert only_envelope(tmp_path).status == "interrupted"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/test_retrieval_benchmark_cli.py -q`

Expected: new lifecycle assertions fail because no canonical Artifact exists.

- [ ] **Step 3: Integrate the recorder**

Add optional `--run-id`; default to `f"eval-{uuid4().hex}"`. Compute SHA-256 of exact query YAML bytes and store it as `metadata.datasetChecksum`. Start the recorder before building embedding/rerank/Milvus clients. Convert `_passes(payload)` to terminal `passed` or `failed`; map classified dependency/configuration exceptions to `infra_invalid`. Catch `KeyboardInterrupt`, `asyncio.CancelledError` and supported signal-driven cancellation exactly as in Snapshot, persist `interrupted`, and preserve cancellation semantics. Preserve the existing tenant-safe payload and exit codes.

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
uv run pytest tests/test_retrieval_benchmark_cli.py tests/test_evaluation_recording.py -q
uv run ruff check scripts/run_retrieval_benchmark.py tests/test_retrieval_benchmark_cli.py
uv run pyright scripts/run_retrieval_benchmark.py
```

Expected: all commands exit 0.

```powershell
git add apps/backend/scripts/run_retrieval_benchmark.py apps/backend/tests/test_retrieval_benchmark_cli.py
git commit -m "feat: persist retrieval benchmark runs"
```

---

### Task 6: Live and CLS Automatic Persistence

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/tests/test_live_benchmark_cli.py`
- Modify: `apps/backend/tests/test_live_cls_acceptance.py`

**Interfaces:**
- Consumes: existing Live safe payload, `EvaluationRunRecorder`, explicit Live run ID.
- Produces: canonical Live/CLS Artifact and PostgreSQL record for pass, valid fail, recovery denial, CLS timeout and infrastructure failure.

- [ ] **Step 1: Write failing Live/CLS persistence tests**

```python
@pytest.mark.asyncio
async def test_recovery_denied_is_saved_as_valid_failure(tmp_path: Path) -> None:
    payload, exit_code = await run_live_with_fakes(tmp_path, category="recovery_denied")
    envelope = only_envelope(tmp_path)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert envelope.status == "failed"
    assert envelope.validity == "VALID_FAIL"


@pytest.mark.asyncio
async def test_cls_timeout_is_saved_as_infra_invalid(tmp_path: Path) -> None:
    payload, exit_code = await run_live_with_fakes(tmp_path, category="cls_index_timeout")
    envelope = only_envelope(tmp_path)
    assert exit_code == 2
    assert payload["status"] == "infra_invalid"
    assert envelope.metadata["evidenceSource"] == "cls"


@pytest.mark.asyncio
async def test_live_cancellation_is_saved_as_interrupted(tmp_path: Path) -> None:
    with pytest.raises(asyncio.CancelledError):
        await run_live_with_fakes(tmp_path, raised=asyncio.CancelledError())
    assert only_envelope(tmp_path).status == "interrupted"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/test_live_benchmark_cli.py tests/test_live_cls_acceptance.py -q`

Expected: persistence assertions fail because Live only writes worktree-local `var/benchmarks/live` JSON.

- [ ] **Step 3: Replace the primary Live report path with the shared recorder**

Start a Live envelope before evidence preparation and scenario execution. Convert the existing safe result allowlist into `metrics` and `result_payload`; preserve `evidenceSource`, `verificationPassed`, `cleanupSucceeded`, `failureStage` and `authorizationCode`. Keep `LIVE_REPORT_ROOT` only as backward-compatible optional export for `report`, while canonical reads prefer `EvaluationArchive.load(run_id)`.

Map current `classify_live_failure()` results exactly:

```python
status_map = {
    "passed": "passed",
    "failed": "failed",
    "infra_invalid": "infra_invalid",
}
```

Unexpected exceptions at the CLI boundary become `infra_invalid/live_runtime_error`; do not persist raw exception messages. `KeyboardInterrupt`, `asyncio.CancelledError` and supported signal-driven cancellation follow the shared interrupted path before cleanup and cancellation propagation.

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
uv run pytest tests/test_live_benchmark_cli.py tests/test_live_cls_acceptance.py tests/test_evaluation_recording.py -q
uv run ruff check src/super_ai/evaluation/live/cli.py tests/test_live_benchmark_cli.py tests/test_live_cls_acceptance.py
uv run pyright src/super_ai/evaluation/live/cli.py
```

Expected: all commands exit 0.

```powershell
git add apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/tests/test_live_benchmark_cli.py apps/backend/tests/test_live_cls_acceptance.py
git commit -m "feat: persist live and cls evaluations"
```

---

### Task 7: Idempotent History Import, Reconciliation and Summary

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/history_import.py`
- Create: `apps/backend/src/super_ai/evaluation/summary.py`
- Create: `apps/backend/scripts/manage_evaluation_history.py`
- Create: `apps/backend/tests/test_evaluation_history_import.py`
- Create: `apps/backend/tests/test_evaluation_summary.py`

**Interfaces:**
- Consumes: explicit history paths, `EvaluationArchive`, `EvaluationRepository.list_runs_with_results()`.
- Produces: `import_history()`, `reconcile_history()`, `build_history_summary()`, CLI commands `import-history`, `reconcile`, `summarize`.

- [ ] **Step 1: Write failing importer safety and idempotency tests**

```python
@pytest.mark.asyncio
async def test_importer_rejects_path_traversal_and_forbidden_fields(tmp_path: Path) -> None:
    report = await import_history(
        sources=[tmp_path / "legacy"],
        archive=archive,
        repository=fake_repository,
    )
    assert report.rejected == 2
    assert report.imported == 0


@pytest.mark.asyncio
async def test_same_run_same_checksum_is_idempotent_but_conflict_is_reported() -> None:
    first = await import_history(sources=[source_a], archive=archive, repository=repository)
    second = await import_history(sources=[source_a, source_conflict], archive=archive, repository=repository)
    assert first.imported == 1
    assert second.duplicates == 1
    assert second.conflicts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["groundTruth", "primaryCause", "answer_key", "chainOfThought"])
async def test_importer_rejects_hidden_answer_key_variants(key: str) -> None:
    source = write_legacy_result({"metadata": {"nested": {key: "hidden"}}})
    report = await import_history(sources=[source], archive=archive, repository=repository)
    assert report.rejected == 1
    assert report.imported == 0
```

- [ ] **Step 2: Run importer tests and verify failure**

Run: `uv run pytest tests/test_evaluation_history_import.py -q`

Expected: collection fails because importer module does not exist.

- [ ] **Step 3: Implement explicit-source adapters and reconciliation**

Define the report value object:

```python
@dataclass(frozen=True, slots=True)
class HistoryImportReport:
    imported: int
    duplicates: int
    reconstructed: int
    rejected: int
    conflicts: int
    database_pending: int
    entries: tuple[HistoryImportEntry, ...]
```

Implement exact async functions `import_history(*, sources: Sequence[Path], archive: EvaluationArchive, repository: EvaluationRepository) -> HistoryImportReport` and `reconcile_history(*, archive: EvaluationArchive, repository: EvaluationRepository, stale_after: timedelta = timedelta(hours=6)) -> HistoryImportReport`. Support only recognized existing Snapshot combined JSON, Retrieval report JSON, Live safe report JSON and current database records. Never recursively scan outside explicitly supplied roots. Store source path and source checksum in `metadata.importSource`; do not copy content fields outside the envelope allowlist. Apply the same per-container allowlists and canonical-key rejection to all imported nested content. Database-only Live audits come from `list_benchmark_diagnostic_tasks()`, create `provenance=reconstructed`, `passed=None`, empty metrics and explicit `result_payload={"missingResultArtifact": True}`. During reconciliation, a `running` record older than `stale_after` becomes `interrupted/stale_running_record`; newer running records remain untouched. The management CLI invokes every async operation through `asyncio.run()`.

- [ ] **Step 4: Write failing deterministic summary tests**

```python
def test_summary_distinguishes_native_reconstructed_and_pending() -> None:
    summary = build_history_summary(
        ENVELOPES,
        database_checksums={"eval-1": artifact_checksum(ENVELOPES[0])},
    )
    assert summary.counts.total == 3
    assert summary.counts.reconstructed == 1
    assert summary.counts.database_pending == 1
    assert "Recall@1" in summary.markdown
    assert "不可恢复边界" in summary.markdown


def test_summary_detects_all_checksum_reconciliation_states() -> None:
    summary = build_history_summary(
        [ARCHIVE_ONLY, SAME_CHECKSUM, DIFFERENT_CHECKSUM],
        database_checksums={
            "db-only": DB_ONLY_CHECKSUM,
            SAME_CHECKSUM.run_id: artifact_checksum(SAME_CHECKSUM),
            DIFFERENT_CHECKSUM.run_id: "0" * 64,
        },
    )
    assert summary.reconciliation.archive_only == 1
    assert summary.reconciliation.database_only == 1
    assert summary.reconciliation.synchronized == 1
    assert summary.reconciliation.conflicts == 1


@pytest.mark.asyncio
async def test_reconcile_marks_only_stale_running_records_interrupted() -> None:
    report = await reconcile_history(
        archive=archive,
        repository=repository,
        stale_after=timedelta(hours=6),
    )
    assert report.entries[0].status == "interrupted"
    assert archive.load("stale-run").failure_category == "stale_running_record"


@pytest.mark.asyncio
async def test_reconcile_backfills_legacy_null_checksum_once() -> None:
    repository.add_legacy_snapshot(artifact_checksum=None)
    first = await reconcile_history(archive=archive, repository=repository)
    second = await reconcile_history(archive=archive, repository=repository)
    assert first.database_pending == 0
    assert repository.checksum_for("legacy-snapshot") == artifact_checksum(
        archive.load("legacy-snapshot")
    )
    assert second.conflicts == 0
```

- [ ] **Step 5: Implement index and Markdown generation**

`build_history_summary()` accepts `database_checksums: Mapping[str, str | None]`, compares both identity and checksum, sorts by `(created_at, run_id)`, computes per-kind/status/provenance counts, averages only present numeric values, and never converts missing values to zero. Reconciliation explicitly reports archive-only, database-only, synchronized, NULL-checksum and checksum-conflict states. For the 15 migrated Snapshot rows whose checksum is NULL, reconstruct the canonical envelope from DB, write or compare the Artifact, then call `attach_artifact_checksum`; never treat NULL as already synchronized. Write `index.jsonl` and `summary.md` atomically under the archive root. The index contains one safe flattened row per Run and the Artifact checksum.

- [ ] **Step 6: Implement the management CLI**

Use exact subcommands:

```text
python scripts/manage_evaluation_history.py import-history --source D:\eval-source-a --source D:\eval-source-b --config ../../config/project.json
python scripts/manage_evaluation_history.py reconcile --config ../../config/project.json
python scripts/manage_evaluation_history.py summarize --config ../../config/project.json
python scripts/manage_evaluation_history.py audit --config ../../config/project.json
```

`audit` loads every canonical Artifact through the strict schema and recursive forbidden-field validator, checks its path and checksum, and prints aggregate counts only. All commands return 0 on success, 1 when conflicts/rejections/invalid artifacts exist, and 2 for configuration/database/archive infrastructure failure.

- [ ] **Step 7: Run tests and commit**

Run:

```powershell
uv run pytest tests/test_evaluation_history_import.py tests/test_evaluation_summary.py tests/test_evaluation_archive.py tests/test_evaluation_persistence.py -q
uv run ruff check src/super_ai/evaluation/history_import.py src/super_ai/evaluation/summary.py scripts/manage_evaluation_history.py
uv run pyright src/super_ai/evaluation/history_import.py src/super_ai/evaluation/summary.py scripts/manage_evaluation_history.py
```

Expected: all commands exit 0.

```powershell
git add apps/backend/src/super_ai/evaluation/history_import.py apps/backend/src/super_ai/evaluation/summary.py apps/backend/scripts/manage_evaluation_history.py apps/backend/tests/test_evaluation_history_import.py apps/backend/tests/test_evaluation_summary.py
git commit -m "feat: import and summarize evaluation history"
```

---

### Task 8: Local Historical Migration, Documentation and Full Verification

**Files:**
- Modify: `apps/backend/README.md`
- Modify: `openspec/changes/persist-evaluation-results/tasks.md`
- Runtime output outside Git: configured `D:\桌面\后端\agent_py-evaluation-archive\`

**Interfaces:**
- Consumes: all previous tasks and explicitly enumerated legacy worktree paths.
- Produces: real shared archive, reconciled PostgreSQL records, `index.jsonl`, `summary.md`, completed OpenSpec task evidence.

- [ ] **Step 1: Configure the ignored local user project file**

Add only this non-secret section to ignored `config/user.project.json`:

```json
"evaluation": {
  "archiveDir": "D:\\桌面\\后端\\agent_py-evaluation-archive"
}
```

Verify `git status --short` does not show `config/user.project.json`.

- [ ] **Step 2: Run a dry inventory and explicit historical import**

Use only these explicitly identified `var/benchmarks` sources as repeated `--source` arguments; omit a path only when it no longer exists and record that fact in the acceptance report:

```text
D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\var\benchmarks
D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\benchmark-public-title-leak\apps\backend\var\benchmarks
D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\benchmark-rag-live-expansion\apps\backend\var\benchmarks
D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\fix-live-evidence-args\apps\backend\var\benchmarks
D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\snapshot-tool-calling-contract\apps\backend\var\benchmarks
```

Do not delete or move the source files. Expected report must account for:

- all 15 database Snapshot runs;
- all recognizable Retrieval JSON files;
- all recognizable Live/CLS safe reports;
- reconstructed Live audit-only records;
- duplicates and conflicts separately.

Run `import-history` once, then a second time. Expected second run: `imported=0`; all prior accepted items appear as duplicates, not conflicts.

- [ ] **Step 3: Reconcile and generate the summary**

Run:

```powershell
uv run python scripts/manage_evaluation_history.py reconcile --config ../../config/project.json
uv run python scripts/manage_evaluation_history.py summarize --config ../../config/project.json
```

Expected: PostgreSQL and archive run IDs agree except explicitly reported reconstructed/pending cases; `summary.md` includes latest APY-013 `eval-52e52567c646499abac4790d51df4906` with score 89.

- [ ] **Step 4: Update operational documentation**

Document configuration, automatic persistence, exit codes, import/reconcile/summarize commands, worktree-external retention, and the historical recovery boundary. Do not publish the user's absolute path as a repository default; use a neutral example such as `D:\agent-py-data\evaluation-archive` in committed docs.

- [ ] **Step 5: Run complete verification**

From `apps/backend`:

```powershell
uv run pytest tests/test_evaluation_history.py tests/test_evaluation_archive.py tests/test_evaluation_recording.py tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py tests/test_snapshot_benchmark_cli.py tests/test_benchmark_runner.py tests/test_retrieval_benchmark_cli.py tests/test_live_benchmark_cli.py tests/test_live_cls_acceptance.py tests/test_evaluation_history_import.py tests/test_evaluation_summary.py -q
uv run ruff check .
uv run pyright
uv run pytest -q
```

From repository root:

```powershell
openspec validate persist-evaluation-results --strict
openspec validate --all
npm run docs:build
```

Expected: all commands exit 0 with no unconditional skips added.

- [ ] **Step 6: Inspect the real archive for forbidden content**

Run:

```powershell
uv run python scripts/manage_evaluation_history.py audit --config ../../config/project.json
```

Expected: zero forbidden keys, zero invalid schemas, zero path escapes, zero checksum mismatches and zero conflicting terminal Artifacts.

- [ ] **Step 7: Commit the documentation and completed task evidence**

```powershell
git add apps/backend/README.md openspec/changes/persist-evaluation-results/tasks.md
git commit -m "docs: document evaluation history operations"
```

Do not add `D:\桌面\后端\agent_py-evaluation-archive`, any `config/user.project.json`, legacy `var/` files, credentials or real logs.

---

## Acceptance Evidence

Implementation is complete only when the final report provides:

1. actual counts by Snapshot/Retrieval/Live-CLS, status and provenance;
2. PostgreSQL/archive reconciliation counts and any unresolved conflicts;
3. the absolute local path to `summary.md` and `index.jsonl`;
4. proof that a repeated import is idempotent;
5. focused test, full pytest, Ruff, Pyright, OpenSpec and docs build outputs;
6. confirmation that no secrets, Ground Truth, prompts, private reasoning, raw CLS logs or runtime artifacts entered Git.
