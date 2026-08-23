# Conversation Benchmark Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Conversation safety/fallback evaluation deterministic and make PG Lock CLS evidence reflect a real bounded timeout without exposing benchmark answers.

**Architecture:** Add a deterministic pre-router safety decision consumed by Chat streaming, inject timeout only through an evaluation-owned model boundary, and derive the PG Lock CLS milestone from a real observation plus evaluator-only record semantics. Existing AIOps diagnosis, recovery, scoring weights, RAG, and ground truth remain unchanged.

**Tech Stack:** Python 3.10+, FastAPI backend, asyncio, SQLAlchemy/PostgreSQL, LangChain model protocol, pytest, Ruff, Pyright, Tencent CLS MCP.

## Global Constraints

- Add no dependency and change no score threshold.
- Never expose benchmark/oracle identifiers to Agent-visible output.
- Blocked input performs zero Router LLM, Agent LLM, query rewrite, and tool calls.
- Timeout failure is deterministic and separately counted from paid provider calls; six scenarios produce four provider calls, five model-boundary attempts, and one injected failure.
- PG Lock timeout evidence requires the same run/scenario/incident scope and a real timeout observation.
- Use unique pytest basetemp/cache paths because the legacy `var/pytest` directory is inaccessible on this Windows host.
- Preserve immutable prior artifacts; all real reruns use new Run IDs.

---

### Task 1: Deterministic Conversation input safety gate

**Files:**
- Create: `apps/backend/src/super_ai/chat/input_safety.py`
- Modify: `apps/backend/src/super_ai/chat/intent.py`
- Modify: `apps/backend/src/super_ai/chat/streaming.py`
- Modify: `apps/backend/src/super_ai/chat/execution_policy.py`
- Test: `apps/backend/tests/test_chat_intent_router.py`
- Test: `apps/backend/tests/test_stream_rag_chat_api.py`
- Test: `apps/backend/tests/test_conversation_model_eval.py`

**Interfaces:**
- Produces `ChatInputSafetyDecision(blocked: bool, reason_code: str | None)` and `evaluate_chat_input_safety(content: str)`.
- Extends `ChatRoute` with `blocked_reason: str | None = None`.
- `ChatStreamingService` consumes `blocked_reason` and emits a fixed safe response without building `ChatAgentRequest`.

- [ ] Add failing unit tests for Chinese/English override-plus-sensitive-action inputs, benign educational text, and ordering before explicit identifier/model routing.
- [ ] Run `uv run pytest tests/test_chat_intent_router.py -q --basetemp=var/pytest-safety-red -o cache_dir=var/cache-safety-red`; expect the malicious cases to route to a high-risk intent or call the model.
- [ ] Implement the pure two-signal classifier with bounded normalization and allowlisted reason code.
- [ ] Add failing streaming tests asserting blocked requests persist one user and one assistant message, return a fixed refusal, and invoke neither memory preparation/refresh, query rewrite, runner, tools, nor title generation. Cover fresh messages and the existing `existing_user_message_id`/`assistant_message_id` idempotent persistence contract.
- [ ] Run the streaming tests and observe the missing blocked branch.
- [ ] Implement the blocked streaming branch and a zero-budget blocked execution policy; store only `blockedReason` in route metadata.
- [ ] Run intent, streaming, execution-policy and Conversation Model tests; expect all pass.
- [ ] Run Ruff and Pyright on modified Chat files.

### Task 2: Deterministic provider-timeout evaluation

**Files:**
- Modify: `apps/backend/src/super_ai/chat/model_evaluation.py`
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Test: `apps/backend/tests/test_conversation_model_eval.py`
- Test: `apps/backend/tests/test_evaluation_history.py`

**Interfaces:**
- Produces evaluation-only `InjectedTimeoutChatModel` delegating no call and raising `TimeoutError` for the degraded scenario.
- Metrics distinguish `providerCallCount` (paid provider calls), `modelBoundaryAttemptCount`, `scenarioAttemptCount`, and `injectedFailureCount`.

- [ ] Add a failing test using a provider that always succeeds; assert `explanation_timeout` still degrades, provider calls equal four, model-boundary attempts equal five, scenario attempts equal six, and injected failures equal one. Prompt Injection contributes no provider or model-boundary call.
- [ ] Run the focused test and verify current `fallback_not_exercised` failure.
- [ ] Implement scenario-specific injected timeout without changing production provider configuration.
- [ ] Extend v2 metric allowlists and persistence tests for the two new bounded counters.
- [ ] Run model-eval, history, recording and persistence tests; expect all pass.
- [ ] Run Ruff and Pyright on model-evaluation/history files.

### Task 3: Real PG Lock timeout observation

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/postgres.py`
- Test: `apps/backend/tests/test_live_postgres_docker.py`
- Test: `apps/backend/tests/test_live_benchmark_runner.py`

**Interfaces:**
- `PostgresLockScenarioDriver.inject()` returns checks `waiter_has_lock_event`, `blocker_edge_confirmed`, and `business_probe_timed_out`.
- The waiter task remains shielded and available to recovery/cleanup after the timeout observation.

- [ ] Add a failing Docker contract test requiring the third check while the waiter remains blocked.
- [ ] Run only that Docker test; expect missing `business_probe_timed_out`.
- [ ] Add a run-state timeout flag and bounded `wait_for(shield(waiter_task))` observation.
- [ ] Add a negative test where the update completes before the deadline and the observation is not confirmed.
- [ ] Run Postgres Live driver/runner tests and verify cleanup leaves no run-scoped sessions.

### Task 4: Answer-isolated CLS timeout evidence

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cls_evidence.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/evidence_client.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Test: `apps/backend/tests/test_live_cls_evidence.py`
- Test: `apps/backend/tests/test_live_evidence_client.py`
- Test: `apps/backend/tests/test_evaluation_artifacts.py`
- Test: `apps/backend/tests/test_live_evaluation_scoring.py`

**Interfaces:**
- Produces `PostgresLockClsRecordProvider.records(...)` requiring all three observation checks.
- Agent-visible `SearchLog` output contains counts and allowlisted event facts, but no `benchmarkEvidenceId`, `run_id`, `scenario_id`, or `incident_id`.
- Evaluator-only artifact projection receives trusted raw tool-audit scope and maps a PG Lock `request_timeout` event to `cls-live-request-timeout` only after run/scenario/incident all match.

- [ ] Add failing tests that the provider refuses an unconfirmed observation and emits `request_timeout` only for a confirmed observation.
- [ ] Add failing tests that Composite output contains none of the four benchmark/scope identifiers while preserving bounded operational event fields.
- [ ] Add failing artifact tests for: valid timeout maps; empty records, `database_contention` only, wrong run, wrong scenario, and wrong incident do not map.
- [ ] Implement the provider, safe event allowlist update, registry wiring, and evaluator-only mapping.
- [ ] Run CLS, evidence-client, artifact and scoring tests; expect all pass with answer-isolation assertions.
- [ ] Run Ruff and Pyright on all Live files.

### Task 5: Focused and real acceptance

**Files:**
- Modify: `docs/superpowers/reports/2026-08-22-chat-execution-eval-acceptance.md`
- Generated/ignored: `apps/backend/var/benchmarks/conversation/*.json`

**Interfaces:**
- Consumes all prior tasks and produces immutable real evaluation artifacts in Archive and PostgreSQL.

- [ ] Run the complete Conversation/Live focused pytest set with a unique basetemp; explicitly include Chat Live CLI defaults, evaluation-session-before-Pending-Action, and nullable `chat_run_id` adapter/repository contracts; expect zero failures.
- [ ] Run Ruff, targeted Pyright, and `git diff --check`; expect clean output.
- [ ] Run real six-scenario Conversation Model Eval with a new Run ID; require 6/6, four provider calls, five model-boundary attempts, one injected timeout, and prompt-injection safety 1.0.
- [ ] Run real Chat PG Lock Live with a new Run ID; require valid scoring, all three evidence milestones, recovery verification and cleanup success.
- [ ] Verify JSON, Evaluation Archive and PostgreSQL terminal records for both runs.
- [ ] Update the acceptance report with Git SHA, commands, metrics, old-vs-new comparison and any remaining valid capability miss.
