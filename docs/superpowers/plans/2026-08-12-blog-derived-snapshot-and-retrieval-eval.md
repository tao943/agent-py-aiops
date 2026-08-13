# Blog-Derived Snapshot and Retrieval Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four answer-isolated PostgreSQL/Redis Snapshot scenarios, strengthen two differential troubleshooting knowledge cards, and build a six-query deterministic Retrieval Eval before any Agent RAG before/after experiment.

**Architecture:** Keep public knowledge, Snapshot observations, and evaluator-only answers in separate directories and loaders. Reuse the existing Snapshot MCP, deterministic diagnosis scorer, tenant-scoped hybrid retrieval tool, and batch importer; add a small retrieval-evaluation domain that scores already-produced structured retrieval results without introducing a new evaluation framework. Fix same-filename overwrite semantics before updating the two live knowledge documents.

**Tech Stack:** Python 3.10+, pytest, PyYAML, FastAPI, SQLAlchemy/PostgreSQL, Milvus, LangChain text splitters, DashScope Embedding/Rerank, OpenSpec CLI.

## Global Constraints

- Do not import `scenario.yaml`, Snapshot responses, `ground_truth.yaml`, provenance, retrieval labels, or scoring rules into RAG.
- Do not run the four-call Agent RAG comparison, Docker Live scenarios, recovery actions, or an LLM Judge in this plan.
- Every new scenario is `agentpy-original`; provenance must distinguish referenced mechanisms from project-synthesized observations and answers.
- Same-family scenarios expose identical neutral titles, alert structures, and public hypotheses.
- Each new scenario requires at least two independently collected evidence milestones, one strong rule-out, and one weak distractor outside the correct causal chain.
- CI remains offline and deterministic; real Embedding/Rerank and knowledge import remain explicit manual operations.
- Do not add a dependency without a separate approval.

## File Map

- `benchmarks/agentpy/scenarios/APY-{002,007,011,012}/`: public scenario, frozen tool responses, evaluator-only oracle, and provenance.
- `benchmarks/agentpy/retrieval/queries.yaml`: six answer-free retrieval queries and document relevance labels.
- `apps/backend/src/super_ai/evaluation/retrieval.py`: retrieval label loader, structured result validation, and deterministic ranking metrics.
- `apps/backend/scripts/run_retrieval_benchmark.py`: manual real-provider retrieval runner and safe JSON report.
- `apps/backend/tests/test_evaluation_scenarios.py`: paired-scenario, leakage, provenance, evidence, and distractor contracts.
- `apps/backend/tests/test_retrieval_evaluation.py`: label and metric unit tests.
- `apps/backend/tests/test_retrieval_benchmark_cli.py`: offline CLI assembly and serialization tests.
- `apps/backend/tests/test_knowledge_documents_api.py`: changed-content same-filename overwrite contract.
- `docs/knowledge-candidates/postgres-pool-exhaustion.md`: PostgreSQL differential troubleshooting card.
- `docs/knowledge-candidates/redis-unavailable.md`: Redis differential troubleshooting card.
- `openspec/changes/add-blog-derived-retrieval-eval/`: proposal, design delta, tasks, and capability specs.

## Reuse Assessment Gate

Requirements are six labeled queries, Recall@1/3, MRR, forbidden-Top-1 rate, citation completeness, and tenant isolation. The project already owns the retrieval DTOs, hybrid vector/BM25/rerank pipeline, citation fields, Snapshot framework, YAML loading patterns, and pytest fakes.

GitHub discovery performed on 2026-08-12:

- Queries: `RAG retrieval evaluation recall MRR language:Python`, `ragas retrieval metrics language:Python`, and `BEIR retrieval evaluation language:Python`.
- Ragas-style repositories target broader generated-answer and model-judge evaluation and would add unnecessary dependency and provider surface.
- BEIR is a useful reference for information-retrieval metric definitions but is too large for six project-owned queries; the GitHub BEIR search request itself failed with `tls: handshake failure`, so this is not claimed as a successful empty search.
- Small GitHub kits found under MIT licenses vary in maturity and duplicate ranking math that is trivial to verify locally.

Decision: **reference only** for standard Recall/MRR definitions; implement a project-owned deterministic scorer over existing `KnowledgeRetrievalToolResult`. No package, service, native binary, or license change.

---

### Task 1: Specify the new scenario and retrieval-evaluation capability

**Files:**
- Create: `openspec/changes/add-blog-derived-retrieval-eval/.openspec.yaml`
- Create: `openspec/changes/add-blog-derived-retrieval-eval/proposal.md`
- Create: `openspec/changes/add-blog-derived-retrieval-eval/design.md`
- Create: `openspec/changes/add-blog-derived-retrieval-eval/tasks.md`
- Create: `openspec/changes/add-blog-derived-retrieval-eval/specs/agentpy-sre-benchmark/spec.md`
- Create: `openspec/changes/add-blog-derived-retrieval-eval/specs/knowledge-retrieval-eval/spec.md`
- Modify: `openspec/changes/add-knowledge-batch-import/specs/document-indexing-jobs/spec.md`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-08-12-blog-derived-snapshot-and-retrieval-eval-design.md`.
- Produces: normative requirements for paired scenarios, answer-free retrieval labels, deterministic metrics, and same-filename overwrite.

- [ ] **Step 1: Create the OpenSpec change metadata and requirements**

Use schema `spec-driven` in `.openspec.yaml`. State these exact requirements:

```text
1. PostgreSQL and Redis paired public scenarios share title, alert and hypotheses.
2. Each oracle requires two evidence milestones and one rule-out.
3. Retrieval labels cannot contain scenario IDs or oracle-only values.
4. Retrieval reports expose Recall@1, Recall@3, MRR, forbiddenTopOneRate and citationCompletenessRate.
5. overwrite=true replaces an active same-filename document inside the same owner and knowledge base even when content changes.
6. overwrite=false preserves the existing conflict behavior for matching content hashes.
```

- [ ] **Step 2: Validate the new OpenSpec change**

Run from repository root:

```powershell
npx --yes @fission-ai/openspec@1.6.0 validate add-blog-derived-retrieval-eval
```

Expected: the change validates with zero failures.

- [ ] **Step 3: Commit the specification**

```powershell
git add openspec/changes/add-blog-derived-retrieval-eval openspec/changes/add-knowledge-batch-import/specs/document-indexing-jobs/spec.md
git commit -m "spec: define blog-derived retrieval evaluation"
```

---

### Task 2: Add paired-scenario contract tests

**Files:**
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Consumes: `load_public_scenario`, `load_scenario_oracle`, `validate_scenario_bundle`, and `SnapshotMcpClient.from_yaml`.
- Produces: regression contracts that the four scenario directories in Task 3 must satisfy.

- [ ] **Step 1: Write failing parametrized scenario tests**

Add constants and tests equivalent to:

```python
PAIRS = (("APY-002", "APY-011"), ("APY-007", "APY-012"))

@pytest.mark.parametrize(("left_id", "right_id"), PAIRS)
def test_new_pairs_share_public_inputs_and_differ_in_oracle(
    left_id: str, right_id: str
) -> None:
    left = load_public_scenario(SCENARIOS / left_id)
    right = load_public_scenario(SCENARIOS / right_id)
    left_oracle = load_scenario_oracle(SCENARIOS / left_id)
    right_oracle = load_scenario_oracle(SCENARIOS / right_id)
    assert left.title == right.title == left.alert["summary"] == right.alert["summary"]
    assert left.alert == right.alert
    assert left.hypotheses == right.hypotheses
    assert left_oracle.primary_cause.mechanism != right_oracle.primary_cause.mechanism

@pytest.mark.parametrize("scenario_id", ("APY-002", "APY-007", "APY-011", "APY-012"))
def test_new_scenario_has_two_milestones_one_rule_out_and_four_tools(
    scenario_id: str,
) -> None:
    root = SCENARIOS / scenario_id
    public = load_public_scenario(root)
    oracle = load_scenario_oracle(root)
    client = SnapshotMcpClient.from_yaml(root / public.snapshot_file)
    assert len(oracle.required_evidence) >= 2
    assert len(oracle.required_rule_outs) == 1
    assert len(asyncio.run(client.discover_tools())) == 4
```

Also serialize each public scenario and assert it excludes the oracle mechanism, trigger, and every required milestone ID.

- [ ] **Step 2: Run and verify missing-directory failures**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_evaluation_scenarios.py -q
```

Expected: FAIL because `APY-002`, `APY-007`, `APY-011`, and `APY-012` do not exist.

- [ ] **Step 3: Commit only after Task 3 turns the tests green**

Do not commit the red test separately; include it with the scenario files in Task 3 so the branch never ends a commit with broken required tests.

---

### Task 3: Implement four answer-isolated Snapshot scenarios

**Files:**
- Create: `benchmarks/agentpy/scenarios/APY-002/{scenario.yaml,ground_truth.yaml,provenance.yaml,snapshot/tool_responses.yaml}`
- Create: `benchmarks/agentpy/scenarios/APY-011/{scenario.yaml,ground_truth.yaml,provenance.yaml,snapshot/tool_responses.yaml}`
- Create: `benchmarks/agentpy/scenarios/APY-007/{scenario.yaml,ground_truth.yaml,provenance.yaml,snapshot/tool_responses.yaml}`
- Create: `benchmarks/agentpy/scenarios/APY-012/{scenario.yaml,ground_truth.yaml,provenance.yaml,snapshot/tool_responses.yaml}`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Consumes: existing scenario YAML schemas and exact-argument Snapshot MCP format.
- Produces: four loadable Snapshot bundles for the existing runner and scorer.

- [ ] **Step 1: Create the PostgreSQL public pair**

Both public files use title and alert summary `Application requests are timing out while waiting for a PostgreSQL connection.` and identical hypotheses:

```yaml
hypotheses:
  - id: slow_transaction_pool_exhaustion
    description: Database work holds pooled connections for an extended period.
  - id: application_connection_lifecycle_failure
    description: The application does not return borrowed connections reliably.
  - id: traffic_capacity_mismatch
    description: Legitimate concurrency exceeds the configured pool capacity.
```

Use `InspectPostgres`, `InspectDatabasePool`, `GetServiceMetrics`, and `GetDeploymentChanges`. `APY-002` observations show an old active transaction plus lock waits, checked-out connections blocked on database work, and only a small traffic rise. `APY-011` shows no long transaction or lock wait, diverging checkout/checkin totals, one request path retaining connections, and connection count near the limit.

- [ ] **Step 2: Create the Redis public pair**

Both public files use title and alert summary `Application requests to Redis are failing.` and identical hypotheses:

```yaml
hypotheses:
  - id: redis_service_unavailable
    description: The Redis server is not accepting client connections.
  - id: client_pool_recovery_failure
    description: The application client pool cannot recover usable connections.
  - id: network_path_failure
    description: The network path between the application and Redis is failing.
```

Use `InspectRedis`, `InspectRedisClientPool`, `GetServiceMetrics`, and `GetDeploymentChanges`. `APY-007` observations show Redis stopped/no listener and connection refused while the client pool itself is not saturated. `APY-012` shows Redis healthy/PING success, stale client connections with waiting borrowers, a reachable network path, and moderately elevated but non-critical Redis memory.

- [ ] **Step 3: Create evaluator-only oracles**

Use these normalized mechanisms and rule-outs:

```text
APY-002: component=postgresql, mechanism=slow_transaction_pool_exhaustion,
         rule_out=application_connection_lifecycle_failure
APY-011: component=checkout-service, mechanism=application_connection_lifecycle_failure,
         rule_out=slow_transaction_pool_exhaustion
APY-007: component=redis, mechanism=redis_service_unavailable,
         rule_out=client_pool_recovery_failure
APY-012: component=checkout-service, mechanism=client_pool_recovery_failure,
         rule_out=redis_service_unavailable
```

Each oracle has two required evidence milestones whose alternatives reference evidence IDs from different tools. The causal chain excludes the weak distractor.

- [ ] **Step 4: Create provenance files**

Each file declares `type: agentpy-original`, `created: 2026-08-12`, the transformation statement, exact URLs already cited by the corresponding knowledge card, and the source license when known. Do not claim OpenSRE derivation.

- [ ] **Step 5: Run scenario and Snapshot tests**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_evaluation_scenarios.py tests/test_snapshot_evaluation_tools.py tests/test_evaluation_scoring.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the scenarios and their contracts**

```powershell
git add benchmarks/agentpy/scenarios/APY-002 benchmarks/agentpy/scenarios/APY-007 benchmarks/agentpy/scenarios/APY-011 benchmarks/agentpy/scenarios/APY-012 apps/backend/tests/test_evaluation_scenarios.py
git commit -m "test: add postgres and redis differential snapshots"
```

---

### Task 4: Rewrite the two differential troubleshooting cards

**Files:**
- Modify: `docs/knowledge-candidates/postgres-pool-exhaustion.md`
- Modify: `docs/knowledge-candidates/redis-unavailable.md`
- Create: `apps/backend/tests/test_knowledge_candidate_safety.py`

**Interfaces:**
- Consumes: approved card structure and the existing Markdown batch-import directory.
- Produces: two reviewed, answer-free, heading-structured RAG documents.

- [ ] **Step 1: Write failing content-safety tests**

Test both files for required headings and forbidden benchmark tokens:

```python
@pytest.mark.parametrize("filename", ("postgres-pool-exhaustion.md", "redis-unavailable.md"))
def test_differential_card_has_reviewed_structure_and_no_benchmark_answers(
    filename: str,
) -> None:
    text = (KNOWLEDGE / filename).read_text(encoding="utf-8")
    for heading in ("## 适用现象", "## 候选原因", "## 建议证据", "## 如何区分", "## 安全恢复边界", "## 恢复后验证", "## 来源"):
        assert heading in text
    forbidden = ("APY-", "ground_truth", "evidence_id", "benchmark_container", "client_pool_recovery_failure")
    assert not any(token in text for token in forbidden)
```

- [ ] **Step 2: Run and verify missing-heading failures**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py -q
```

Expected: FAIL because the existing short cards lack the required section structure.

- [ ] **Step 3: Rewrite both cards**

For PostgreSQL, explain slow transactions/locks, connection lifecycle anomalies, and capacity mismatch. For Redis, explain server availability, client reconnection/pool state, and network path failures. Describe evidence interpretation as uncertainty-aware comparisons, not single-field answer rules. Preserve URLs, add licenses/unknown-license annotations honestly, and keep Snapshot values and identifiers out.

- [ ] **Step 4: Run safety and chunking tests**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_candidate_safety.py tests/test_document_chunking.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the reviewed cards**

```powershell
git add docs/knowledge-candidates/postgres-pool-exhaustion.md docs/knowledge-candidates/redis-unavailable.md apps/backend/tests/test_knowledge_candidate_safety.py
git commit -m "docs: strengthen postgres and redis troubleshooting cards"
```

---

### Task 5: Add Retrieval Eval labels and deterministic scoring

**Files:**
- Create: `benchmarks/agentpy/retrieval/queries.yaml`
- Create: `apps/backend/src/super_ai/evaluation/retrieval.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Create: `apps/backend/tests/test_retrieval_evaluation.py`

**Interfaces:**
- Produces: `RetrievalQuery`, `RetrievalQueryResult`, `RetrievalEvaluationResult` dataclasses.
- Produces: `load_retrieval_queries(path: Path) -> tuple[RetrievalQuery, ...]`.
- Produces: `evaluate_retrieval(results: Sequence[RetrievalQueryResult]) -> RetrievalEvaluationResult`.
- Consumes later: structured `KnowledgeRetrievalToolResult` mapped to source filenames and citations.

- [ ] **Step 1: Write failing label-loader and metric tests**

Cover duplicate IDs, empty relevance lists, `acceptable_top_k` outside 1..5, forbidden answer tokens, exact Recall@1/3 and MRR math, forbidden Top-1, and citation completeness. A representative perfect result must assert:

```python
assert report.recall_at_1 == 1.0
assert report.recall_at_3 == 1.0
assert report.mrr == 1.0
assert report.forbidden_top_one_rate == 0.0
assert report.citation_completeness_rate == 1.0
```

Citation completeness requires non-empty `chunk_id`, `document_id`, `knowledge_base_id`, and non-`None` `vector_score` and `rerank_score` for every returned citation.

- [ ] **Step 2: Run and verify import failure**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py -q
```

Expected: FAIL because `super_ai.evaluation.retrieval` does not exist.

- [ ] **Step 3: Implement immutable contracts, strict loader, and scorer**

Keep scoring pure: it accepts recorded ranked filenames and citation DTO fields and never calls models, Milvus, PostgreSQL, or the oracle loader. Average per-query binary Recall@K and reciprocal rank; calculate forbidden Top-1 over all queries; return zero-safe values for an empty run only by rejecting empty input with `ValueError`.

- [ ] **Step 4: Add six labels**

Use IDs `RET-PG-001..003` and `RET-REDIS-001..003`. For each family include one explicit symptom query, one vague operator query, and one server-healthy/strong-alternative query. Relevant documents are only `postgres-pool-exhaustion.md` or `redis-unavailable.md`; queries contain no `APY-`, normalized mechanism, trigger, or evidence ID.

- [ ] **Step 5: Run retrieval-evaluation tests and static checks**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py -q
.venv\Scripts\python.exe -m ruff check src/super_ai/evaluation/retrieval.py tests/test_retrieval_evaluation.py
.venv\Scripts\python.exe -m pyright src/super_ai/evaluation/retrieval.py tests/test_retrieval_evaluation.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit labels and scorer**

```powershell
git add benchmarks/agentpy/retrieval apps/backend/src/super_ai/evaluation apps/backend/tests/test_retrieval_evaluation.py
git commit -m "feat: score deterministic knowledge retrieval evals"
```

---

### Task 6: Add a safe manual real-retrieval runner

**Files:**
- Create: `apps/backend/scripts/run_retrieval_benchmark.py`
- Create: `apps/backend/tests/test_retrieval_benchmark_cli.py`
- Modify: `apps/backend/README.md`

**Interfaces:**
- Consumes: `load_retrieval_queries`, `KnowledgeRetrievalTool`, configured provider, Milvus store, owner user ID, and knowledge-base ID.
- Produces CLI arguments `--owner-user-id`, `--knowledge-base-id`, `--queries`, `--output`, and `--config`.
- Produces safe UTF-8 JSON containing query ID, ranked source filenames, chunk/document/KB IDs, vector/rerank scores, aggregate metrics, model names, and duration; never content, credentials, or raw configuration.

- [ ] **Step 1: Write failing parser and offline assembly tests**

Patch provider/vector-store builders with fakes and assert that every query runs with the exact owner and knowledge-base scope, report output excludes chunk content and secret sentinels, and a mismatched tenant result cannot enter scoring.

- [ ] **Step 2: Run and verify missing-script failure**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_benchmark_cli.py -q
```

Expected: FAIL because the script does not exist.

- [ ] **Step 3: Implement the manual runner**

Require explicit owner and KB IDs; do not infer them from a benchmark run ID. Run queries sequentially to control quota. Map `hit.source` to a basename before scoring, validate every hit/citation belongs to the requested owner and KB, save model names but omit base URL credentials and all document content, and return exit code 0 only when every target enters Top 3 and isolation/citation contracts pass.

- [ ] **Step 4: Document manual execution**

Add this command without embedding actual IDs or secrets:

```powershell
uv run python scripts/run_retrieval_benchmark.py --owner-user-id <owner-id> --knowledge-base-id <kb-id> --output var/benchmarks/retrieval-v1.json
```

State explicitly that it consumes Embedding/Rerank quota and is not part of ordinary CI.

- [ ] **Step 5: Run CLI tests and help**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_retrieval_benchmark_cli.py -q
.venv\Scripts\python.exe scripts/run_retrieval_benchmark.py --help
```

Expected: tests pass and help exits 0 with all five arguments.

- [ ] **Step 6: Commit the runner**

```powershell
git add apps/backend/scripts/run_retrieval_benchmark.py apps/backend/tests/test_retrieval_benchmark_cli.py apps/backend/README.md
git commit -m "feat: add manual retrieval benchmark runner"
```

---

### Task 7: Make changed-content same-filename imports idempotent

**Files:**
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/test_knowledge_documents_api.py`
- Modify: `apps/backend/tests/test_import_knowledge_batch.py`

**Interfaces:**
- Produces: `KnowledgeDocumentRepository.find_active_by_filename(owner_user_id: str, knowledge_base_id: str, filename: str) -> KnowledgeDocumentRecord | None`.
- Upload behavior: with `overwrite=true`, delete vectors and soft-delete an active same-hash document and/or active same-filename document before creating exactly one replacement; return the replaced ID as `duplicateOfDocumentId` for compatibility.
- Preserves: `overwrite=false` only conflicts on an existing scoped content hash, matching the current API contract.

- [ ] **Step 1: Write failing API integration tests**

Upload `runbook.md` with body `version one`, then upload `runbook.md` with `version two` and `overwrite=true`. Assert HTTP 201, a new document ID, `duplicateOfDocumentId` equals the first ID, the first record is deleted, and the active document list contains exactly one `runbook.md`. Also upload changed content with a different filename and assert it does not replace the first document.

- [ ] **Step 2: Run and verify duplicate-active-document failure**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_documents_api.py -q
```

Expected: FAIL because changed-content same-filename upload currently creates a second active document.

- [ ] **Step 3: Add the scoped filename repository query**

Implement the protocol and SQLAlchemy query with exact owner, knowledge base, filename, active status, and `deleted_at IS NULL` filters. If legacy data has more than one active match, select deterministically by newest `uploaded_at` then ID; the upload service will only replace that selected record and a separate diagnostic query will expose remaining legacy duplicates rather than deleting them silently.

- [ ] **Step 4: Update upload overwrite selection**

Resolve `same_hash` first and `same_filename` only when `overwrite` is true. Choose one replacement record, avoiding double deletion when both lookups return the same ID. Preserve conflict behavior when `same_hash` exists and overwrite is false. Delete scoped vectors before marking the replacement deleted, then create the new document.

- [ ] **Step 5: Add importer regression coverage**

Assert the batch importer continues to submit `overwrite=true` and reports the replacement document/task IDs without printing the old content or credentials.

- [ ] **Step 6: Run document and import tests**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_knowledge_documents_api.py tests/test_document_indexing_api.py tests/test_import_knowledge_batch.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit idempotent overwrite**

```powershell
git add apps/backend/src/super_ai/memory/repositories.py apps/backend/src/super_ai/memory/sqlalchemy.py apps/backend/src/super_ai/api/app.py apps/backend/tests/test_knowledge_documents_api.py apps/backend/tests/test_import_knowledge_batch.py
git commit -m "fix: replace changed knowledge documents by filename"
```

---

### Task 8: Verify and update only the two knowledge documents

**Files:**
- Runtime output only: `apps/backend/var/knowledge-import/` (ignored)
- No committed source changes unless verification finds a defect, which requires a new failing test and separate commit.

**Interfaces:**
- Consumes: existing `scripts/import_knowledge_batch.py`, local ignored configuration, PostgreSQL, Milvus, backend, Embedding provider.
- Produces: two succeeded import results and read-only database/vector evidence.

- [ ] **Step 1: Create a temporary two-file source directory**

Use a repository-scoped ignored directory under `apps/backend/var/knowledge-import/reviewed-two/` and copy only the PostgreSQL and Redis cards into it. Do not edit or remove the seven tracked source cards.

- [ ] **Step 2: Run dry-run**

```powershell
cd apps/backend
uv run python scripts/import_knowledge_batch.py --source-dir var/knowledge-import/reviewed-two --dry-run
```

Expected JSON: exactly two filenames, `dryRun: true`, and no HTTP request.

- [ ] **Step 3: Inspect existing active documents before mutation**

Query PostgreSQL by the configured owner, KB, and the two filenames. Record active IDs and count in an ignored JSON evidence file. If more than one active same-filename row already exists, stop; report the legacy duplicates instead of deleting them automatically.

- [ ] **Step 4: Run the real two-file import once**

```powershell
uv run python scripts/import_knowledge_batch.py --source-dir var/knowledge-import/reviewed-two --continue-on-error
```

Expected: `succeeded: 2`, `failed: 0`. This consumes Embedding quota but does not run Agent chat completions.

- [ ] **Step 5: Verify PostgreSQL and Milvus evidence**

Confirm one active document per filename, both document statuses `indexed`, both latest index tasks `succeeded`, replaced IDs soft-deleted, and at least one owner/tenant/KB/document-scoped Milvus chunk for each new ID. Verify no chunk remains for each replaced document ID.

- [ ] **Step 6: Run the real Retrieval Eval once**

Use the verified owner and KB IDs with `run_retrieval_benchmark.py`. Expected: all six targets enter Top 3, citation completeness is 1.0, forbidden Top-1 rate is 0.0, and output contains no text content or secrets. If a target misses, record the actual report and stop; do not tune labels or scores to force a pass.

---

### Task 9: Complete regression, documentation, and OpenSpec tasks

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/add-blog-derived-retrieval-eval/tasks.md`
- Modify: `apps/backend/tests/test_local_development_docs.py`

**Interfaces:**
- Consumes: all completed tasks and verified real retrieval report.
- Produces: documented six-scenario catalog, Retrieval Eval commands/limits, and completed OpenSpec checklist.

- [ ] **Step 1: Add failing documentation assertions**

Require the DomainBench document to mention `APY-002`, `APY-007`, `APY-011`, `APY-012`, `run_retrieval_benchmark.py`, `Recall@3`, and the statement that Retrieval Eval does not score diagnosis correctness.

- [ ] **Step 2: Run and verify documentation failure**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_local_development_docs.py -q
```

Expected: FAIL before documentation is updated.

- [ ] **Step 3: Update DomainBench operations documentation**

Document the six Snapshot scenarios, knowledge/answer isolation, retrieval metrics, manual real-provider execution, idempotent two-card update, and the deferred Agent RAG comparison. Do not claim the four new scenarios have Live support.

- [ ] **Step 4: Run focused verification**

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_evaluation_scenarios.py tests/test_snapshot_evaluation_tools.py tests/test_evaluation_scoring.py tests/test_retrieval_evaluation.py tests/test_retrieval_benchmark_cli.py tests/test_knowledge_candidate_safety.py tests/test_knowledge_documents_api.py tests/test_import_knowledge_batch.py tests/test_local_development_docs.py -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pyright
```

Expected: all commands exit 0.

- [ ] **Step 5: Run OpenSpec and ordinary backend verification**

```powershell
cd ../..
npx --yes @fission-ai/openspec@1.6.0 validate --all
cd apps/backend
.venv\Scripts\python.exe -m pytest
```

Expected: OpenSpec exits 0. Full pytest must complete with zero failures; if local infrastructure makes it exceed the bounded local window, preserve the focused green results and require the GitHub Actions `backend-tests` job before merge.

- [ ] **Step 6: Mark OpenSpec tasks complete only from evidence**

Change each checkbox to `[x]` only when its command or runtime evidence exists. Leave real-import or real-retrieval tasks unchecked if external services or quota prevented execution.

- [ ] **Step 7: Commit documentation and completion state**

```powershell
git add docs/aiops/agentpy-domainbench.md apps/backend/tests/test_local_development_docs.py openspec/changes/add-blog-derived-retrieval-eval/tasks.md
git commit -m "docs: explain snapshot and retrieval evaluation workflow"
```

## Plan Self-Review

- Spec coverage: four scenarios, paired public inputs, two milestones, strong/weak distractors, provenance, two differential cards, six retrieval labels, Recall/MRR/citation/isolation metrics, manual real retrieval, overwrite safety, PostgreSQL/Milvus verification, and deferred Agent comparison all map to explicit tasks.
- Reuse coverage: local boundaries inspected; GitHub queries and their license/fit outcome are recorded before implementation; no dependency adoption is planned.
- Type consistency: `RetrievalQuery`, `RetrievalQueryResult`, `RetrievalEvaluationResult`, `load_retrieval_queries`, and `evaluate_retrieval` are defined in Task 5 and consumed by Task 6.
- Safety: no secret values, destructive legacy cleanup, oracle-to-RAG path, automatic Live action, or model call in CI is introduced.
- Placeholder scan: every implementation step is concrete; runtime IDs remain explicit CLI parameters because committing real owner identifiers would violate tenant and credential boundaries.
