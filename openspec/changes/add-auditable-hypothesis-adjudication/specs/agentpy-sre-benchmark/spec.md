## ADDED Requirements

### Requirement: Evidence-driven v4 artifacts expose auditable dispositions

`evidence-driven-v4` Artifact SHALL record graph/workflow version, every public hypothesis disposition, cited Evidence,
reason code, assessment source, model-call count, safe role audit, Validator routing, resume count, and recovery policy.

#### Scenario: Closed v4 hypothesis lacks evidence
- **WHEN** a v4 Artifact marks a hypothesis refuted or causally inactive without current-task public Evidence
- **THEN** scoring MUST fail the differential-diagnosis hard gate

#### Scenario: Sensitive execution data exists internally
- **WHEN** checkpoint blobs or provider failures contain prompts, model responses, credentials, private reasoning, or raw CLS logs
- **THEN** Artifact and public evidence-chain API MUST NOT expose those values

### Requirement: Historical artifact scoring remains stable

Benchmark SHALL dispatch v4-only semantics by explicit `workflowVersion`. Existing v2 and v3 fixtures SHALL remain
readable and preserve their prior total score, hard gates, required-evidence result, Validator interpretation, and recovery interpretation.

#### Scenario: A historical v3 artifact is rescored
- **WHEN** the upgraded scorer reads a frozen `evidence-driven-v3` fixture
- **THEN** its observable scoring result MUST equal the recorded pre-upgrade baseline
- **AND** v4 disposition requirements MUST NOT be applied to it

### Requirement: Benchmark enforces bounded and isolated execution

Benchmark SHALL reject answer access and SHALL record an efficiency failure when model-call count exceeds eight.

#### Scenario: Agent attempts answer access
- **WHEN** a scenario path traverses outside its allowlisted root, public input contains nested oracle fields, or Agent calls `ReadGroundTruth`
- **THEN** execution MUST fail the isolation gate without exposing the answer

#### Scenario: Model-call budget is exceeded
- **WHEN** an Artifact records more than eight model requests including retries
- **THEN** Benchmark MUST fail the bounded-execution gate and MUST NOT lower other thresholds to compensate
