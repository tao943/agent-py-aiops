# Conversation RAG Adaptive Query Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a request-scoped adaptive query-rewrite boundary to Conversation Agent knowledge retrieval and persist a real baseline-versus-rewrite Retrieval evaluation.

**Architecture:** A deterministic router decides whether the current knowledge query is standalone or context-dependent. Only context-dependent queries invoke one bounded structured model call; validation failures fall back to the original query. The transformer is injected only into the Conversation `knowledge_question` LangChain tool, ahead of the existing tenant-safe cached hybrid retrieval stack. A separate benchmark runner executes baseline and rewrite arms against the same real Milvus/Rerank data and records both as immutable `retrieval` artifacts.

**Tech Stack:** Python 3.12, asyncio, Pydantic, LangChain structured tools, PostgreSQL evaluation repository, Milvus, existing embedding/rerank/chat provider, Pytest, Ruff, Pyright.

## Global Constraints

- Scope is Conversation Agent `knowledge_question` retrieval only; AIOps RAG, CLS, PostgreSQL evidence tools, recovery, and existing AIOps scoring remain unchanged.
- Reuse outcome is `reference only + project-owned thin wrapper`; add no dependency.
- Direct queries make zero rewrite model calls; contextual queries make at most one.
- The request-scoped transformer owns an `asyncio.Lock`-protected one-shot rewrite allowance; after the first rewrite attempt, all later retrieval tool calls in the same request use their original query without another rewrite model call.
- Total knowledge-route model budget is three, while LangChain Agent middleware remains limited to two.
- Rewriter timeout is 15.0 seconds and cannot extend the overall Chat deadline.
- The transformer may change only `KnowledgeRetrievalToolInput.query`; owner, accessible knowledge bases, filters, document IDs, metadata, and topK remain unchanged.
- Missing context, timeout, model failure, invalid schema, or semantic-guard failure must retrieve with the original query.
- Audit output must not persist prompt text, reasoning, raw model output, or context content.
- Use TDD and run only focused tests; do not run the full Pytest suite.
- Real evaluation requires explicit `--confirm-real-model`, does not call CLS or Docker, and must persist both A/B arms even when the rewrite arm misses its target.

---

### Task 1: Deterministic router and bounded structured rewriter

**Files:**
- Create: `apps/backend/src/super_ai/chat/query_rewrite.py`
- Create: `apps/backend/tests/test_chat_query_rewrite.py`

**Interfaces:**
- Consumes: existing `super_ai.llm.ChatModel`; bounded Conversation messages represented as `(role, content)` values.
- Produces: `QueryRewriteDecision`, `QueryRewriteAudit`, `QueryRewriteOutcome`, `AdaptiveQueryRewriteRouter.decide(...)`, `StructuredQueryRewriter.rewrite(...)`, and `AdaptiveKnowledgeQueryTransformer.transform(...)`.

- [ ] **Step 1: Write router tests before production code**

  Add focused tests proving that a standalone query such as `PostgreSQL SQLSTATE 40P01 如何排查` returns `direct/standalone_query`, a contextual query such as `那这个要怎么处理` with prior messages returns `rewrite/context_reference`, and the same query without prior messages returns `direct_without_context/missing_context`. Also cover low-information and explicit follow-up expressions.

- [ ] **Step 2: Run the router tests and verify RED**

  Run from `apps/backend`:

  ```powershell
  uv run pytest tests/test_chat_query_rewrite.py -q
  ```

  Expected: collection/import failure because `super_ai.chat.query_rewrite` does not exist.

- [ ] **Step 3: Implement the deterministic router minimally**

  Create immutable dataclasses and literal enums. Normalize whitespace, identify context-reference/follow-up phrases, and classify a normalized query of at most 12 Chinese characters as low-information only when it lacks a protected ASCII token, error code, resource ID, known component, or concrete failure term. If no earlier user/assistant content exists, convert any rewrite decision to `direct_without_context/missing_context`.

- [ ] **Step 4: Run the router tests and verify GREEN**

  Run the same focused command and require zero failures.

- [ ] **Step 5: Add failing structured-rewriter and fallback tests**

  Add asynchronous tests with a small fake `ChatModel` covering: exactly one call for contextual input; two sequential and two concurrent contextual transforms still make exactly one model call; strict JSON fields `rewrittenQuery` and `usedContext`; timeout; model exception; malformed JSON; unknown field; empty and over-512-character query; `usedContext=false`; and semantic guards for `40P01`, `order-service`, resource IDs, Chinese/English negation, and the selected history-topic anchor. Assert every failure returns the original query and one safe error code without prompt, response, reasoning, or context text.

- [ ] **Step 6: Run the new tests and verify RED**

  Expected: failures for missing `StructuredQueryRewriter` and transformer behavior.

- [ ] **Step 7: Implement strict parsing, semantic guards, and timeout**

  Protect a request-local consumed flag with `asyncio.Lock`; consume the allowance before awaiting the provider so timeout/error cannot trigger a second paid attempt. Invoke the existing chat model once through `asyncio.wait_for(..., timeout=15.0)`. Parse model content as a strict Pydantic object with `extra="forbid"`, require `usedContext is True`, and validate all protected terms case-insensitively. Select the nearest complete prior turn that provides a unique component/error/resource/failure anchor; require the rewritten query to retain that anchor. If recent turns contain competing topics with no unique nearest anchor, return `direct_without_context`/`missing_context` rather than guessing. Map failures only to `rewrite_timeout`, `rewrite_model_failed`, `rewrite_schema_invalid`, or `rewrite_semantic_guard_failed`. Build the audit from action, reason, applied flag, call count, elapsed milliseconds, and safe error code.

- [ ] **Step 8: Run Task 1 tests and refactor while green**

  Require all `test_chat_query_rewrite.py` tests to pass, then remove duplicated normalization/parsing helpers without changing behavior.

---

### Task 2: Inject the transformer into Conversation retrieval and reserve its budget

**Files:**
- Modify: `apps/backend/src/super_ai/retrieval/tool.py`
- Modify: `apps/backend/src/super_ai/retrieval/__init__.py`
- Modify: `apps/backend/src/super_ai/chat/execution_policy.py`
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Modify: `apps/backend/tests/test_knowledge_retrieval_tool.py`
- Modify: `apps/backend/tests/test_chat_execution_policy.py`
- Modify: `apps/backend/tests/test_chat_react_budget.py`
- Modify: `apps/backend/tests/test_retrieval_cache.py`

**Interfaces:**
- Consumes: `AdaptiveKnowledgeQueryTransformer` from Task 1 and existing `KnowledgeRetrievalToolRunner.run(...)`.
- Produces: retrieval-layer `KnowledgeRetrievalQueryTransform` dataclass and `KnowledgeRetrievalQueryTransformer` protocol; optional `query_transformer` argument on `create_langchain_knowledge_retrieval_tool(...)`; `ChatExecutionBudget.max_query_rewrite_calls`.

- [ ] **Step 1: Write failing retrieval-boundary tests**

  Extend tool tests with a fake transformer that replaces only the query and returns safe metadata. Assert the runner receives the effective query while `top_k`, filters, owner, and accessible knowledge-base IDs remain byte-for-byte/equality equivalent. Assert the LangChain payload includes `queryRewrite`, while factory calls without a transformer preserve the current payload.

- [ ] **Step 2: Run retrieval tests and verify RED**

  ```powershell
  uv run pytest tests/test_knowledge_retrieval_tool.py tests/test_retrieval_cache.py -q
  ```

  Expected: failure because the factory has no transformer contract.

- [ ] **Step 3: Add the retrieval-layer protocol and optional factory hook**

  Define the protocol in `retrieval/tool.py` so the retrieval package does not import the chat package. In `run_knowledge_retrieval`, construct the canonical input once, optionally transform it, call the unchanged runner with the transformed input, then attach a copied `queryRewrite` mapping to the LangChain-only payload. Export the protocol/result from `retrieval/__init__.py`.

- [ ] **Step 4: Verify retrieval tests GREEN, including effective-query cache reuse**

  Ensure two equal rewritten effective queries hit the existing cache and that differing owner/version/filter values still do not collide.

- [ ] **Step 5: Write failing policy and middleware budget tests**

  Change the knowledge policy expectation to `max_model_calls == 3` and `max_query_rewrite_calls == 1`; assert all other policies default to zero rewrite calls. Assert `build_agent_middleware` gives LangChain `run_limit == 2`, not three.

- [ ] **Step 6: Run budget tests and verify RED**

  ```powershell
  uv run pytest tests/test_chat_execution_policy.py tests/test_chat_react_budget.py -q
  ```

  Expected: failures because the budget has no rewrite reservation and middleware currently uses the total.

- [ ] **Step 7: Implement the reserved budget and request-scoped injection**

  Add `max_query_rewrite_calls: int = 0` after existing required budget fields. Set the knowledge route to `(max_model_calls=3, max_tool_calls=2, deadline_seconds=120.0, max_query_rewrite_calls=1)`. Set middleware model limit to `max_model_calls - max_query_rewrite_calls`. In `LangChainChatAgentRunner.stream`, only when the route is `knowledge_question`, extract at most the last two complete prior user/assistant turns from the already-bounded `request.messages`, create a request-scoped transformer with `self._llm_provider.create_chat_model()`, and pass it to the tool factory. Pass no transformer for every other route.

- [ ] **Step 8: Add and pass integration safety tests**

  Prove direct queries call no rewriter, two distinct retrieval tool calls in one request still call the rewriter at most once, prompt-injection text cannot alter owner/KB/filter/topK, an incorrect cross-topic rewrite is rejected, multi-topic history without a unique nearest anchor does not guess, AIOps construction remains transformer-free, and the combined Agent-plus-Rewriter ceiling is three. Run:

  ```powershell
  uv run pytest tests/test_chat_query_rewrite.py tests/test_knowledge_retrieval_tool.py tests/test_chat_execution_policy.py tests/test_chat_react_budget.py tests/test_retrieval_cache.py -q
  ```

---

### Task 3: Add a persistent Retrieval A/B benchmark

**Files:**
- Create: `benchmarks/agentpy/retrieval/query_rewrite_cases.yaml`
- Create: `apps/backend/src/super_ai/evaluation/query_rewrite.py`
- Create: `apps/backend/scripts/run_query_rewrite_benchmark.py`
- Create: `apps/backend/tests/test_query_rewrite_benchmark.py`
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Modify: `apps/backend/tests/test_evaluation_persistence.py`

**Interfaces:**
- Consumes: canonical retrieval runner, Task 1 transformer, `evaluate_retrieval`, `EvaluationRunRecorder`, and existing `evaluation_kind="retrieval"` envelope schema.
- Produces: YAML case loader; baseline/rewrite arm executor; content-free report; two terminal immutable artifacts whose run IDs end in `-baseline` and `-rewrite`.

- [ ] **Step 1: Write fixture-loader and deterministic evaluator tests**

  Define 8–12 cases spanning PostgreSQL deadlock/pool exhaustion, Redis maxclients/failover, Nginx timeout/routing, Kubernetes DNS/endpoints, and queue backlog/consumer stall. Each case contains an ID, bounded user/assistant context, follow-up query, relevant document basenames, forbidden top-one document basenames, and acceptable topK. Test duplicate IDs, empty context, invalid roles, missing relevance labels, and unsafe path handling.

- [ ] **Step 2: Run benchmark tests and verify RED**

  ```powershell
  uv run pytest tests/test_query_rewrite_benchmark.py -q
  ```

  Expected: import failure because the evaluator and runner do not exist.

- [ ] **Step 3: Implement the case loader and paired arm executor**

  Reuse `RetrievalQueryResult` and `evaluate_retrieval`. The baseline arm sends the raw follow-up to canonical retrieval. The rewrite arm transforms the same input and then sends the effective query. Collect only IDs, document basenames, rank/channel audit, safe rewrite metadata, per-query duration, and aggregate metrics; never save context or rewritten text. Add `rewriteAppliedCount`, `rewriteModelCallCount`, `averageDurationMs`, and `p95DurationMs` to the retrieval metric allowlist.

- [ ] **Step 4: Add failure, interruption, and persistence tests**

  Assert both arms are created as immutable running retrieval envelopes before either arm executes. Completed arms persist terminal metrics; a benchmark miss exits 1 but still persists; configuration/infrastructure exceptions terminate both arms with `infra_invalid` (the unexecuted arm records a safe skipped reason) and exit 2; cancellation terminates both arms as interrupted and returns 130 at the CLI boundary. Cover database start failure, baseline execution failure, and baseline interruption so no arm remains running or absent. Assert metadata contains the dataset checksum and safe model names but no API key, prompt, raw response, query text, or context.

- [ ] **Step 5: Implement the explicit real-model CLI**

  Require `--confirm-real-model`, `--owner-user-id`, and `--knowledge-base-id`; accept `--config`, `--cases`, `--output`, and `--run-id`. Before any model call, resolve the owner's active/indexed documents and require every relevant/forbidden basename to resolve uniquely inside the selected KB, reject duplicate/ambiguous basenames, and require at least 8 valid cases; otherwise terminate both artifacts as `infra_invalid` and exit 2. Build the configured chat/embedding/rerank provider and Milvus tool once, execute baseline then rewrite against the same scope, save both artifacts through `EvaluationRunRecorder`, and optionally write a combined compatibility JSON report. Return 0 only when rewrite reaches the existing Retrieval thresholds without reducing recall/citation safety relative to baseline; return 1 for a valid miss, 2 for configuration/infrastructure failure, and 130 for interruption.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

  Run `test_query_rewrite_benchmark.py` and the focused persistence tests until zero failures.

---

### Task 4: Focused regression, documentation, and real A/B acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/aiops/agentpy-domainbench.md`
- Verify: all files changed by Tasks 1–3

**Interfaces:**
- Consumes: completed feature and benchmark CLI.
- Produces: user-facing runbook plus fresh offline and real evaluation evidence.

- [ ] **Step 1: Document behavior and operational boundaries**

  Add the direct/rewrite/fallback flow, three-call total budget, safe audit fields, explicit real-model confirmation, exit codes, and two-artifact persistence behavior. State that the benchmark does not use CLS or Docker and that failures never widen retrieval scope.

- [ ] **Step 2: Run focused Pytest regression**

  ```powershell
  uv run pytest tests/test_chat_query_rewrite.py tests/test_knowledge_retrieval_tool.py tests/test_chat_execution_policy.py tests/test_chat_react_budget.py tests/test_query_rewrite_benchmark.py tests/test_retrieval_cache.py tests/test_conversation_eval.py tests/test_evaluation_persistence.py -q
  ```

  Expected: zero failures; this is intentionally not the full suite.

- [ ] **Step 3: Run Ruff and Pyright**

  ```powershell
  uv run ruff check src/super_ai/chat/query_rewrite.py src/super_ai/chat/execution_policy.py src/super_ai/chat/streaming.py src/super_ai/retrieval src/super_ai/evaluation/query_rewrite.py scripts/run_query_rewrite_benchmark.py tests/test_chat_query_rewrite.py tests/test_query_rewrite_benchmark.py
  uv run pyright
  ```

  Expected: both commands exit 0.

- [ ] **Step 4: Preflight the real scope without exposing credentials**

  Resolve the configured PostgreSQL/Milvus services, owner user, active indexed knowledge base, and expected document basenames. Require at least 8 valid labeled cases and unique resolution of every relevant/forbidden basename. Print only safe IDs/counts and model names. If the dataset, labels, or migrations are unavailable/ambiguous, stop with exit 2 and preserve both failed artifacts rather than silently running an empty-RAG benchmark.

- [ ] **Step 5: Run and persist the real Retrieval A/B**

  From `apps/backend`, run the new script with the discovered safe owner/KB IDs, configured project file, a stable run ID, and `--confirm-real-model`. Do not print secrets or raw responses. Require both `*-baseline` and `*-rewrite` artifacts to exist in PostgreSQL and the configured Evaluation Archive.

- [ ] **Step 6: Report measured results without overclaiming**

  Compare Recall@1, Recall@3, MRR, forbidden Top-1, citation completeness, rewrite application/model-call counts, average latency, and P95 latency. Label this as a `query-rewrite + retrieval component A/B`, because its baseline deliberately bypasses the production Agent's own tool-query wording. Also run one non-scoring real Conversation `knowledge_question` smoke request and verify safe `queryRewrite` metadata proves the production injection is reachable. Do not treat the smoke as retrieval-quality evidence. Distinguish implementation correctness from empirical retrieval gain; if rewrite does not beat baseline, preserve the result and identify dataset/routing/model causes rather than lowering thresholds.
