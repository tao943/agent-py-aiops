# Snapshot Decision and Evidence Refinement Design

## 1. Goal

Complete the real `APY-013` Snapshot diagnosis after Tool Calling has been repaired. The workflow
must collect the remaining evidence that refines its supported hypothesis and emit a parseable,
canonical root-cause decision without changing ground truth, scoring, thresholds, or answers.

The first paid acceptance gate remains `APY-013`. `APY-014` through `APY-016` may be sampled only
after `APY-013` passes.

## 2. Observed Failure

The real worktree run proved that the model selected and successfully called
`InspectPostgresErrors` and `InspectPostgresWaitGraph`. Those observations supported
`postgres_deadlock` and refuted the main alternatives. The Sufficiency Gate then routed directly
to Decision even though the existing plan still contained `InspectTransactionResourceOrder`, a
step that tests the supported deadlock hypothesis and distinguishes opposite-order acquisition.

The Decision model returned correct persisted evidence IDs, but:

- it used generic `postgres` / `deadlock` labels because Snapshot exposed no public canonical
  decision vocabulary;
- it returned `causalChain` as a string rather than an array, so strict parsing rejected the whole
  decision.

These are public-contract and control-flow defects, not reasons to weaken the evaluator.

## 3. Constraints

- Do not modify `ground_truth.yaml`, evaluator logic, score weights, pass thresholds, required
  evidence, or canonical correct answers.
- Do not reveal evidence IDs, correct-hypothesis markers, answer priority, recovery answers, or
  oracle fields to the Agent.
- Give every public candidate hypothesis the same decision-label structure.
- Do not force execution of every remaining tool or every planned step.
- Preserve the six-attempt executor budget, exact Snapshot tool matching, answer isolation, and
  persisted audit chain.
- Ordinary CI remains offline and requires no paid model, PostgreSQL service, CLS, or Docker.
- Add no dependency or external service.

## 4. Reuse Assessment

The project already contains all required primitives:

- Live scenarios expose candidate-wide `labelsByHypothesis`, `componentAliases`, and
  `mechanismAliases` without giving the Agent the correct candidate.
- Every diagnostic plan step already declares `testsHypotheses`.
- Hypothesis states already distinguish `supported`, `refuted`, and `open` candidates.
- The Sufficiency Gate already owns routing after each persisted observation.
- Root-cause parsing and deterministic validation already fail closed.

GitHub references checked before design:

- `microsoft/AIOpsLab` (MIT) separates public agent interaction from benchmark evaluation and
  emphasizes reproducible evidence environments. Direct adoption would add a Python 3.11 and
  Kubernetes/Helm runtime that is unnecessary for this bounded Snapshot repair.
- `langchain-ai/langgraph` (MIT), already installed, supports deterministic conditional routing
  over persisted graph state.
- `modelcontextprotocol/python-sdk` (MIT), already represented through the project's MCP boundary,
  keeps tool inputs explicit and schema-bound.

The selected outcome is internal reuse with a small custom policy. No new package is adopted.

## 5. Considered Approaches

### 5.1 Prompt-only retry

Tell the model to use exact labels, arrays, and more evidence. This is provider-sensitive, consumes
quota, and cannot supply labels that were never public. Rejected.

### 5.2 Execute every remaining plan step

Continue the entire plan after the model reports sufficient evidence. This would collect unrelated
facts, spend the fixed budget mechanically, and could inflate evidence scores. Rejected.

### 5.3 Candidate vocabulary plus supported-hypothesis refinement — selected

Expose equal canonical labels for all candidates. When the Gate reports sufficient evidence, run
only an unexecuted pre-existing step whose `testsHypotheses` intersects the supported set. Once no
such step remains, proceed to Decision. Accept a string `causalChain` as one sequence item while
retaining all existing downstream validation.

## 6. Public Candidate Contract

Extend `PublicHypothesis` with one public decision label:

```yaml
hypotheses:
  - id: postgres_deadlock
    description: Concurrent transactions deadlocked.
    decision_label:
      component: order-service
      mechanism: opposite_order_transaction_deadlock
```

Every hypothesis in every Snapshot scenario must contain exactly one non-empty `component` and
`mechanism`. The loader rejects partial labels and duplicate hypothesis IDs as configuration
errors. The diagnostic input derives:

- `labelsByHypothesis`: the canonical pair for every candidate;
- `componentAliases`: canonical components map to themselves;
- `mechanismAliases`: every hypothesis ID and canonical mechanism maps to that candidate's
  canonical mechanism.

No alias or label is generated from `ground_truth.yaml`. Scenario authors declare all candidates
in the public file. Answer-isolation tests must prove nested oracle-like fields remain rejected and
the vocabulary contains no evidence/result/oracle keys.

## 7. Supported-Hypothesis Refinement

After parsing a valid Sufficiency decision:

1. Read the current supported hypothesis IDs.
2. Inspect only the unexecuted suffix of the current plan.
3. Select the first step whose `testsHypotheses` intersects the supported IDs and whose normalized
   fingerprint has not already executed.
4. Route to Executor without changing the plan or attempt budget.
5. Skip remaining steps that test only refuted or unresolved alternatives.
6. Route to Decision when no supported-hypothesis refinement step remains.

This policy does not know benchmark milestones or correct answers. It only honors a diagnostic
commitment already made by the Agent and a step already proposed by the Agent. In `APY-013`, the
existing `InspectTransactionResourceOrder` step refines `postgres_deadlock`; a metrics step aimed
only at discarded alternatives does not.

The Sufficiency audit payload records `nextRoute` and a bounded `refinementReason`, making the
extra evidence collection visible. The executor still performs argument normalization, MCP schema
validation, exact Snapshot matching, and normal audit persistence.

## 8. Decision Output Compatibility

The root-cause parser accepts either:

- the existing JSON array of non-empty strings; or
- one non-empty string, normalized to a one-item tuple.

All other types remain invalid. This is a provider-output compatibility rule only. It does not
split, invent, or rewrite causal content. The existing deterministic validator still enforces its
causal-chain length and support rules; the fallback/replanning path therefore continues to fail
closed when one item is not sufficient.

## 9. Data Flow

```text
Public scenario candidates + equal canonical labels
  -> candidate-wide decisionVocabulary
  -> Planner creates hypothesis-targeted steps
  -> Executor persists evidence and normalized audit
  -> Evidence Evaluator updates hypothesis states
  -> Sufficiency Gate says sufficient
  -> remaining step refines supported hypothesis?
       yes -> Executor -> Evidence Evaluator -> Sufficiency Gate
       no  -> Decision
  -> tolerant parse, strict deterministic validation
  -> unchanged Snapshot scoring against private oracle
```

## 10. Testing and Acceptance

Offline tests must prove:

1. all ten Snapshot scenarios contain complete labels for every public candidate;
2. the generated vocabulary is candidate-wide and contains no oracle/evidence/result fields;
3. missing or partial public labels fail scenario loading;
4. a sufficient decision routes to Executor when an unexecuted step targets a supported
   hypothesis;
5. steps targeting only refuted or unresolved candidates are skipped;
6. executed duplicates are not selected and do not consume budget;
7. no matching refinement step routes directly to Decision;
8. string `causalChain` parses as one item while empty strings and non-string/non-array values fail;
9. existing array parsing and deterministic validation remain unchanged;
10. an offline `APY-013` scripted workflow collects both wait-cycle and transaction-resource-order
    evidence without reading the oracle;
11. answer-isolation, Ruff, and strict Pyright remain green.

After offline gates pass, run real `APY-013` once with the worktree `apps/backend/src` first on
`PYTHONPATH`, the base `config/project.json`, and the existing active indexed 30-card RAG database.
Do not run later scenarios if it fails. If it passes, sample `APY-014` through `APY-016`
sequentially and stop at the first repeatable defect or infrastructure-invalid result.

## 11. Non-goals

- Rewriting Snapshot answers or making required evidence optional.
- Generating public labels dynamically from the oracle.
- Executing tools not already present in the Agent plan.
- Adding an evaluator-aware milestone planner.
- Changing RAG content, retrieval scoring, recovery policy, or Live scenario drivers.
- Replacing LangGraph, MCP, or the existing Decision validator.
