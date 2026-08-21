# V4 Structured LLM Validator Design

## Context

The evidence-driven V4 graph already treats semantic validation as a
conditional safety gate. `validator_router_v4` calls
`requires_llm_validation()` only after deterministic validation succeeds, and
routes to the LLM Validator when at least one allowlisted semantic-risk reason
exists:

- an LLM adjudicator contributed to the diagnosis;
- a recovery execution was requested;
- the recovery risk is L2 or L3;
- the diagnosis contains a compound root cause;
- the causal chain crosses components; or
- high-quality evidence conflicts.

Pure deterministic, low-risk diagnoses skip the gate. This routing contract is
correct and remains unchanged.

The defect is inside the gate after it is selected. `_llm_validator_v4()`
currently calls ordinary `model.ainvoke(prompt)` through `_invoke_v4_model()`
and then parses free-form text once. The configured Validator model advertises
`json_mode`, but the V4 path never applies `with_structured_output()`. Schema or
JSON failures are also collapsed into `llm_failed`, so persisted artifacts
cannot distinguish provider failures from parsing failures.

## Reuse assessment

- The project already provides
  `invoke_structured_root_cause_validation()` in `decision_validation.py`. It
  applies a Pydantic schema through `with_structured_output(method=...)`,
  performs one format-only correction retry, validates cited Evidence IDs, and
  returns secret-safe failure metadata.
- Existing provider helpers already select the dedicated Validator model and
  normalize its structured-output method.
- Existing V4 runtime code already enforces model-call budgets, hard deadlines,
  role timeouts, and bounded audit records.
- GitHub references checked: `langchain-ai/langchain` (MIT). Its maintained
  `ChatOpenAI.with_structured_output()` implementation and tests explicitly
  support `method="json_mode"` and Pydantic schemas.

Decision: directly reuse the project's structured Validator helper, with a
small project-owned adapter so its model invocations continue to pass through
the V4 budget, timeout, and audit controls. No dependency, native binary,
external service, or license obligation is added.

## Design

### 1. Preserve the optional gate

`validator_router_v4`, `ValidatorRiskContext`, and the allowlisted reason codes
remain unchanged. Model text cannot influence whether the gate is selected.

For `APY-LIVE-ORDER-POOL-LEAK-001`, the gate remains required when a controlled
recovery execution is requested, with reason code `execution_requested`.
Proposal-only and low-risk deterministic paths continue directly to the policy
gate without spending a Validator model call.

### 2. Use provider-compatible structured validation

When the route selects the gate and a root-cause candidate exists,
`_llm_validator_v4()` will:

1. create the configured dedicated Validator chat model;
2. select the provider's normalized Validator structured-output method;
3. invoke `invoke_structured_root_cause_validation()` with the public candidate,
   public structured observations, and persisted Evidence IDs;
4. use a V4 invocation adapter for every attempt so budget reservation, hard
   deadline, Validator role timeout, and model-call audit behavior are retained;
5. allow at most one format-only correction retry.

The correction retry changes only formatting instructions. It must not add new
facts, hidden answers, ground truth, or recovery authority.

The structured helper may be extended with an optional invocation callback.
Existing callers retain their current direct `ainvoke()` behavior when no
callback is supplied. The callback receives the already configured structured
invoker and prompt, preventing duplicate schema or parsing implementations in
the V4 workflow.

### 3. Persist bounded success and failure metadata

On a valid structured decision, the V4 payload records:

- `validationOrigin = "llm_semantic"`;
- `semanticValidationStatus = "valid"`;
- `semanticValidationAttempts` from the structured helper;
- the configured `validationModel` identifier;
- updated model-call count and bounded audits.

On model invocation or structured parsing failure, the deterministic diagnosis
is preserved, but recovery is forced to `manual_review` and execution remains
denied. The payload records only allowlisted metadata already represented by
the structured outcome:

- `validationErrorCategory`;
- `validationErrorCode`;
- `validationErrorCodes`;
- `validationErrorPhase`;
- `validationRetryable`;
- `validationHttpStatusClass`;
- `semanticValidationAttempts`;
- `validationModel`.

The raw provider exception, raw model response, prompt, credentials, private
reasoning, and ground truth are never persisted.

If the candidate is absent, no model is called. The gate fails closed with a
bounded `candidate_missing` classification and a manual-review recovery plan.

### 4. Safety and compatibility

- Deterministic validation remains the prerequisite for semantic validation.
- A semantic failure cannot convert an invalid deterministic diagnosis into an
  executable recovery.
- A semantic success does not bypass the existing recovery policy gate or human
  approval requirements.
- Existing checkpoint and step payloads remain backward compatible; new fields
  are additive.
- Model attempts, including the correction retry, count against the V4 model
  budget and appear in audit records.
- Earlier evaluation artifacts are immutable and will not be rewritten.

## Test strategy

1. Add a focused V4 workflow test proving the configured Validator
   `json_mode` reaches `with_structured_output()` and a schema-valid response
   produces `llm_semantic / valid`.
2. Add a regression test where the first structured response fails parsing and
   the format-only retry succeeds; assert exactly two attempts and two budgeted
   audits.
3. Extend the fail-closed test to assert safe timeout classification and
   `manual_review` without raw exception content.
4. Add a retry-exhaustion test that asserts structured-parse error codes and no
   executable recovery.
5. Keep routing tests proving proposal-only deterministic diagnoses skip the
   LLM Validator while execution requests select it.
6. Run focused pytest for V4 Validator, decision-validation helper, and routing
   tests, followed by Ruff and Pyright only for touched modules.
7. Run one new real Multi canary for `APY-LIVE-ORDER-POOL-LEAK-001`, persist it
   under a unique run ID, perform explicit cleanup, and verify either a valid
   semantic decision or a precise safe failure classification.

## Acceptance criteria

- The V4 LLM Validator uses the configured structured-output method rather than
  free-form text parsing.
- A format-only correction is attempted at most once and is included in model
  budget and audit accounting.
- Successful validation persists `llm_semantic / valid`.
- Provider and parsing failures are distinguishable through bounded safe
  metadata and always force `manual_review` with
  `executionPermitted = false`.
- Low-risk deterministic paths still skip the LLM Validator entirely.
- No raw model output, exception text, secret, private reasoning, or ground
  truth is persisted.
- No new dependency or external service is introduced.
