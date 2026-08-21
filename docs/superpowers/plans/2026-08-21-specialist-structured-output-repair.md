# Specialist Structured Output Repair Implementation Plan

> Design: `docs/superpowers/specs/2026-08-21-specialist-structured-output-repair-design.md`

## Goal

Make real Qwen Specialist roles use the provider-configured structured-output
method and preserve bounded diagnostic traceability in failed Live terminal
envelopes, without weakening recovery gates or rewriting historical runs.

## Task 1: Propagate the Specialist structured-output method

**Files**

- Modify: `apps/backend/tests/test_aiops_specialist_model_roles.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify: `apps/backend/src/super_ai/aiops/investigation_runtime.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`

**Steps**

1. Add failing tests that construct a Specialist with `json_mode` and record the
   method used for Local Plan and Evidence Analysis.
2. Add a failing workflow wiring test proving provider capability selection is
   passed into `SpecialistExecutor`.
3. Add a required `structured_output_method` constructor parameter and forward
   it to `invoke_bounded_structured_role()`.
4. Include the method in the role execution identity so a repaired `json_mode`
   call cannot reuse a failed checkpoint created under `function_calling`; test
   that method changes alter the identity while same-method retries stay
   idempotent.
5. Wire production construction through `_provider_structured_output_method()`.
6. Run the focused Specialist and workflow tests; refactor only after green.

## Task 2: Carry bounded diagnostics across Live failures

**Files**

- Modify: `apps/backend/tests/test_live_benchmark_runner.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`

**Steps**

1. Add a failing runner test in which diagnosis succeeds and recovery is
   denied; assert the raised classified failure includes diagnostic task ID and
   bounded process metrics.
2. Introduce a typed bounded failure context built only from `RunArtifact`
   identifiers and `investigation_audit`: role status, duration, model/tool/
   evidence counts, aggregation state, missing domains, and checksum.
3. Attach that context when rethrowing post-diagnosis classified failures,
   preserving stage/category/cleanup semantics.
4. Verify failures before diagnosis remain metadata-free.

## Task 3: Persist diagnostics in failed terminal envelopes

**Files**

- Modify: `apps/backend/tests/test_live_benchmark_cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`

**Steps**

1. Add a failing CLI test for `recovery_denied` after a multi-agent diagnostic.
2. Assert the immutable failed envelope stores `diagnosticTaskId` and the same
   bounded process metric names used by successful terminal envelopes.
3. Assert the public CLI response does not reveal internal metrics, prompts,
   evidence content, or model output.
4. Explicitly assert that result metrics requiring Oracle, Recovery, or
   Verification (including score, root-cause correctness, evidence recall, and
   security hard-gate outcome) are absent from failed terminal envelopes.
5. Implement a shared process-metric projection helper; completed evaluations
   may add result metrics only after scoring.
6. Keep cleanup-only persistence as the fallback when no diagnostic context is
   available.

## Task 4: Focused verification

1. Run focused pytest for the three touched test modules.
2. Run Ruff check/format validation for touched Python files.
3. Run the repository's focused Pyright command for the backend package or the
   narrowest supported target.
4. Run `git diff --check` and inspect the final diff for secret or answer data.
5. Commit the repair and tests.

## Task 5: Real canary and result persistence

1. Confirm the configured provider advertises `json_mode` without printing any
   secret values.
2. Run a new uniquely named real Multi canary for
   `APY-LIVE-ORDER-POOL-LEAK-001` using the existing LLM and CLS configuration.
3. Wait through completion with bounded polling, preserving the terminal
   evaluation envelope.
4. Verify cleanup independently even if diagnosis or recovery fails.
5. Report score/status, diagnostic task ID, Specialist statuses/tool counts,
   aggregation status, recovery result, model calls, and elapsed time.
6. If the original 4xx is fixed but a different failure appears, classify it
   from persisted evidence and apply only a localized, test-backed repair before
   rerunning with another unique run ID.

## Acceptance checks

- Both Specialist roles receive `json_mode` in unit tests.
- Production workflow wiring is covered.
- Focused tests, Ruff, and Pyright pass.
- Failed terminal envelopes are traceable and redacted.
- The real canary no longer fails because Specialist roles implicitly use
  `function_calling`.
- Cleanup succeeds and all new run records remain immutable.
