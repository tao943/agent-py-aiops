# Dedicated LLM Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use `qwen3.8-max` only for semantic Decision validation while preserving `qwen3.7-plus` for the Agent, and persist secret-safe structured-parse diagnostics.

**Architecture:** Extend the existing LLM configuration and `QwenOpenAIProvider` with an optional Validator model that reuses the same credentials and endpoint. Route only `_decision_validator` through that model, keep deterministic validation authoritative, and classify Pydantic/LangChain parse failures into an allowlisted audit projected into Step, Checkpoint, and RunArtifact.

**Tech Stack:** Python 3.12, LangChain `ChatOpenAI`, Pydantic v2, pytest, Ruff, Pyright, OpenSpec.

## Global Constraints

- Work only on `feat/snapshot-tool-calling-contract` in the existing isolated worktree.
- Main Agent remains `qwen3.7-plus`; only LLM Validator uses `qwen3.8-max`.
- Validator reuses the existing DashScope API Key, Base URL, temperature, timeout, and retry settings.
- Do not add dependencies, database schema, services, or recovery permissions.
- Do not persist or print API keys, Prompt, raw response, exception text, field values, Ground Truth, Oracle, or raw CLS logs.
- Deterministic Validator, Benchmark answers/weights/thresholds, and Policy Gate remain unchanged.
- Do not run full pytest and do not automatically rerun APY-013.

## File Structure

- `apps/backend/src/super_ai/llm/config.py`: parses main/Validator models and their capability profiles.
- `apps/backend/src/super_ai/llm/provider.py`: constructs main and Validator ChatOpenAI instances with shared transport settings.
- `apps/backend/src/super_ai/aiops/decision_validation.py`: classifies structured parse failures without retaining input values.
- `apps/backend/src/super_ai/aiops/diagnostics.py`: selects the Validator model, adds safe JSON examples, and persists audit fields.
- `apps/backend/src/super_ai/evaluation/artifacts.py`: projects the final validation audit into the scoreable artifact without changing scoring.
- `apps/backend/tests/test_llm_provider.py`: configuration/provider compatibility tests.
- `apps/backend/tests/test_aiops_decision_validation.py`: parse-code and redaction tests.
- `apps/backend/tests/test_aiops_reasoning_trace.py`: workflow model-selection and persistence tests.
- `apps/backend/tests/test_evaluation_artifacts.py`: Artifact projection and historical compatibility tests.
- `apps/backend/tests/test_live_llm.py`: synthetic `qwen3.8-max` Validator readiness.
- `config/user.project.template.json`: documents the non-secret Validator configuration.
- `config/user.project.json`: ignored local configuration; add `qwen3.8-max` without changing the API Key.
- `openspec/changes/harden-aiops-decision-validation/`: records the public behavioral contract.
- `docs/aiops/agentpy-domainbench.md`: records offline/readiness acceptance without claiming a new APY score.

---

### Task 1: Independent Validator configuration and Provider

**Files:**
- Modify: `apps/backend/src/super_ai/llm/config.py`
- Modify: `apps/backend/src/super_ai/llm/provider.py`
- Modify: `apps/backend/tests/test_llm_provider.py`
- Modify: `config/user.project.template.json`

**Interfaces:**
- Extends `LlmProviderConfig` with `validator_model: str` and `validator_structured_output_method: StructuredOutputMethod`.
- Produces `QwenOpenAIProvider.create_validator_model() -> ChatModel`.
- Produces `validator_model_name` and `validator_structured_output_method` read-only properties.

- [ ] **Step 1: Write failing configuration tests**

Extend `_write_config` with `validator_model: str | None = None` and a Validator capability profile. Add assertions:

```python
assert config.validator_model == "qwen3.8-max"
assert config.validator_structured_output_method == "json_mode"
```

Add backward compatibility where absent `validatorModel` equals `chatModel`. Add a negative test where `validatorModel=qwen3.8-max` has no profile and assert `LlmConfigurationError` contains only the safe model name.

- [ ] **Step 2: Run configuration tests and verify RED**

Run:

```powershell
uv run pytest tests/test_llm_provider.py -k "validator or loads_offline" -q
```

Expected: FAIL because Validator configuration fields do not exist.

- [ ] **Step 3: Implement configuration parsing**

Parse `validatorModel` as an optional non-empty string, defaulting to `chatModel`. Resolve both profiles through one private helper that validates `contextWindowTokens` and `structuredOutputMethod`; retain the main context window for current behavior and return the Validator method separately. Never include `api_key` in repr.

- [ ] **Step 4: Write failing Provider construction tests**

Capture configs passed to the existing injectable `model_factory`. Assert main construction receives `chat_model=qwen-test-chat`, Validator construction receives `chat_model=qwen3.8-max`, and both have equal `api_key`, `base_url`, `timeout_seconds`, and `max_retries`. Assert:

```python
assert provider.validator_model_name == "qwen3.8-max"
assert provider.validator_structured_output_method == "json_mode"
```

- [ ] **Step 5: Run Provider tests and verify RED**

Run the same focused command and expect missing methods/properties.

- [ ] **Step 6: Implement Validator construction and template**

Use `dataclasses.replace(self._config, chat_model=self._config.validator_model)` before passing the config to the existing model factory. This guarantees shared credentials/transport settings without duplicating model creation. Add the two model profiles and `validatorModel` to the user template; keep all credential values empty.

- [ ] **Step 7: Run Task 1 tests and verify GREEN**

```powershell
uv run pytest tests/test_llm_provider.py -q
uv run ruff check src/super_ai/llm/config.py src/super_ai/llm/provider.py tests/test_llm_provider.py
```

Expected: PASS and Ruff clean.

- [ ] **Step 8: Commit Task 1**

```powershell
git add -- apps/backend/src/super_ai/llm/config.py apps/backend/src/super_ai/llm/provider.py apps/backend/tests/test_llm_provider.py config/user.project.template.json
git commit -m "feat: configure a dedicated validator model"
```

### Task 2: Secret-safe structured parse subcategories

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Produces `StructuredParseErrorCode` with the eight allowlisted codes in the design.
- Produces private `_StructuredParseFailure(code: StructuredParseErrorCode)` carrying no raw value.
- Preserves `StructuredValidationOutcome.error_codes: tuple[str, ...]` and its two-attempt limit.

- [ ] **Step 1: Write failing parse classification tests**

Use raw `SequenceChatModel` responses and structured envelopes to cover:

```python
(
    "invalid_json",
    "structured_envelope_mismatch",
    "missing_required_field",
    "invalid_enum",
    "wrong_container_type",
    "extra_field",
    "unknown_evidence_id",
)
```

Each test supplies the same invalid shape twice, expects `retry_exhausted`, exact stable `error_codes`, `error_phase=structured_parse`, and asserts a sentinel raw value is absent from `repr(outcome)`.

- [ ] **Step 2: Run parse tests and verify RED**

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "parse_error_code or unknown_envelope_evidence" -q
```

Expected: FAIL because all parse failures currently collapse to `invalid_json_or_schema`.

- [ ] **Step 3: Implement safe classification**

Catch `json.JSONDecodeError` as `invalid_json`. Validate structured envelope keys before schema conversion and map malformed envelope/parsing error to `structured_envelope_mismatch`. Catch Pydantic `ValidationError`, project only `error["type"]` and the final allowlisted string in `error["loc"]`, then map `missing`, `literal_error`, list/tuple/set type errors, and `extra_forbidden`. Validate Evidence ownership after schema validation and raise `unknown_evidence_id` without retaining the ID. Other `TypeError`/`ValueError` maps to `invalid_json_or_schema`.

- [ ] **Step 4: Preserve errors across the bounded retry**

Accumulate distinct codes from attempt one and two, stable in first-seen order and capped at six. A successful second attempt returns no error codes; two failures return the accumulated tuple. Do not change Provider exception classification.

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "structured_validator or model_failure" -q
uv run ruff check src/super_ai/aiops/decision_validation.py tests/test_aiops_decision_validation.py
```

Expected: PASS, existing timeout/auth/rate/provider classification remains green, and Ruff is clean.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/tests/test_aiops_decision_validation.py
git commit -m "fix: classify validator parse failures safely"
```

### Task 3: Route only semantic validation through qwen3.8-max

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces compatibility helpers `_validator_chat_model`, `_validator_model_name`, and `_validator_structured_output_method`.
- Persists `validationModel` and `validationErrorCodes` in Step, Checkpoint, and returned validation state.
- Preserves main-model calls for every non-Validator node.

- [ ] **Step 1: Write failing model-selection and Prompt tests**

Add a provider fake with separately recording main and Validator models. Run `_decision_validator` with an already grounded Candidate and assert exactly one Validator call, zero new main calls, method `json_mode`, and:

```python
assert payload["validationModel"] == "qwen3.8-max"
assert "JSON" in validator_prompt
assert '"status":"valid"' in validator_prompt
assert "ground_truth" not in validator_prompt.casefold()
assert "oracle" not in validator_prompt.casefold()
```

Add a legacy fake without Validator methods and assert it safely uses `create_chat_model()` and records no secret-bearing model metadata.

- [ ] **Step 2: Run workflow tests and verify RED**

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py -k "dedicated_validator or validator_prompt" -q
```

Expected: FAIL because Diagnostics always calls the main model and lacks new audit fields.

- [ ] **Step 3: Implement compatible model selection**

Use callable/property `getattr` helpers. Real `QwenOpenAIProvider` selects the dedicated model; old fake providers fall back to the existing main model/method and audit name `legacy-main-model`. Do not alter Decision generation or other Agent nodes.

- [ ] **Step 4: Add the safe JSON response example**

Serialize a fixed example with `status=valid`, Candidate Evidence IDs, empty unsupported/missing lists, and a generic public summary. The example is shape-only and contains no labels, Ground Truth, Oracle, recovery action, or scoring field. Keep the existing full public Candidate/Observation inputs and two-attempt behavior.

- [ ] **Step 5: Persist all allowlisted fields**

Copy `outcome.error_codes` into `validationErrorCodes` and the safe configured model into `validationModel` in the Step, Checkpoint, and returned state payloads. Keep single `validationErrorCode` for Provider-call compatibility.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py -k "validator or decision_validation" -q
uv run ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py
```

Expected: PASS and Ruff clean.

- [ ] **Step 7: Commit Task 3**

```powershell
git add -- apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: route semantic validation to a dedicated model"
```

### Task 4: Project validation audit into RunArtifact

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`

**Interfaces:**
- Produces `ValidationAudit(model: str | None, origin: str | None, error_category: str | None, error_codes: tuple[str, ...], error_phase: str | None, attempts: int)`.
- Extends `RunArtifact` with trailing `validation_audit: ValidationAudit | None = None` so existing constructors remain compatible.

- [ ] **Step 1: Write failing Artifact tests**

Build a v3 valid validation step containing `qwen3.8-max`, two parse codes, `retry_exhausted`, `structured_parse`, and two attempts. Assert exact projection. Add malformed/unknown strings and assert they are dropped to `None`/empty. Add a historical step without new fields and assert `validation_audit` remains compatible and decision extraction is unchanged.

- [ ] **Step 2: Run Artifact tests and verify RED**

```powershell
uv run pytest tests/test_evaluation_artifacts.py -k "validation_audit or resilient" -q
```

Expected: FAIL because RunArtifact lacks validation audit metadata.

- [ ] **Step 3: Implement the safe projection**

Read only the final `decision_validation` step. Allowlist origins, categories, phases, model strings matching the existing safe model-name character set, parse codes from Task 2, and integer attempts in `0..2`. Never copy unknown payload keys or response content.

- [ ] **Step 4: Run Task 4 tests and verify GREEN**

```powershell
uv run pytest tests/test_evaluation_artifacts.py tests/test_evaluation_scoring.py -q
uv run ruff check src/super_ai/evaluation/artifacts.py tests/test_evaluation_artifacts.py
```

Expected: PASS; scoring is byte-for-byte unaffected.

- [ ] **Step 5: Commit Task 4**

```powershell
git add -- apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/tests/test_evaluation_artifacts.py
git commit -m "feat: audit validator model and parse failures"
```

### Task 5: Local qwen3.8-max readiness, specification, and bounded acceptance

**Files:**
- Modify: `config/user.project.json` (ignored local file; never stage)
- Modify: `apps/backend/tests/test_live_llm.py`
- Modify: `openspec/changes/harden-aiops-decision-validation/specs/aiops-diagnosis-tasks/spec.md`
- Modify: `openspec/changes/harden-aiops-decision-validation/tasks.md`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Configures local `validatorModel=qwen3.8-max` and its `json_mode` profile with the existing API Key untouched.
- Adds one synthetic semantic Validator readiness using only fictional public facts/Evidence IDs.

- [ ] **Step 1: Update ignored local configuration safely**

Use `apply_patch` to add only `validatorModel` and the `qwen3.8-max` capability profile. Do not rewrite, print, stage, or commit the API Key. Load the config and print only main model, Validator model, methods, and `hasApiKey=true`.

- [ ] **Step 2: Write the live Validator readiness test**

Invoke `invoke_structured_root_cause_validation` through `live_provider.create_validator_model()` with fictional Candidate facts and `ev-synthetic-trigger/impact`. Assert decision exists, status is valid, error category is absent, and the readiness result/model audit says `qwen3.8-max`. Do not use APY data, RAG, CLS, PostgreSQL evidence, or Ground Truth.

- [ ] **Step 3: Run offline focused groups**

Group A:

```powershell
uv run pytest tests/test_llm_provider.py tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py -q
```

Group B:

```powershell
uv run pytest tests/test_evaluation_artifacts.py tests/test_evaluation_scoring.py tests/test_evaluation_cli.py tests/test_snapshot_evaluation_tools.py tests/test_live_diagnostic_adapter.py -q
```

Expected: both groups PASS. Do not replace them with full pytest.

- [ ] **Step 4: Run exactly the synthetic Validator readiness**

```powershell
uv run pytest tests/test_live_llm.py -m live_llm -k "validator" -q
```

Expected: PASS using `qwen3.8-max`. This consumes only the small Validator readiness request and does not run APY-013.

- [ ] **Step 5: Update OpenSpec and DomainBench**

Specify independent Validator selection, backwards-compatible fallback, safe parse codes, audit fields, and unchanged recovery policy. Record exact offline/readiness commands and results; explicitly state that no new APY-013 score was produced.

- [ ] **Step 6: Run final gates**

```powershell
uv run ruff check src tests
uv run pyright
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate harden-aiops-decision-validation --strict
git diff --check
git status --short
```

Expected: Ruff clean, Pyright `0 errors`, focused OpenSpec valid, no whitespace errors, and ignored `config/user.project.json` absent from Git status.

- [ ] **Step 7: Commit tracked acceptance files**

```powershell
git add -- apps/backend/tests/test_live_llm.py openspec/changes/harden-aiops-decision-validation/specs/aiops-diagnosis-tasks/spec.md openspec/changes/harden-aiops-decision-validation/tasks.md docs/aiops/agentpy-domainbench.md
git commit -m "docs: record dedicated validator readiness"
```

## Final Verification

- [ ] Re-run both offline focused groups after the documentation commit.
- [ ] Re-run Ruff, Pyright, focused OpenSpec strict, and `git diff --check`.
- [ ] Confirm Git status is clean and `config/user.project.json` is ignored.
- [ ] Do not rerun live readiness or APY-013; final reporting references the single synthetic Validator readiness from Task 5.
