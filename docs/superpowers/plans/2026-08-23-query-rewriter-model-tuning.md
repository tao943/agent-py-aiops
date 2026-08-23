# Query Rewriter Model Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route query rewriting through `qwen3.7-flash`, use a 25-second bounded rewrite timeout, accept Recall@3 ≥ 0.90 for the 10-case A/B, and diagnose rather than relax Forbidden Top-1.

**Architecture:** Extend the existing LLM provider with an optional, backward-compatible Rewriter model profile and factory. Both production Conversation and the real A/B CLI consume that factory; retrieval, Agent, Validator, Embedding and Rerank remain unchanged.

**Tech Stack:** Python 3.11+, Pydantic/dataclasses, LangChain OpenAI-compatible provider, pytest, Ruff, Pyright.

## Global Constraints

- Rewriter model is `qwen3.7-flash` and shares the existing provider transport and API key.
- Rewriter timeout is 25.0 seconds; failure still falls back to the original query.
- Recall@3 threshold is 0.90; Forbidden Top-1 remains at most 0.05.
- Do not change benchmark labels, corpus, Agent model, Validator model, Embedding model or Rerank model.
- Do not run the full pytest suite.
- Do not add dependencies.

---

### Task 1: Independent Rewriter Model Configuration

**Files:**
- Modify: `apps/backend/src/super_ai/llm/config.py`
- Modify: `apps/backend/src/super_ai/llm/provider.py`
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Modify: `apps/backend/src/super_ai/chat/query_rewrite.py`
- Modify: `config/user.project.json`
- Test: `apps/backend/tests/test_llm_provider.py`
- Test: `apps/backend/tests/test_stream_rag_chat_api.py`
- Test: `apps/backend/tests/test_chat_query_rewrite.py`

**Interfaces:**
- Consumes: existing `LlmProviderConfig`, `QwenOpenAIProvider`, `StructuredQueryRewriter` and `LangChainChatAgentRunner`.
- Produces: `query_rewrite_model`, `query_rewrite_structured_output_method`, `create_query_rewrite_model()` and a 25.0-second default rewrite timeout.

- [ ] **Step 1: Write failing provider and runtime tests**

  Assert an explicit `queryRewriteModel` selects its capability profile, `create_query_rewrite_model()` replaces only `chat_model`, missing configuration falls back to `chatModel`, production Conversation uses the Rewriter factory, legacy providers without the new factory fall back to `create_chat_model()`, and the default `StructuredQueryRewriter` timeout is 25.0 seconds. Retain a hanging-model test with an injected short timeout that proves cancellation, original-query fallback, `rewrite_timeout`, and `modelCallCount=1`.

- [ ] **Step 2: Run focused tests and observe the expected failures**

  Run: `uv run pytest tests/test_llm_provider.py tests/test_stream_rag_chat_api.py tests/test_chat_query_rewrite.py -q`

  Expected: new assertions fail because the independent Rewriter factory and 25-second default do not exist.

- [ ] **Step 3: Implement the minimal backward-compatible provider extension**

  Parse `queryRewriteModel` with fallback to `chatModel`, validate it against `modelCapabilities`, expose its structured-output method, and construct the model by replacing only `chat_model`. Add centralized compatibility helpers that use the dedicated provider members when present and otherwise fall back to `create_chat_model()` and `structured_output_method`; update production Conversation to use those helpers. Add the `qwen3.7-flash` capability profile to local project configuration without exposing or changing the API key.

- [ ] **Step 4: Run focused tests**

  Run: `uv run pytest tests/test_llm_provider.py tests/test_stream_rag_chat_api.py tests/test_chat_query_rewrite.py -q`

  Expected: PASS.

### Task 2: A/B Gate and Safe Artifact Metadata

**Files:**
- Modify: `apps/backend/scripts/run_query_rewrite_benchmark.py`
- Test: `apps/backend/tests/test_query_rewrite_benchmark.py`
- Modify: `docs/superpowers/reports/2026-08-23-conversation-query-rewrite-retrieval-ab.md`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: `LlmProvider.create_query_rewrite_model()` and query-rewrite A/B metrics.
- Produces: immutable artifacts that record `queryRewriteModel`, apply Recall@3 ≥ 0.90, retain Forbidden Top-1 ≤ 0.05, and contain a content-safe corpus fingerprint plus document/chunk counts in the terminal result payload without changing stable run identity.

- [ ] **Step 1: Write failing benchmark tests**

  Assert Recall@3 exactly 0.90 passes, 0.89 fails, Forbidden Top-1 0.10 still fails, the CLI uses the Rewriter factory, model metadata distinguishes `chatModel` from `queryRewriteModel`, and terminal corpus metadata contains only an irreversible fingerprint and counts without changing running-envelope metadata.

- [ ] **Step 2: Run the benchmark tests and observe the expected failures**

  Run: `uv run pytest tests/test_query_rewrite_benchmark.py -q`

  Expected: new threshold/factory/metadata assertions fail.

- [ ] **Step 3: Implement the gate and CLI wiring**

  Change only the 10-case query-rewrite Recall@3 threshold to 0.90, keep Forbidden Top-1 at 0.05, create the Rewriter through its compatibility helper, pass 25.0 seconds explicitly, and record the dedicated model name in artifact metadata. Derive a deterministic SHA-256 corpus fingerprint from sorted active indexed chunk identifiers/source/version metadata and store only the digest, document count and chunk count.

- [ ] **Step 4: Run focused quality checks**

  Run: `uv run pytest tests/test_query_rewrite_benchmark.py tests/test_llm_provider.py tests/test_stream_rag_chat_api.py tests/test_chat_query_rewrite.py -q`

  Run: `uv run ruff check src tests scripts/run_query_rewrite_benchmark.py`

  Run: `uv run pyright`

  Expected: all commands exit 0.

### Task 3: Real Fixed-Variable A/B and Forbidden Diagnosis

**Files:**
- Modify: `docs/superpowers/reports/2026-08-23-conversation-query-rewrite-retrieval-ab.md`
- Generated and ignored: `apps/backend/var/benchmarks/<new-run-id>.json`
- Persisted externally: configured Evaluation Archive and PostgreSQL evaluation records.

**Interfaces:**
- Consumes: the same owner, knowledge base, ten reviewed cases, Embedding model and Rerank model used by the prior run.
- Produces: a new immutable baseline/rewrite pair and a case-level Forbidden Top-1 diagnosis.

- [ ] **Step 1: Execute the real A/B under a new run ID**

  Run the existing CLI with `--confirm-real-model`, the prior owner/KB/config and a new immutable run ID. Do not invoke CLS or Docker.

- [ ] **Step 2: Verify persistence and compare results**

  Confirm both terminal PostgreSQL records and archive files exist. Compare Recall@1/3, MRR, Forbidden Top-1, citations, applied rewrites, timeout count, average and P95 latency against `conversation-query-rewrite-ab-20260823-2`. Compare against the old run as a single-variable experiment only if an equivalent old corpus fingerprint can be reconstructed; otherwise restrict the causal comparison to the new run's same-corpus baseline/rewrite arms.

- [ ] **Step 3: Diagnose Forbidden Top-1 without changing labels**

  Identify every case whose Top-1 equals its reviewed forbidden document. Attribute only causes directly proven by safe existing evidence, such as `rewrite_timeout` followed by original-query fallback; do not infer BM25/vector/RRF/Rerank responsibility from final ranks alone. If an unexplained forbidden hit remains, preserve the failure and recommend a separate content-safe staged-ranking diagnostic rather than changing labels.

- [ ] **Step 4: Update the acceptance report**

  Record exact models, thresholds, safe failure codes, results and persistence evidence. State `VALID_PASS` only if every unchanged safety gate passes.
