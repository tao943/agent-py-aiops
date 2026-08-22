# Specialist Health and Analysis Observability Design

**Date:** 2026-08-22  
**Status:** Confirmed for implementation planning  
**Scope:** AIOps V4 Multi-Agent Specialist contracts, execution, aggregation, evaluation artifacts, and Live Benchmark metrics

## 1. Problem

The final Order Pool 3x3 A/B campaign proved that the Multi-Agent workflow can collect complete evidence and produce a correct grounded diagnosis, while every Runtime and Log Specialist is reported as `inconclusive`. The single terminal status currently mixes three independent concerns:

1. whether assigned diagnostic tools completed and persisted usable evidence;
2. whether the Specialist's structured Evidence Analysis completed;
3. whether the model proposed follow-up questions.

This makes a correct `100/100 VALID_PASS` look like a fully healthy Multi-Agent run even when a Specialist analysis retries to exhaustion or reaches its hard deadline. It also makes a successful Log analysis look degraded merely because it records non-blocking follow-up questions.

The persisted checkpoints from the campaign establish the concrete failure modes:

| Role | Evidence result | Analysis result |
| --- | --- | --- |
| Runtime 01 | 3/3 tools and 3 Evidence records completed | `retry_exhausted` |
| Runtime 02 | 3/3 tools and 3 Evidence records completed | `retry_exhausted` |
| Runtime 03 | 3/3 tools and 3 Evidence records completed | `specialist_hard_deadline_expired` |
| Log 01-03 | CLS tool and Evidence completed | structured analysis returned non-empty follow-up questions |

The central Fact Adapter correctly matched `order_connection_checkout_without_checkin`, grounded trigger/mechanism/impact in four independent incident Evidence records, closed the alternatives, and emitted a `deterministic_grounded` decision. The diagnosis is therefore valid, but Specialist health is not represented accurately.

## 2. Goals

- Separate evidence collection health from model-analysis health.
- Preserve backward compatibility for existing `terminalStatus` consumers.
- Treat follow-up questions as information, not automatic analysis failure.
- Retain complete incident Evidence when structured analysis fails or times out.
- Persist bounded, secret-safe Specialist failure details in Diagnostic Steps, evaluation artifacts, PostgreSQL metrics, and the external Evaluation Archive.
- Improve structured Evidence Analysis first-pass reliability without weakening its schema or accepting free-form output.
- Make production Multi-Agent release decisions depend on Specialist health separately from diagnosis score.

## 3. Non-goals

- Do not change the root-cause scorer, Ground Truth, or score thresholds.
- Do not allow a Specialist to make the central root-cause decision.
- Do not weaken Evidence ownership, tool allowlists, tenant scope, or recovery authorization.
- Do not expose prompts, raw model responses, raw CLS logs, credentials, Oracle fields, or Chain-of-Thought.
- Do not add a new observability framework or external dependency.
- Do not enable production `auto` Multi-Agent routing in this change.

## 4. Reuse assessment

The project already contains the required primitives: `SpecialistResult`, bounded structured model invocation, LangGraph `Send` and PostgreSQL checkpointing, deterministic Evidence aggregation, safe evaluation artifacts, and immutable archive recording. These will be extended rather than replaced.

GitHub discovery considered LangGraph and OpenTelemetry. LangGraph (MIT) supplies the existing branch/checkpoint runtime but intentionally does not define domain-specific investigation health. OpenTelemetry Python (Apache-2.0) demonstrates the useful separation of execution status from events and attributes, but adopting it would add an unnecessary dependency and would not solve the Specialist contract. No compatible drop-in Specialist status contract was found. The decision is **reference only** for the status-separation principle and **custom implementation using existing project modules**.

## 5. Status contract

### 5.1 Evidence status

Add a required bounded field to `SpecialistResult`:

```python
SpecialistEvidenceStatus = Literal["complete", "partial", "none"]
```

- `complete`: every accepted local-plan tool step completed and all resulting Evidence records were persisted.
- `partial`: at least one valid Evidence record exists, but an assigned step failed, timed out, or was not started.
- `none`: no valid incident Evidence was persisted by the Specialist.

Knowledge references and pre-existing Evidence do not count toward this status.

### 5.2 Analysis status

Add a required bounded field:

```python
SpecialistAnalysisStatus = Literal[
    "complete",
    "degraded",
    "timeout",
    "failed",
    "skipped",
]
```

- `complete`: structured Evidence Analysis parsed and passed ownership and scope validation.
- `degraded`: analysis did not produce an accepted structured result after bounded correction, while usable Evidence remains.
- `timeout`: the analysis call or its correction reached the Specialist hard deadline.
- `failed`: the provider or validation path failed without a usable analysis result and is not classified as a deadline timeout.
- `skipped`: analysis was intentionally not started because evidence was absent, the model budget was unavailable, or the remaining deadline could not support one complete attempt.

### 5.3 Backward-compatible terminal status

Keep the current `terminalStatus` field and derive it conservatively:

| Evidence | Analysis | Terminal status |
| --- | --- | --- |
| complete | complete | completed |
| complete | degraded/timeout/failed/skipped | inconclusive |
| partial | any | inconclusive |
| none | timeout | timeout |
| none | failed/skipped/degraded | failed |

The Aggregator may use valid Evidence from `completed` and `inconclusive` branches, but it must never reinterpret an analysis-degraded branch as fully healthy.

### 5.4 Follow-up questions

`unresolvedQuestions` remains bounded public output but no longer determines `terminalStatus`. A successfully parsed and scope-valid Evidence Analysis is `analysisStatus=complete` even when it contains follow-up questions. The questions are advisory and cannot create positive Evidence, change hypothesis disposition, or authorize recovery.

Persist only:

- a bounded `followUpQuestionCount` in terminal metrics;
- bounded public questions in checkpoints and the Diagnostic Step where already allowed;
- no unbounded question text in the Evaluation Artifact or Archive.

## 6. Structured Evidence Analysis reliability

The Specialist Evidence Analysis role will reuse the existing bounded structured invocation helper and provider-specific structured-output method. Its prompt will add one explicit public JSON contract and a synthetic example containing no scenario answer or real Evidence ID.

The contract will state that:

- every referenced Evidence ID must be owned by the Specialist;
- every proposed hypothesis must be in the assignment;
- follow-up questions are optional and do not mean the assigned evidence collection failed;
- extra fields and free-form wrappers are forbidden.

One correction attempt remains allowed. It starts only when the remaining hard-deadline window is at least one full role timeout plus a fixed scheduling margin. Otherwise the role returns `retry_skipped_insufficient_deadline` without making another provider call. The first failure category and the final safe error category are both retained.

Raw provider responses and exception messages are never persisted. Safe analysis error codes are allowlisted and include:

- `parse_error`
- `schema_validation_failed`
- `scope_rejected`
- `provider_4xx`
- `provider_5xx`
- `provider_timeout`
- `retry_exhausted`
- `retry_skipped_insufficient_deadline`
- `specialist_soft_deadline_expired`
- `specialist_hard_deadline_expired`
- `specialist_model_budget_exhausted`

## 7. Aggregation and central decision

The Evidence Aggregator continues to validate Evidence ownership, completed tool-call audits, allowed tools, source fingerprints, duplicates, and conflicts. It additionally projects per-role evidence and analysis health.

The central Fact Adapter, trusted-pattern resolver, Sufficiency Gate, Decision, deterministic Validator, optional semantic Validator, and Recovery Policy remain authoritative and unchanged. Specialist analysis output remains untrusted advice. A degraded Specialist cannot lower the evidence requirements for a central grounded decision.

If all mandatory roles have `evidenceStatus=none`, the existing `multi_investigation_failed` behavior remains. Partial or degraded evidence may support a proposal or manual review only when the existing central gates allow it.

## 8. Persistence and evaluation projection

Each role entry in the `evidence_aggregator` Diagnostic Step adds:

```text
evidenceStatus
analysisStatus
analysisErrorCode
analysisAttemptCount
followUpQuestionCount
softDeadlineExceeded
hardDeadlineExceeded
completedToolCount
expectedToolCount
```

The Aggregator summary adds bounded maps and counts for the same fields. `build_run_artifact()` accepts only known roles, enum values, non-negative bounded counts, and allowlisted safe error codes. Invalid or private values are dropped or normalized according to the existing artifact safety pattern.

Live evaluation metrics add:

- `specialistEvidenceStatuses`
- `specialistAnalysisStatuses`
- `specialistAnalysisErrorCodes`
- `specialistAnalysisAttemptCounts`
- `specialistFollowUpQuestionCounts`
- `specialistEvidenceCompletionBasisPoints`
- `specialistAnalysisCompletionBasisPoints`
- `specialistDegradationBasisPoints`
- `specialistDeadlineHitBasisPoints`
- `specialistStructuredRetryBasisPoints`

Existing fields remain readable for historical runs. Missing new fields on old artifacts are interpreted as unknown, not as success or failure. Historical immutable artifacts are never rewritten.

## 9. Release gate

Diagnosis score and Specialist health are separate gates.

A Benchmark forced-Multi run may pass diagnosis scoring when the central evidence chain is valid, even if a Specialist analysis is degraded. Its health metrics must still expose that degradation.

Production `auto` Multi-Agent remains disabled. A future enablement decision requires, over an approved A/B campaign:

- all mandatory roles have `evidenceStatus=complete`;
- all mandatory roles have `analysisStatus=complete`;
- no Specialist deadline hit;
- no reduction in Root Cause Top-1, Evidence Recall, safety, verification, or cleanup;
- at least one confirmed capability gain that is not merely a scoring projection difference.

## 10. Error handling

- Tool failure preserves already persisted Evidence and produces `partial` rather than discarding the branch.
- Analysis failure never invents facts and never deletes tool Evidence.
- Deadline expiry prevents new model calls but does not cancel completed persistence.
- Replay and retry preserve the existing Specialist execution keys and do not duplicate model calls, tools, Evidence, or audit events.
- One failed branch cannot authorize recovery; existing central safety gates remain mandatory.

## 11. Testing and acceptance

Implementation follows test-first development.

Focused tests must prove:

1. complete tools plus successful analysis and follow-up questions produce `evidenceStatus=complete`, `analysisStatus=complete`, and `terminalStatus=completed`;
2. complete tools plus `retry_exhausted` preserve Evidence and produce `complete/degraded/inconclusive`;
3. complete tools plus analysis hard deadline produce `complete/timeout/inconclusive`;
4. partial tool completion produces `partial` regardless of analysis text;
5. no Evidence plus timeout or failure preserves existing terminal safety semantics;
6. a correction retry starts only with a complete remaining deadline window;
7. Aggregator Step and checkpoints persist bounded per-role health and safe reason codes;
8. Artifact and Archive project the new metrics without raw questions, prompts, responses, credentials, Oracle fields, or unknown roles;
9. historical artifacts without new fields remain readable;
10. central deterministic resolution and recovery authorization are unchanged;
11. concurrent/replayed branches do not duplicate health records or model budgets.

After offline tests, run one real Order Pool forced-Multi canary with the same active 30-card RAG, CLS, models, tools, and scorer. Acceptance requires:

- Runtime and Log `evidenceStatus=complete`;
- Log `analysisStatus=complete` even if it records follow-up questions;
- Runtime analysis status and exact safe error code visible in PostgreSQL and Archive;
- Root Cause Top-1 and Evidence Recall remain correct;
- safety, verification, and cleanup pass;
- no claim that production `auto` Multi is enabled.

## 12. Security and data boundaries

Only public, task-scoped Evidence IDs and allowlisted status metadata may cross Specialist boundaries. The new fields must not include raw model output, full prompts, exception bodies, credentials, Ground Truth, Oracle data, or hidden reasoning. Checkpoint deserialization remains tenant- and task-scoped. Evaluation projection remains an allowlist, not a generic payload copy.
