# Grounded Causal-Chain Repair Design

## Goal

Prevent a structurally invalid LLM `causalChain` from discarding an otherwise canonical,
evidence-grounded root-cause decision. Repair only that field from already persisted Observation
summaries, then send the repaired decision through the unchanged strict validator.

## Confirmed Failure

Real run `eval-8cf07fdc70d9460ea16ec245bd0ab2d6` collected every required `APY-013`
evidence milestone. The model returned the correct component, mechanism, evidence IDs, and a
semantically useful causal narrative, but encoded the entire chain as one string. String
compatibility normalized it to one array item; deterministic validation correctly requires two to
six steps and rejected `causalChain`.

The workflow then misclassified this output-shape defect as an evidence gap, called an unrelated
metrics tool, and still produced no final decision.

## Constraints

- Do not change ground truth, scoring, thresholds, answers, evidence requirements, or canonical
  labels.
- Do not split arbitrary model prose into invented clauses.
- Do not repair component, mechanism, trigger, confidence, or evidence IDs.
- Do not call another evidence tool or model merely to repair this structural field.
- Use only persisted public hypothesis state and Observation summaries linked to the decision's
  evidence IDs.
- Preserve the existing deterministic and LLM Decision validators.
- Fail closed when grounded repair cannot produce two to six supported steps.
- Add no dependency or external service.
- Do not run another paid acceptance during this implementation.

## Selected Design

Reuse `build_grounded_fallback_decision()`, which already requires exactly one strongly supported
public hypothesis, at least two persisted evidence IDs, public canonical labels, and Observation
summaries linked to that hypothesis and evidence set.

After parsing and canonical-label normalization in the Decision node:

1. Run the existing deterministic structural check.
2. Continue normally when there are no gaps.
3. Attempt repair only when the exact gap set is `{"causalChain"}`.
4. Build the existing grounded fallback from public hypotheses, hypothesis states, Observation
   decisions, and public vocabulary.
5. Require the fallback to match the LLM decision's component and mechanism and to provide two to
   six non-empty causal steps.
6. Construct a new `RootCauseDecision` that preserves the LLM component, mechanism, trigger,
   evidence IDs, and confidence, replacing only `causal_chain` with the grounded Observation
   summaries.
7. Re-run the deterministic check. Accept the repair only when it has no remaining gap.
8. Record `decisionOrigin=llm_grounded_causal_chain_repair` for auditability.
9. Continue to the unchanged Decision Validator node.

If any condition fails, retain the existing invalid-decision and bounded Replanner behavior.

## Why This Does Not Inflate Scores

The repair has no access to `ground_truth.yaml` or evaluator milestones. It cannot choose a
hypothesis, label, or evidence item: those must already be supported and persisted before repair.
It does not turn an unsupported decision into a supported one; it converts multiple audited
Observation summaries into the array shape the validator already requires.

## Data Flow

```text
LLM decision
  -> parse + canonical-label normalization
  -> deterministic gaps == {causalChain}?
       no  -> existing path
       yes -> existing grounded fallback from persisted observations
               -> same component/mechanism and 2..6 linked summaries?
                    no  -> existing invalid/replan path
                    yes -> replace only causal_chain
                           -> deterministic check again
                           -> unchanged Decision Validator
```

## Tests and Acceptance

Offline tests must prove:

1. a one-item LLM causal chain is repaired from two or more linked supported Observation summaries;
2. component, mechanism, trigger, evidence IDs, and confidence remain unchanged;
3. the repair origin is persisted explicitly;
4. wrong labels, multiple supported hypotheses, fewer than two linked summaries, and gaps beyond
   `causalChain` fail closed;
5. valid two-to-six-item LLM causal chains remain unchanged;
6. the scripted `APY-013` workflow returns the real one-string shape, produces a validated
   decision, and never calls `GetDatabaseMetrics` or enters a decision-validation Replanner;
7. answer isolation, the relevant Snapshot suite, Ruff, strict Pyright, and the ordinary offline
   suite remain green.

No real model run is part of this change. A second paid `APY-013` acceptance requires a separate
user decision after offline evidence is reviewed.
