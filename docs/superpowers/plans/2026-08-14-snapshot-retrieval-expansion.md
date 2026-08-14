# Snapshot and Retrieval Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand AgentPy from six to ten answer-isolated Snapshot scenarios and from sixty to sixty-four Retrieval queries while retaining exactly thirty generic knowledge cards.

**Architecture:** Add four complete Snapshot fixtures through the existing YAML loaders and scoring contracts. Add one evaluator-only Snapshot-to-card coverage manifest and a focused loader that validates references without exposing the manifest to RAG, prompts, artifacts, or Milvus.

**Tech Stack:** Python 3.10, PyYAML, pytest, existing AgentPy evaluation loaders, Markdown knowledge cards

## Global Constraints

- Snapshot count must be exactly 10: existing six plus `APY-013` through `APY-016`.
- Retrieval query count must be exactly 64: 58 answerable and 6 no-answer probes.
- `docs/knowledge-candidates` must remain exactly 30 Markdown files.
- Do not add Snapshot IDs, ground truth fields, evidence IDs, triggers, fixed Snapshot values, or evaluator mappings to RAG chunks or Agent inputs.
- Do not weaken existing Retrieval metric thresholds or remove difficult queries.
- Add no dependency and do not call Docker, CLS, Milvus, or a real model in ordinary CI.
- Use TDD and commit each independently reviewable task.
- Execute inline in the current session; do not start subagents.

---

### Task 1: Lock the expansion counts and answer-isolation contracts

**Files:**
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_retrieval_evaluation.py`
- Modify: `apps/backend/tests/test_knowledge_candidate_safety.py`

**Interfaces:**
- Consumes: `load_scenario(path: Path)`, `load_oracle(path: Path)`, `load_retrieval_queries(path: Path)`.
- Produces: executable count, distribution, uniqueness, and answer-isolation contracts for Tasks 2–4.

- [ ] **Step 1: Write the failing count tests**

```python
def test_repository_contains_exactly_ten_snapshot_scenarios() -> None:
    scenario_dirs = sorted(path for path in SCENARIOS.iterdir() if path.is_dir())
    assert [path.name for path in scenario_dirs] == [
        "APY-002", "APY-003", "APY-006", "APY-007", "APY-011",
        "APY-012", "APY-013", "APY-014", "APY-015", "APY-016",
    ]
    assert all((path / "scenario.yaml").is_file() for path in scenario_dirs)
    assert all((path / "ground_truth.yaml").is_file() for path in scenario_dirs)
    assert all((path / "provenance.yaml").is_file() for path in scenario_dirs)
    assert all((path / "snapshot" / "tool_responses.yaml").is_file() for path in scenario_dirs)

def test_repository_contains_sixty_four_queries_with_fixed_distribution() -> None:
    queries = load_retrieval_queries(QUERIES)
    assert len(queries) == 64
    assert sum(not query.expected_no_answer for query in queries) == 58
    assert sum(query.expected_no_answer for query in queries) == 6
    assert len({query.id for query in queries}) == 64
```

- [ ] **Step 2: Add the unchanged-card-count assertion**

```python
def test_expansion_does_not_create_snapshot_answer_cards() -> None:
    cards = sorted(KNOWLEDGE.glob("*.md"))
    assert len(cards) == 30
    assert not any(card.name.startswith("apy-") for card in cards)
```

- [ ] **Step 3: Run the tests and confirm RED**

Run from `apps/backend`:

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_evaluation_scenarios.py tests/test_retrieval_evaluation.py tests/test_knowledge_candidate_safety.py -q -p no:cacheprovider --basetemp=var/pytest-plan-snapshot-counts
```

Expected: FAIL because only six scenario directories and sixty queries exist.

- [ ] **Step 4: Commit the red contracts**

```powershell
git add apps/backend/tests/test_evaluation_scenarios.py apps/backend/tests/test_retrieval_evaluation.py apps/backend/tests/test_knowledge_candidate_safety.py
git commit -m "test: define snapshot and retrieval expansion contracts"
```

### Task 2: Add four complete Snapshot scenario fixtures

**Files:**
- Create: `benchmarks/agentpy/scenarios/APY-013/scenario.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-013/ground_truth.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-013/provenance.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-013/snapshot/tool_responses.yaml`
- Create: the same four files under `APY-014`, `APY-015`, and `APY-016`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Consumes: existing public `scenario.yaml`, evaluator-only `ground_truth.yaml`, provenance, and frozen tool-response schemas.
- Produces: loadable scenarios `APY-013`, `APY-014`, `APY-015`, `APY-016` with distinguishable primary and alternative causes.

- [ ] **Step 1: Add failing semantic assertions for the four scenarios**

```python
@pytest.mark.parametrize(
    ("scenario_id", "mechanism", "required_ids"),
    [
        ("APY-013", "opposite_order_transaction_deadlock", {"deadlock-error", "wait-cycle"}),
        ("APY-014", "benchmark_clients_exhausted_maxclients", {"maxclients-capacity", "scoped-client-set"}),
        ("APY-015", "upstream_response_exceeded_proxy_read_timeout", {"connect-succeeded", "response-timeout"}),
        ("APY-016", "retry_after_ignored_without_backoff", {"retry-amplification", "missing-backoff"}),
    ],
)
def test_new_scenarios_require_discriminating_evidence(
    scenario_id: str, mechanism: str, required_ids: set[str]
) -> None:
    path = SCENARIOS / scenario_id
    scenario = load_scenario(path)
    oracle = load_oracle(path)
    assert scenario.id == scenario_id
    assert oracle.primary_cause.mechanism == mechanism
    assert required_ids <= {milestone.id for milestone in oracle.required_evidence}
    public_text = (path / "scenario.yaml").read_text(encoding="utf-8").lower()
    assert mechanism not in public_text
    assert "primary_cause" not in public_text
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_evaluation_scenarios.py -q -p no:cacheprovider --basetemp=var/pytest-plan-snapshot-fixtures
```

Expected: FAIL with missing `APY-013` through `APY-016` directories.

- [ ] **Step 3: Create the four public scenarios with these exact contracts**

```yaml
# APY-013/scenario.yaml
id: APY-013
title: Order transactions are rolling back during concurrent updates.
symptom_family: postgresql_transaction_failure
difficulty: hard
modes: [snapshot]
alert: {alertname: PostgresTransactionRollbackHigh, service: order-service, severity: critical, summary: Concurrent order updates are failing and retrying.}
hypotheses:
  - {id: postgres_deadlock, description: Concurrent transactions formed a cyclic dependency.}
  - {id: postgres_lock_wait, description: A long transaction is blocking otherwise valid work.}
  - {id: postgres_slow_query, description: Expensive statements are exceeding the request deadline.}
snapshot_file: snapshot/tool_responses.yaml
```

Use the same public shape with these exact IDs and hypotheses:

```yaml
# APY-014
id: APY-014
symptom_family: redis_connection_rejection
hypotheses: [redis_maxclients, redis_process_unavailable, host_file_descriptor_exhaustion, redis_stale_client_pool]
# APY-015
id: APY-015
symptom_family: nginx_gateway_timeout
hypotheses: [nginx_upstream_response_timeout, nginx_upstream_unavailable, nginx_route_mismatch, nginx_gateway_pressure]
# APY-016
id: APY-016
symptom_family: http_rate_limit_amplification
hypotheses: [client_retry_storm, expected_rate_limiting, malicious_traffic, downstream_saturation]
```

- [ ] **Step 4: Create evaluator-only answers and frozen evidence**

Use these exact primary mechanisms and evidence milestones; each `alternatives` value references evidence IDs present in that scenario's `snapshot/tool_responses.yaml`:

```yaml
# APY-013/ground_truth.yaml
primary_cause: {component: order-service, mechanism: opposite_order_transaction_deadlock, trigger: concurrent_updates_acquired_order_and_inventory_rows_in_reverse_order}
required_evidence:
  - {id: deadlock-error, alternatives: [[postgres-40p01-deadlock-record]]}
  - {id: wait-cycle, alternatives: [[postgres-opposite-resource-order, postgres-deadlock-cycle]]}
required_rule_outs: [postgres_lock_wait]
forbidden_claims: [postgres_slow_query]

# APY-014/ground_truth.yaml
primary_cause: {component: live-eval-redis, mechanism: benchmark_clients_exhausted_maxclients, trigger: run_scoped_clients_filled_the_connection_limit}
required_evidence:
  - {id: maxclients-capacity, alternatives: [[redis-connected-equals-maxclients, redis-rejections-increased]]}
  - {id: scoped-client-set, alternatives: [[redis-run-scoped-clients]]}
required_rule_outs: [redis_process_unavailable, host_file_descriptor_exhaustion]
forbidden_claims: [redis_stale_client_pool]

# APY-015/ground_truth.yaml
primary_cause: {component: live-eval-upstream, mechanism: upstream_response_exceeded_proxy_read_timeout, trigger: bounded_slow_response_exceeded_gateway_read_deadline}
required_evidence:
  - {id: connect-succeeded, alternatives: [[nginx-upstream-connect-fast, upstream-health-passed]]}
  - {id: response-timeout, alternatives: [[nginx-504-read-timeout, upstream-response-time-exceeded]]}
required_rule_outs: [nginx_upstream_unavailable, nginx_route_mismatch]
forbidden_claims: [nginx_gateway_pressure]

# APY-016/ground_truth.yaml
primary_cause: {component: checkout-client, mechanism: retry_after_ignored_without_backoff, trigger: immediate_retry_policy_amplified_rate_limited_requests}
required_evidence:
  - {id: retry-amplification, alternatives: [[http-attempts-per-request-rise, http-429-and-volume-rise]]}
  - {id: missing-backoff, alternatives: [[http-retry-after-ignored, http-zero-backoff-timeline]]}
required_rule_outs: [expected_rate_limiting, malicious_traffic]
forbidden_claims: [downstream_saturation]
```

For each provenance file set `type: agentpy-original`, list only cited public mechanism sources, state that alert/observations/distractors/answer are project-synthesized, include license notes, `accessed: 2026-08-14`, and `created: 2026-08-14`. Frozen tool responses must include every evidence ID above plus at least one contradictory distractor, with no `primary_cause`, `oracle`, `trigger`, or recovery answer key.

- [ ] **Step 5: Run the Snapshot suite and confirm GREEN**

Run the Step 2 command again.

Expected: all `test_evaluation_scenarios.py` tests PASS and exactly ten scenario directories are found.

- [ ] **Step 6: Commit the fixtures**

```powershell
git add benchmarks/agentpy/scenarios apps/backend/tests/test_evaluation_scenarios.py
git commit -m "feat: add four production failure snapshots"
```

### Task 3: Add evaluator-only Snapshot knowledge coverage

**Files:**
- Create: `benchmarks/agentpy/retrieval/snapshot_knowledge_coverage.yaml`
- Create: `apps/backend/src/super_ai/evaluation/knowledge_coverage.py`
- Create: `apps/backend/tests/test_snapshot_knowledge_coverage.py`

**Interfaces:**
- Produces: `SnapshotKnowledgeCoverage(snapshot_id: str, documents: tuple[str, ...])` and `load_snapshot_knowledge_coverage(path: Path, *, scenario_root: Path, knowledge_root: Path) -> tuple[SnapshotKnowledgeCoverage, ...]`.
- Consumes: Snapshot directory names and Markdown filenames only; it never returns card contents.

- [ ] **Step 1: Write failing completeness and rejection tests**

```python
def test_coverage_maps_every_snapshot_to_existing_generic_cards() -> None:
    rows = load_snapshot_knowledge_coverage(COVERAGE, scenario_root=SCENARIOS, knowledge_root=KNOWLEDGE)
    assert {row.snapshot_id for row in rows} == {path.name for path in SCENARIOS.iterdir() if path.is_dir()}
    assert all(row.documents for row in rows)
    assert len(list(KNOWLEDGE.glob("*.md"))) == 30

@pytest.mark.parametrize("bad_document", ["../ground_truth.yaml", "APY-013.md", "missing.md"])
def test_coverage_rejects_paths_answers_and_missing_cards(tmp_path: Path, bad_document: str) -> None:
    path = tmp_path / "coverage.yaml"
    path.write_text(f"coverage:\n  APY-013: [{bad_document}]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot_knowledge_coverage(path, scenario_root=SCENARIOS, knowledge_root=KNOWLEDGE)
```

- [ ] **Step 2: Run and confirm RED**

Run:

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_snapshot_knowledge_coverage.py -q -p no:cacheprovider --basetemp=var/pytest-plan-coverage
```

Expected: FAIL because the module and manifest do not exist.

- [ ] **Step 3: Implement the immutable loader**

```python
@dataclass(frozen=True, slots=True)
class SnapshotKnowledgeCoverage:
    snapshot_id: str
    documents: tuple[str, ...]

def load_snapshot_knowledge_coverage(
    path: Path, *, scenario_root: Path, knowledge_root: Path
) -> tuple[SnapshotKnowledgeCoverage, ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = payload.get("coverage") if isinstance(payload, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Snapshot knowledge coverage must be a non-empty mapping.")
    scenario_ids = {item.name for item in scenario_root.iterdir() if item.is_dir()}
    if set(raw) != scenario_ids:
        raise ValueError("Snapshot knowledge coverage must match repository scenarios exactly.")
    rows: list[SnapshotKnowledgeCoverage] = []
    for snapshot_id, value in sorted(raw.items()):
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Coverage for {snapshot_id} must contain document names.")
        documents = tuple(value)
        for document in documents:
            if Path(document).name != document or document.lower().startswith("apy-"):
                raise ValueError(f"Coverage document is unsafe: {document}.")
            if not (knowledge_root / document).is_file():
                raise ValueError(f"Coverage document does not exist: {document}.")
        rows.append(SnapshotKnowledgeCoverage(snapshot_id, documents))
    return tuple(rows)
```

- [ ] **Step 4: Add the exact mapping**

```yaml
coverage:
  APY-002: [postgres-pool-exhaustion.md]
  APY-003: [nginx-upstream-502.md]
  APY-006: [nginx-upstream-502.md, nginx-routing-service-discovery.md]
  APY-007: [redis-unavailable.md]
  APY-011: [postgres-pool-exhaustion.md]
  APY-012: [redis-unavailable.md, redis-failover-reconnect.md]
  APY-013: [postgres-deadlock.md]
  APY-014: [redis-maxclients-pressure.md]
  APY-015: [nginx-upstream-timeout.md]
  APY-016: [http-rate-limit-retry-storm.md]
```

- [ ] **Step 5: Verify GREEN and prove the file is not imported**

Run the Step 2 command, then:

```powershell
rg -n "snapshot_knowledge_coverage|APY-013|APY-014|APY-015|APY-016" apps/backend/src/super_ai/knowledge docs/knowledge-candidates
```

Expected: pytest PASS; `rg` returns no importer or knowledge-card matches and exits 1.

- [ ] **Step 6: Commit**

```powershell
git add benchmarks/agentpy/retrieval/snapshot_knowledge_coverage.yaml apps/backend/src/super_ai/evaluation/knowledge_coverage.py apps/backend/tests/test_snapshot_knowledge_coverage.py
git commit -m "feat: validate evaluator-only snapshot knowledge coverage"
```

### Task 4: Add four answer-free Retrieval queries

**Files:**
- Modify: `benchmarks/agentpy/retrieval/queries.yaml`
- Modify: `apps/backend/tests/test_retrieval_evaluation.py`

**Interfaces:**
- Consumes: `RetrievalQuery` loader validation and the existing 30-card catalog.
- Produces: `RET-L-013`, `RET-O-009`, `RET-A-015`, and `RET-X-009` with fixed labels.

- [ ] **Step 1: Add failing query-identity assertions**

```python
def test_expansion_queries_target_generic_cards_without_snapshot_leakage() -> None:
    by_id = {query.id: query for query in load_retrieval_queries(QUERIES)}
    expected = {
        "RET-L-013": "postgres-deadlock.md",
        "RET-O-009": "redis-maxclients-pressure.md",
        "RET-A-015": "nginx-upstream-timeout.md",
        "RET-X-009": "http-rate-limit-retry-storm.md",
    }
    assert {query_id: by_id[query_id].relevant_documents[0] for query_id in expected} == expected
    assert all("apy-" not in by_id[query_id].query.lower() for query_id in expected)
```

- [ ] **Step 2: Run and confirm RED**

Run the Task 1 Retrieval test command.

Expected: FAIL because the four IDs are absent and the query count is 60.

- [ ] **Step 3: Append the reviewed queries**

```yaml
  - {id: RET-L-013, type: log_signal, query: "ERROR deadlock detected，SQLSTATE 40P01；并发事务的资源获取次序相反，如何确认等待环而不是普通长锁？", relevant_documents: [postgres-deadlock.md], acceptable_top_k: 3, forbidden_top_one: [postgres-slow-query-lock-wait.md], source_type: public-symptom-rewrite, review_status: reviewed, expected_no_answer: false}
  - {id: RET-O-009, type: operator_perturbation, query: "redis 新连接一直提示 max number of clients reached，服务还活着，咋区分连接泄漏和机器 fd 不够", relevant_documents: [redis-maxclients-pressure.md], acceptable_top_k: 3, forbidden_top_one: [host-file-descriptor-exhaustion.md], source_type: project-synthesized, review_status: reviewed, expected_no_answer: false}
  - {id: RET-A-015, type: ambiguous_symptom, query: "入口能很快连到后端，但等到网关期限才返回 504，怎样证明耗时发生在等待响应阶段？", relevant_documents: [nginx-upstream-timeout.md], acceptable_top_k: 3, forbidden_top_one: [nginx-upstream-502.md], source_type: project-synthesized, review_status: reviewed, expected_no_answer: false}
  - {id: RET-X-009, type: cross_component_distractor, query: "429 上升后总请求量反而继续放大，下游健康且保护规则已命中，应该检查攻击流量还是客户端重试策略？", relevant_documents: [http-rate-limit-retry-storm.md], acceptable_top_k: 3, forbidden_top_one: [service-circuit-breaker-degradation.md], source_type: project-synthesized, review_status: reviewed, expected_no_answer: false}
```

- [ ] **Step 4: Run Retrieval and knowledge safety tests**

Run:

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_retrieval_evaluation.py tests/test_snapshot_knowledge_coverage.py tests/test_knowledge_candidate_safety.py -q -p no:cacheprovider --basetemp=var/pytest-plan-retrieval-64
```

Expected: PASS with 64 queries, 58 answerable probes, 6 no-answer probes, and 30 cards.

- [ ] **Step 5: Commit**

```powershell
git add benchmarks/agentpy/retrieval/queries.yaml apps/backend/tests/test_retrieval_evaluation.py
git commit -m "feat: expand retrieval benchmark to sixty four queries"
```

### Task 5: Complete offline verification and documentation

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Test: all Task 1–4 files

**Interfaces:**
- Consumes: the completed ten-scenario, thirty-card, sixty-four-query corpus.
- Produces: auditable documentation and an offline regression result; it does not claim a real Retrieval score.

- [ ] **Step 1: Document corpus counts and the evaluator-only mapping boundary**

Add a dated section containing:

```markdown
### 2026-08-14 Snapshot/Retrieval corpus expansion

- Snapshot fixtures: 10 (`APY-002`, `APY-003`, `APY-006`, `APY-007`, `APY-011`–`APY-016`).
- Generic knowledge cards: 30; no scenario-specific answer card was added.
- Retrieval queries: 64 (58 answerable, 6 no-answer probes).
- `snapshot_knowledge_coverage.yaml` is evaluator-only and is excluded from import, prompts, artifacts, and reports.
- The historical 60-query real Retrieval result remains the current measured baseline until the validation plan executes the 64-query run.
```

- [ ] **Step 2: Run the focused offline regression**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pytest tests/test_evaluation_scenarios.py tests/test_retrieval_evaluation.py tests/test_snapshot_knowledge_coverage.py tests/test_knowledge_candidate_safety.py -q -p no:cacheprovider --basetemp=var/pytest-plan-snapshot-final
```

Expected: PASS with no skips.

- [ ] **Step 3: Run static checks for changed Python files**

```powershell
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m ruff check src/super_ai/evaluation/knowledge_coverage.py tests/test_snapshot_knowledge_coverage.py tests/test_evaluation_scenarios.py tests/test_retrieval_evaluation.py
& 'D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe' -m pyright src/super_ai/evaluation/knowledge_coverage.py tests/test_snapshot_knowledge_coverage.py
```

Expected: Ruff reports `All checks passed!`; Pyright reports 0 errors.

- [ ] **Step 4: Audit the diff and commit**

```powershell
git diff --check
git status --short
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record expanded benchmark corpus"
```

Expected: `git diff --check` emits no output; no runtime report, secret, cache, or `var` file is staged.

## Self-review

- Spec coverage: Tasks 1–2 produce ten Snapshot scenarios; Task 3 covers all ten with existing generic cards; Task 4 produces exactly sixty-four queries; Task 5 records but does not fabricate real metrics.
- Placeholder scan: the plan contains no deferred implementation markers or undefined error-handling work.
- Type consistency: the coverage loader signature and dataclass names match in Tasks 3 and 5; Retrieval types remain the existing `RetrievalQuery` contracts.
- Isolation: the only Snapshot-to-card linkage is evaluator-only; no plan step imports it into RAG or exposes it to the Agent.
