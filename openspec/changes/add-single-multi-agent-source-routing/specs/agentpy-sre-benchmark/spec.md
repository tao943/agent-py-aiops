## ADDED Requirements

### Requirement: Investigation strategy override is benchmark-only

The internal Live Benchmark CLI SHALL support `auto`, `single`, and `multi` investigation strategies.
Ordinary diagnosis API clients MUST NOT force an investigation strategy.

#### Scenario: A benchmark run forces Single
- **WHEN** the internal CLI runs with `--strategy single`
- **THEN** the diagnostic execution SHALL retain all safety gates but bypass the Multi score threshold
- **AND** the terminal result SHALL record the requested and effective strategies

#### Scenario: A normal API request contains a strategy field
- **WHEN** an ordinary client attempts to force `multi`
- **THEN** the API SHALL reject or ignore the field according to its fixed request-schema policy
- **AND** the service-owned strategy mode SHALL remain `auto`

#### Scenario: Forced Multi fails a hard gate
- **WHEN** Benchmark requests `multi` but time, budget, capability, or source-count gates fail
- **THEN** Workflow MUST refuse Multi
- **AND** it SHALL persist the hard-gate reason

### Requirement: Strategy comparison uses persisted safe metrics

Every strategy run SHALL persist enough bounded metrics to reconstruct a paired A/B comparison after
process restart without storing Oracle labels, required Evidence identifiers, Prompt text, or raw logs.

#### Scenario: Runtime objects are discarded after a run
- **WHEN** an evaluator completes a run and the in-memory RunArtifact is no longer available
- **THEN** the system SHALL reconstruct strategy, policy, correctness, Evidence Recall, duration, model-call count, duplicate-Evidence rate, fallback, and security hard-gate values from persisted owner-scoped records

#### Scenario: Sensitive evaluator content is offered to terminal persistence
- **WHEN** terminal metrics or metadata contain expected cause, required Evidence, Oracle, Prompt, credential, or raw-log shaped data
- **THEN** persistence MUST reject the payload

### Requirement: Multi-agent default requires measured benefit

Multi-Agent MUST NOT become the default production route unless paired runs satisfy both capability and
performance gates with no safety failure.

#### Scenario: Performance passes without capability gain
- **WHEN** Multi P95 is at most 1.5 times Single and extra model calls are at most 2 but neither Evidence Recall improves by 10 percentage points nor Root Cause Top-1 improves by 5 percentage points
- **THEN** the result SHALL remain `benchmark_only`

#### Scenario: Capability and performance gates pass
- **WHEN** paired runs satisfy a required capability gain, Multi P95 is at most 1.5 times Single, duplicate Evidence is at most 10 percent, extra model calls are at most 2, and every safety hard gate passes
- **THEN** the result MAY become `eligible_for_default_review`
- **AND** production default enablement SHALL still require an explicit decision
