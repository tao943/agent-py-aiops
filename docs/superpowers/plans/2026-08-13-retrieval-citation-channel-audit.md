# Retrieval Citation Channel Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Citation completeness validate the channels that actually recalled each hit while preserving the existing hybrid retrieval, RRF, rerank, and ranking behavior.

**Architecture:** Extend the pure evaluation DTO with existing retrieval provenance fields and derive completeness and channel participation from rank/score consistency. The benchmark runner maps those fields without altering retrieval, then emits content-free per-hit provenance and aggregate diagnostic coverage rates.

**Tech Stack:** Python 3.12, dataclasses, pytest, Ruff, Pyright, OpenSpec

## Global Constraints

- Do not change vector recall, BM25 recall, RRF, rerank, query labels, ranking, or pass thresholds.
- Do not fabricate zero scores or discard single-channel hits.
- Every returned hit remains in the Citation completeness denominator.
- `rrf_score`, `rerank_rank`, and `rerank_score` are required for every complete audit.
- Channel coverage rates are diagnostic only and do not affect `_passes`.
- Reuse existing retrieval provenance fields; add no dependency.
- Execute inline in the current session; do not start subagents.

---

### Task 1: Channel-aware Citation audit and metrics

**Files:**
- Modify: `apps/backend/tests/test_retrieval_evaluation.py`
- Modify: `apps/backend/src/super_ai/evaluation/retrieval.py`

**Interfaces:**
- Consumes: existing `RetrievalQueryResult.citations` and retrieval rank/score provenance.
- Produces: expanded `RetrievalCitationAudit`, `retrieval_channels`, and three coverage fields on `RetrievalEvaluationResult`.

- [ ] **Step 1: Write failing audit semantics tests**

Add a helper that constructs a fully traced audit, then parameterized cases for BM25-only, vector-only, hybrid, rank/score contradictions, no recall channel, missing RRF, and missing rerank provenance. Assert the first three are complete and all invalid cases are incomplete.

- [ ] **Step 2: Write failing aggregate coverage test**

Evaluate one vector-only, one BM25-only, and one hybrid hit. Assert vector and BM25 coverage are `2 / 3`, hybrid coverage is `1 / 3`, and Citation completeness is `1.0`.

- [ ] **Step 3: Run RED tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py -q
```

Expected: FAIL because the DTO does not accept ranks/BM25/RRF and the result has no channel coverage fields.

- [ ] **Step 4: Implement minimal channel-aware audit**

Add optional rank/score fields to `RetrievalCitationAudit`, a rank-derived `retrieval_channels` property, strict rank/score consistency checks, the at-least-one-channel rule, and required fusion/rerank fields. Add diagnostic aggregate coverage rates using the same Citation denominator.

- [ ] **Step 5: Update existing evaluation fixtures and run GREEN tests**

Update positional/legacy fixtures to explicitly provide valid provenance, keeping the existing ranking expectations unchanged. Re-run the Task 1 command and expect PASS.

- [ ] **Step 6: Commit Task 1**

```powershell
git add apps/backend/src/super_ai/evaluation/retrieval.py apps/backend/tests/test_retrieval_evaluation.py
git commit -m "fix: audit retrieval citation channels"
```

### Task 2: Runner provenance mapping and safe report

**Files:**
- Modify: `apps/backend/tests/test_retrieval_benchmark_cli.py`
- Modify: `apps/backend/scripts/run_retrieval_benchmark.py`

**Interfaces:**
- Consumes: `KnowledgeRetrievalHit` and `KnowledgeRetrievalCitationSource` rank/score fields.
- Produces: complete audit mapping, per-hit `retrievalChannels`, and diagnostic metric fields.

- [ ] **Step 1: Write failing runner mapping tests**

Make the fake tool return valid hybrid provenance and assert each safe hit contains `vectorRank`, `bm25Rank`, `rerankRank`, `vectorScore`, `bm25Score`, `rrfScore`, `rerankScore`, and `retrievalChannels`. Add a BM25-only fake and assert it remains complete with `retrievalChannels == ["bm25"]`.

- [ ] **Step 2: Extend the missing-Citation regression test**

Keep the existing assertion that omitted Citation yields completeness `0.0`, proving fallback hit identities do not make missing Citation provenance complete.

- [ ] **Step 3: Run RED CLI tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_benchmark_cli.py -q
```

Expected: FAIL because runner output and audit construction do not map the new fields.

- [ ] **Step 4: Implement minimal runner mapping**

Map audit provenance from the matching Citation; when Citation is absent, retain only fallback stable identities and set every provenance field to `None`. Emit all provenance fields from hits, derive `retrievalChannels` from ranks, and expose the three coverage rates under `metrics`.

- [ ] **Step 5: Run GREEN CLI and combined tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py tests/test_retrieval_benchmark_cli.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```powershell
git add apps/backend/scripts/run_retrieval_benchmark.py apps/backend/tests/test_retrieval_benchmark_cli.py
git commit -m "feat: report retrieval channel provenance"
```

### Task 3: Specifications, documentation, and focused verification

**Files:**
- Modify: `openspec/changes/expand-rag-retrieval-benchmark/specs/knowledge-retrieval-eval/spec.md`
- Modify: `openspec/changes/expand-rag-retrieval-benchmark/design.md`
- Modify: `docs/superpowers/specs/2026-08-13-thirty-card-rag-and-retrieval-benchmark-design.md`
- Modify: `docs/aiops/agentpy-domainbench.md`
- Test: relevant retrieval tests and project validation commands

**Interfaces:**
- Consumes: implemented Citation audit semantics and report fields.
- Produces: non-contradictory normative and explanatory documentation.

- [ ] **Step 1: Replace obsolete vector-required wording**

Specify stable identity, pairwise vector/BM25 rank-score consistency, at least one recall channel, required RRF/rerank provenance, missing-Citation denominator behavior, and diagnostic-only coverage rates.

- [ ] **Step 2: Run focused tests and static checks**

From `apps/backend`, run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retrieval_evaluation.py tests/test_retrieval_benchmark_cli.py tests/test_knowledge_retrieval_tool.py -q
.venv\Scripts\python.exe -m ruff check src/super_ai/evaluation/retrieval.py scripts/run_retrieval_benchmark.py tests/test_retrieval_evaluation.py tests/test_retrieval_benchmark_cli.py
.venv\Scripts\python.exe -m pyright src/super_ai/evaluation/retrieval.py scripts/run_retrieval_benchmark.py tests/test_retrieval_evaluation.py tests/test_retrieval_benchmark_cli.py
```

Expected: all commands PASS.

- [ ] **Step 3: Validate OpenSpec**

Run the repository's existing OpenSpec validation command against `expand-rag-retrieval-benchmark`; expect PASS.

- [ ] **Step 4: Inspect the diff for ranking changes and secrets**

Confirm no hybrid retrieval implementation, query labels, scores, credentials, tenant identifiers, or ignored runtime reports were changed or staged.

- [ ] **Step 5: Commit Task 3**

```powershell
git add openspec/changes/expand-rag-retrieval-benchmark docs/superpowers/specs/2026-08-13-thirty-card-rag-and-retrieval-benchmark-design.md docs/aiops/agentpy-domainbench.md
git commit -m "docs: define citation channel consistency"
```

## Self-review

- Spec coverage: audit semantics, runner mapping, missing-Citation denominator, safe report, diagnostic coverage, documentation, and verification each map to a task.
- Type consistency: audit and report field names match the approved design.
- Scope: no retrieval algorithm, datastore, model, label, or dependency changes are included.
- Placeholder scan: no deferred implementation requirements remain.
