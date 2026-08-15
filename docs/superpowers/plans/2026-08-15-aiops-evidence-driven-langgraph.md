# Evidence-Driven AIOps LangGraph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Implementation stays with the primary agent after the one required plan-review subagent.

**Goal:** Replace the fixed AIOps diagnostic sequence with a bounded evidence-sufficiency loop, real replanning, root-cause validation, and policy-controlled recovery proposals without changing benchmark answers or scoring.

**Architecture:** Extend the existing typed reasoning contracts, then add `sufficiency_gate`, a real `replanner`, `decision_validator`, `recovery_planner`, and `policy_gate` nodes to `AiopsDiagnosticService`. Reuse the current LangGraph, MCP, persistence, audit, Snapshot, and Live recovery boundaries; the graph may record a whitelisted side-effect-free proposal but never execute an infrastructure mutation.

**Tech Stack:** Python 3.10+, LangGraph 1.2.8+, LangChain 1.3.12+, FastAPI, PostgreSQL repositories, MCP tool contracts, pytest, Ruff, strict Pyright.

## Global Constraints

- Add no dependency or external service.
- The initial diagnostic plan contains at most 4 steps; all persisted executor attempts, including rejected duplicates and failed tools, total at most 6; at most 2 replans are allowed.
- Ordinary CI remains offline and calls no real DashScope, CLS, Docker, Milvus, Alertmanager, or MCP server.
- Ground truth, scorer failures, private Live handles, and oracle recovery policy never enter Agent state, prompts, RAG, checkpoints, or reports.
- Do not change scenario files, ground truth, score weights, `bounded_plan <= 6`, or pass thresholds.
- Nginx remains proposal-only; no write, reload, restart, signal, or route switch is permitted.
- PostgreSQL and Redis mutations remain exclusively controlled by their existing deterministic Live recovery services.
- Persist structured decisions and bounded summaries only; never persist private chain-of-thought or raw model output.
- Do not run paid LLM/CLS acceptance without separate explicit approval.

## File Structure

- Modify `apps/backend/src/super_ai/aiops/reasoning.py`: typed model-output contracts and strict parsers.
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: graph state, routing, node implementations, tool policy, persistence, prompts, and bounded loop helpers.
- Modify `apps/backend/src/super_ai/aiops/__init__.py`: export the new public audit types used by artifacts/tests.
- Modify `apps/backend/src/super_ai/evaluation/live/diagnostics.py`: mark only the existing Nginx proposal tool as request-scoped `proposal_only`.
- Modify `apps/backend/src/super_ai/evaluation/artifacts.py`: ignore invalidated decisions and count all executed diagnostic steps without exceeding six.
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: parser, routing, replanning, validation, budget, and answer-isolation coverage.
- Modify `apps/backend/tests/test_aiops_diagnostics.py`: service persistence, fallback, checkpoint, and report regression coverage.
- Modify `apps/backend/tests/test_live_diagnostic_adapter.py`: proposal policy mapping and Nginx audit compatibility.
- Modify `apps/backend/tests/test_evaluation_artifacts.py`: invalid decision and replanned-step artifact compatibility.

---

### Task 1: Add strict workflow decision contracts

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Modify: `apps/backend/src/super_ai/aiops/__init__.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Consumes: existing `_json_mapping`, `_required_str`, `_string_tuple`, `RootCauseDecision`.
- Produces: `EvidenceSufficiencyDecision`, `RootCauseValidationDecision`, `RecoveryPlan`, `RecoveryPolicyDecision`, `parse_evidence_sufficiency`, `parse_root_cause_validation`, and `parse_recovery_plan`.

- [ ] **Step 1: Write failing parser tests**

Add imports and tests that pin known IDs, bounded lists, proposal arguments, and exact status vocabularies:

```python
from super_ai.aiops.reasoning import (
    parse_evidence_sufficiency,
    parse_recovery_plan,
    parse_root_cause_validation,
)


def test_sufficiency_rejects_unknown_evidence_hypotheses_and_tools() -> None:
    payload = json.dumps(
        {
            "status": "insufficient",
            "evidenceIds": ["fabricated"],
            "supportedHypotheses": ["unknown"],
            "refutedHypotheses": [],
            "unresolvedHypotheses": ["h-open"],
            "missingEvidence": ["Read the lock graph."],
            "recommendedTools": ["Shell"],
            "summary": "More evidence is required.",
        }
    )
    with pytest.raises(ValueError):
        parse_evidence_sufficiency(
            payload,
            available_evidence_ids={"ev-1"},
            known_hypotheses={"h-open"},
            available_tools={"InspectPostgresLockGraph"},
        )


def test_root_cause_validation_limits_unsupported_fields() -> None:
    with pytest.raises(ValueError, match="unsupported field"):
        parse_root_cause_validation(
            json.dumps(
                {
                    "status": "invalid",
                    "evidenceIds": ["ev-1"],
                    "unsupportedFields": ["privateReasoning"],
                    "missingEvidence": ["A direct trigger observation is missing."],
                    "summary": "The trigger is unsupported.",
                }
            ),
            available_evidence_ids={"ev-1"},
        )


def test_recovery_plan_requires_schema_valid_proposal_fields() -> None:
    plan = parse_recovery_plan(
        json.dumps(
            {
                "mode": "proposal_only",
                "action": "propose_nginx_timeout_mitigation",
                "target": "live_eval_upstream",
                "rationale": "The upstream response exceeded the read timeout.",
                "tool": "ProposeNginxTimeoutMitigation",
                "arguments": {
                    "target": "live_eval_upstream",
                    "risk": "A larger timeout can retain connections longer.",
                    "rollback": "Restore the previous timeout after approval.",
                    "verificationSteps": [
                        "Repeat the gateway probe.",
                        "Confirm the upstream latency is within the approved timeout.",
                    ],
                    "humanApprovalRequired": True,
                },
                "risk": "A larger timeout can retain connections longer.",
                "rollback": "Restore the previous timeout after approval.",
                "verificationSteps": [
                    "Repeat the gateway probe.",
                    "Confirm the upstream latency is within the approved timeout.",
                ],
                "evidenceIds": ["ev-1"],
                "decisionConfidence": 0.91,
                "humanApprovalRequired": True,
            }
        ),
        available_evidence_ids={"ev-1"},
        proposal_tools={"ProposeNginxTimeoutMitigation"},
    )
    assert plan.mode == "proposal_only"
    assert plan.tool == "ProposeNginxTimeoutMitigation"
    assert plan.human_approval_required is True
```

- [ ] **Step 2: Run the parser tests and confirm red state**

Run:

```powershell
Set-Location apps/backend
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-contracts-red
```

Expected: collection fails because the new symbols do not exist.

- [ ] **Step 3: Add immutable decision types and parsers**

Add these public dataclasses and use the existing JSON helpers for parsing:

```python
@dataclass(frozen=True, slots=True)
class EvidenceSufficiencyDecision:
    status: Literal["sufficient", "insufficient"]
    evidence_ids: tuple[str, ...]
    supported_hypotheses: tuple[str, ...]
    refuted_hypotheses: tuple[str, ...]
    unresolved_hypotheses: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    recommended_tools: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class RootCauseValidationDecision:
    status: Literal["valid", "invalid"]
    evidence_ids: tuple[str, ...]
    unsupported_fields: tuple[
        Literal["component", "mechanism", "trigger", "causalChain"], ...
    ]
    missing_evidence: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    mode: Literal[
        "no_action", "proposal_only", "external_policy_required", "manual_review"
    ]
    action: str
    target: str
    rationale: str
    tool: str | None
    arguments: dict[str, object]
    risk: str
    rollback: str
    verification_steps: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    decision_confidence: float
    human_approval_required: bool


@dataclass(frozen=True, slots=True)
class RecoveryPolicyDecision:
    status: Literal["allowed", "denied", "deferred"]
    authorization_code: str
    execution_permitted: bool
    proposal_recorded: bool
    human_approval_required: bool
    summary: str
```

Implement the three parsers with these exact validation rules:

```python
_ROOT_CAUSE_FIELDS = {"component", "mechanism", "trigger", "causalChain"}
_RECOVERY_MODES = {
    "no_action",
    "proposal_only",
    "external_policy_required",
    "manual_review",
}
_MAX_AUDIT_ITEMS = 6


def _known_strings(
    payload: Mapping[str, object],
    key: str,
    *,
    allowed: Set[str],
    label: str,
) -> tuple[str, ...]:
    values = _string_tuple(payload, key)
    if len(values) > _MAX_AUDIT_ITEMS:
        raise ValueError(f"{label} cannot contain more than {_MAX_AUDIT_ITEMS} items.")
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"{label} references unknown value: {', '.join(sorted(unknown))}.")
    return values
```

`parse_recovery_plan` must require `tool` to be a known proposal tool only in
`proposal_only` mode; other modes require `tool` to be absent or null. It must require a numeric
confidence from zero through one, persisted evidence IDs, a boolean approval flag, and at least two
verification steps for `proposal_only` and `external_policy_required`.

- [ ] **Step 4: Export types and run green tests**

Export the four dataclasses from `super_ai.aiops.__init__`, then run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-contracts-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/aiops/reasoning.py src/super_ai/aiops/__init__.py tests/test_aiops_reasoning_trace.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/aiops/reasoning.py src/super_ai/aiops/__init__.py tests/test_aiops_reasoning_trace.py
```

Expected: all commands pass with zero Pyright errors.

- [ ] **Step 5: Commit typed contracts**

```powershell
git add apps/backend/src/super_ai/aiops/reasoning.py apps/backend/src/super_ai/aiops/__init__.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: add aiops workflow decision contracts"
```

---

### Task 2: Add the evidence sufficiency gate and real replanning loop

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`

**Interfaces:**
- Consumes: Task 1 `EvidenceSufficiencyDecision` and `parse_evidence_sufficiency`.
- Produces: `sufficiency_gate` and real `replanner` checkpoints, maximum-six executor-attempt loop, and `terminationReason`.

- [ ] **Step 1: Write a failing service test for gap-targeted replanning**

Create a scripted chat model whose initial plan contains one session inspection, whose first
sufficiency response requests `InspectPostgresLockGraph`, and whose replan adds that exact tool.
The test must assert:

```python
assert [item.tool_name for item in snapshot.observations] == [
    "InspectPostgresSessions",
    "InspectPostgresLockGraph",
]
assert [step.phase for step in steps] == [
    "planner",
    "executor",
    "evidence_evaluation",
    "sufficiency_gate",
    "replanner",
    "executor",
    "evidence_evaluation",
    "sufficiency_gate",
    "decision",
    "report",
]
replanner = next(step for step in steps if step.phase == "replanner")
assert replanner.payload["reason"] == "evidence_gap"
assert replanner.payload["addedStepCount"] == 1
assert replanner.payload["replanCount"] == 1
```

The scripted model must answer each prompt by detecting these stable phrases:

```python
if "evidence sufficiency decision" in prompt:
    return json.dumps(sufficiency_responses.pop(0))
if "gap-targeted diagnostic replan" in prompt:
    return json.dumps({"steps": [lock_graph_step]})
```

- [ ] **Step 2: Write failing budget and duplicate-call tests**

Add tests proving that a replan returning the already executed tool and arguments is rejected and
that the graph stops after two replans or six persisted executor attempts. Include an initial plan
with one duplicate, then try to append enough new steps to exceed six; assert the duplicate consumes
one attempt even though it does not call MCP. Assert the final report payload
contains one of these bounded reasons:

```python
assert report.payload["terminationReason"] in {
    "evidence_sufficient",
    "no_useful_step",
    "replan_limit_reached",
    "step_budget_exhausted",
}
assert len([step for step in steps if step.phase == "executor"]) <= 6
assert len([step for step in steps if step.phase == "replanner"]) <= 2
assert len({audit.tool_name + json.dumps(audit.arguments, sort_keys=True) for audit in audits}) == len(audits) - 1
```

Subtract one because the knowledge retrieval audit is not a diagnostic plan step.

- [ ] **Step 3: Run the new tests and confirm red state**

```powershell
Set-Location apps/backend
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-replan-red
```

Expected: failures show the old `replanner` only advances the fixed plan and no
`sufficiency_gate` checkpoint exists.

- [ ] **Step 4: Extend graph state and initialize fixed budgets**

Add state fields with exact types:

```python
tool_definitions: tuple[McpToolDefinition, ...]
evidence_sufficiency: JsonDict
next_route: Literal["executor", "replanner", "decision", "recovery_planner", "report"]
replan_count: int
max_replans: int
max_total_steps: int
executed_step_fingerprints: Annotated[list[str], add]
executor_attempt_count: int
termination_reason: str
```

Initialize `replan_count=0`, `max_replans=2`, `max_total_steps=6`,
`executor_attempt_count=0`, and an empty fingerprint list. Persist
`workflowVersion="evidence-driven-v2"` in the planner payload and checkpoint.
Change the initial planning prompt and `parse_plan` call boundary so the initial plan is capped at
four steps; retain the parser's absolute six-step safety cap for replans and legacy callers.

- [ ] **Step 5: Add stable duplicate detection**

Add a pure helper and call it before every execution and while accepting replanned steps:

```python
def _step_fingerprint(step: Mapping[str, object]) -> str:
    canonical = {
        "tool": str(step.get("tool") or ""),
        "arguments": _json_dict(step.get("arguments")),
    }
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

Every entry into Executor increments `executor_attempt_count` before duplicate detection or MCP
execution. An executor receiving a duplicate must not call MCP. It records an `executor` step with
`status="failed"`, `errorCategory="duplicate_step"`, advances the plan index, consumes one of the
six attempts, and routes through the sufficiency gate. When the count reaches six, no further
executor node may be entered.

- [ ] **Step 6: Replace the fixed replanner graph edges**

Build this exact node and edge layout:

```python
graph.add_node("planner", self._planner)
graph.add_node("executor", self._executor)
graph.add_node("evidence_evaluator", self._evidence_evaluator)
graph.add_node("sufficiency_gate", self._sufficiency_gate)
graph.add_node("replanner", self._replanner)
graph.add_node("decision", self._decision)
graph.add_node("report", self._report)
graph.add_edge(START, "planner")
graph.add_edge("planner", "executor")
graph.add_edge("executor", "evidence_evaluator")
graph.add_edge("evidence_evaluator", "sufficiency_gate")
graph.add_conditional_edges(
    "sufficiency_gate",
    self._route_after_sufficiency,
    {"executor": "executor", "replanner": "replanner", "decision": "decision"},
)
graph.add_conditional_edges(
    "replanner",
    self._route_after_replanner,
    {"executor": "executor", "decision": "decision"},
)
graph.add_edge("decision", "report")
graph.add_edge("report", END)
```

- [ ] **Step 7: Implement sufficiency routing**

The gate prompt includes only public hypotheses, hypothesis states, observation decisions,
persisted evidence IDs and bounded summaries, remaining plan tools, and discovered tool names. It
must contain the stable phrase `evidence sufficiency decision`.

Routing rules are deterministic after parsing:

```python
if parsed.status == "sufficient":
    next_route = "decision"
elif _has_unique_remaining_plan_step(state):
    next_route = "executor"
elif _can_replan(state):
    next_route = "replanner"
else:
    next_route = "decision"
```

On model or parse failure, do not mark sufficient. Return an insufficient fallback built from open
hypotheses and route according to remaining unique steps and budgets.

- [ ] **Step 8: Implement gap-targeted replanning**

The replan prompt contains the stable phrase `gap-targeted diagnostic replan`, the latest
sufficiency/validation gaps, public hypotheses, discovered contracts, trusted argument binding,
and executed fingerprints. Parse with existing `parse_plan`, discard duplicate fingerprints, cap
accepted steps to the remaining `6 - executor_attempt_count` budget, append them to the current plan, and
increment `replan_count` exactly once.

If no valid new step remains, append no executor call and set
`termination_reason="no_useful_step"`. Implement `_route_after_replanner` to return `executor`
only when the appended plan has an unexecuted unique step; otherwise return `decision`. Register
both destinations with the conditional edge shown in Step 6.

- [ ] **Step 9: Run targeted tests and commit the loop**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-replan-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_diagnostics.py
git commit -m "feat: replan aiops diagnostics from evidence gaps"
```

Expected: targeted tests, Ruff, and Pyright pass.

---

### Task 3: Validate root-cause decisions before recovery planning

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`

**Interfaces:**
- Consumes: Task 1 `RootCauseValidationDecision`, Task 2 replanning budgets and routing.
- Produces: persisted `decision_validation`, invalid-decision clearing, and validation-driven replanning.

- [ ] **Step 1: Write failing invalid-trigger and valid-decision tests**

Add one scripted run in which Decision returns a root cause with a trigger not supported by any
observation and validation returns:

```python
{
    "status": "invalid",
    "evidenceIds": [evidence_id],
    "unsupportedFields": ["trigger", "causalChain"],
    "missingEvidence": ["Observe the initiating lock holder and resulting wait chain."],
    "summary": "The selected evidence shows impact but not the primary trigger.",
}
```

Assert it routes to one replan when budget remains. Add another run returning `valid` and assert it
reaches `recovery_planning` without another evidence call.

- [ ] **Step 2: Write a failing exhaustion test**

Exhaust the six-step or two-replan budget, return an invalid validation, then assert:

```python
assert report.payload["rootCauseDecision"] is None
assert report.payload["decisionValidation"]["status"] == "invalid"
assert report.payload["terminationReason"] in {
    "replan_limit_reached",
    "step_budget_exhausted",
    "unsupported_decision",
}
assert "证据不足" in report.content
```

- [ ] **Step 3: Run tests and confirm red state**

```powershell
Set-Location apps/backend
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-validator-red
```

Expected: no production `decision_validation` step exists and unsupported decisions remain in the
report payload.

- [ ] **Step 4: Implement deterministic prechecks and semantic validation**

First insert `decision_validator` between Decision and Report while preserving the Task 2 replan
edge:

```python
graph.add_node("decision_validator", self._decision_validator)
graph.add_edge("decision", "decision_validator")
graph.add_conditional_edges(
    "decision_validator",
    self._route_after_decision_validation,
    {"replanner": "replanner", "report": "report"},
)
```

Remove the Task 2 direct `decision -> report` edge.

Before calling the validation model, construct invalid output without a model call when any of
these conditions hold:

```python
def _deterministic_decision_gaps(
    decision: RootCauseDecision,
    *,
    decision_vocabulary: Mapping[str, object],
) -> tuple[str, ...]:
    gaps: list[str] = []
    if not decision.trigger.strip():
        gaps.append("trigger")
    if len(decision.causal_chain) < 2 or len(decision.causal_chain) > 6:
        gaps.append("causalChain")
    labels = _json_dict(decision_vocabulary.get("labelsByHypothesis"))
    if labels and not _decision_uses_public_label(decision, labels):
        gaps.extend(["component", "mechanism"])
    return tuple(dict.fromkeys(gaps))


def _decision_uses_public_label(
    decision: RootCauseDecision,
    labels: Mapping[str, object],
) -> bool:
    for value in labels.values():
        candidate = _json_dict(value)
        if (
            candidate.get("component") == decision.component
            and candidate.get("mechanism") == decision.mechanism
        ):
            return True
    return False
```

Unknown evidence IDs are already rejected by `parse_root_cause_decision`. If a future or legacy
candidate reaches validation with an evidence-reference problem, represent it as
`status="invalid"`, `unsupported_fields=()`, and a bounded `missing_evidence` message. Do not add
`evidenceIds` to the accepted unsupported-field vocabulary.

When deterministic checks pass, call the model with the stable phrase
`root-cause validation decision`. Include the candidate decision, public hypotheses, structured
observations, hypothesis states, and bounded evidence summaries only. Parse with
`parse_root_cause_validation`.

- [ ] **Step 5: Route invalid decisions safely**

Persist the validation step and checkpoint. For invalid decisions with resolvable missing evidence
and remaining budget, set `next_route="replanner"` and make the replanner use
`decision_validation` gaps. When budget is exhausted, return
`root_cause_decision=None`, `termination_reason="unsupported_decision"`, and route to `report`.
Task 4 changes the valid/exhausted destination to `recovery_planner` and makes a missing decision
produce a `no_action` recovery plan.

On validation model failure or malformed output, use `invalid`, never `valid`.

- [ ] **Step 6: Run targeted tests and commit validation**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-validator-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_diagnostics.py
git commit -m "feat: validate grounded aiops root causes"
```

Expected: all targeted checks pass.

---

### Task 4: Add recovery planning and a fail-closed proposal policy gate

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`
- Test: `apps/backend/tests/test_live_nginx_timeout_contracts.py`

**Interfaces:**
- Consumes: Task 1 `RecoveryPlan` and `RecoveryPolicyDecision`, validated root cause from Task 3, existing MCP audit methods.
- Produces: `recovery_planning` and `policy_gate` checkpoints, request-scoped proposal whitelist, audited Nginx proposal, no write execution.

- [ ] **Step 1: Write failing policy tests**

Add service tests for these exact outcomes:

```python
assert denied.payload["status"] == "denied"
assert denied.payload["authorizationCode"] == "proposal_tool_not_allowed"
assert denied.payload["executionPermitted"] is False
assert denied.payload["proposalRecorded"] is False

assert allowed.payload["status"] == "allowed"
assert allowed.payload["authorizationCode"] == "proposal_recorded"
assert allowed.payload["executionPermitted"] is False
assert allowed.payload["proposalRecorded"] is True
```

The denied case supplies no tool policy mapping. The allowed case supplies:

```python
tool_policies={"ProposeNginxTimeoutMitigation": "proposal_only"}
```

Assert the allowed case has one completed audit for the proposal tool and that no audit tool name
contains `write`, `reload`, `restart`, `switch`, `signal`, or `apply`.

- [ ] **Step 2: Write a failing Live adapter test**

Run `ApplicationLiveDiagnosticAdapter` with the Nginx scenario and assert the component evidence
client does not receive `ProposeNginxTimeoutMitigation` during diagnostic evidence planning. It may
receive it only after `decision_validation=valid` and `policy_gate=allowed`.

For PostgreSQL and Redis scenarios, assert no proposal policy is configured and their existing
recovery service remains the only code that mutates the synthetic fixture.

- [ ] **Step 3: Run policy tests and confirm red state**

```powershell
Set-Location apps/backend
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_diagnostics.py tests/test_live_diagnostic_adapter.py tests/test_live_nginx_timeout_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-policy-red
```

Expected: constructor rejects `tool_policies`, and no recovery planning or policy checkpoint exists.

- [ ] **Step 4: Add request-scoped tool policy and exclude proposal tools from evidence plans**

Add this constructor parameter and validate the values immediately:

```python
tool_policies: Mapping[str, Literal["proposal_only"]] | None = None
```

Copy it into an immutable project-owned mapping. During initial planning and replanning, pass only
unclassified tools to diagnostic `parse_plan`. During recovery planning, pass only discovered tools
classified as `proposal_only`.

In the Live adapter, configure the mapping only when `scenario.driver == "nginx_timeout"`:

```python
tool_policies=(
    {"ProposeNginxTimeoutMitigation": "proposal_only"}
    if scenario.driver == "nginx_timeout"
    else None
),
```

Add the final recovery nodes, remove Task 3's `decision_validator -> report` destination, and use
this routing:

```python
graph.add_node("recovery_planner", self._recovery_planner)
graph.add_node("policy_gate", self._policy_gate)
graph.add_conditional_edges(
    "decision_validator",
    self._route_after_decision_validation,
    {"replanner": "replanner", "recovery_planner": "recovery_planner"},
)
graph.add_edge("recovery_planner", "policy_gate")
graph.add_edge("policy_gate", "report")
```

- [ ] **Step 5: Implement recovery planning fallbacks**

The model prompt contains `structured recovery plan` and only the validated decision, public
evidence IDs/summaries, discovered proposal contracts, and safety rules. Parse with
`parse_recovery_plan`.

Use deterministic fallbacks:

```python
if root_cause_decision is None:
    mode = "no_action"
elif proposal_tools:
    mode = "manual_review"
else:
    mode = "external_policy_required"
```

Model or parse failure must never synthesize proposal arguments and must return `manual_review`
when a valid diagnosis exists.

- [ ] **Step 6: Implement deterministic policy and proposal materialization**

Policy rules:

```python
if plan.mode == "no_action":
    status, code = "deferred", "no_grounded_action"
elif plan.mode == "external_policy_required":
    status, code = "deferred", "external_policy_required"
elif plan.mode == "manual_review":
    status, code = "deferred", "manual_review_required"
elif plan.tool not in self._tool_policies:
    status, code = "denied", "proposal_tool_not_allowed"
elif not plan.human_approval_required:
    status, code = "denied", "human_approval_required"
else:
    status, code = "allowed", "proposal_recorded"
```

Only the final branch may call MCP. Revalidate the selected definition's JSON Schema before the
call, create the normal audit, call the existing proposal-only MCP tool, finalize the audit, and
set `proposal_recorded=True`. Any schema, MCP, or audit failure returns `denied` with a bounded
code and no retry through another tool. Always set `execution_permitted=False`.

- [ ] **Step 7: Persist and report policy results**

Persist `recovery_planning` and `policy_gate` steps/checkpoints. Add
`workflowVersion="evidence-driven-v2"`, `recoveryPlan`, and `recoveryPolicy` to the report payload.
The report prompt must explicitly say that `allowed` means the proposal was recorded, not that an
infrastructure mutation was approved or executed.

- [ ] **Step 8: Run targeted safety checks and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_diagnostics.py tests/test_live_diagnostic_adapter.py tests/test_live_nginx_timeout_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-policy-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/live/diagnostics.py tests/test_aiops_diagnostics.py tests/test_live_diagnostic_adapter.py tests/test_live_nginx_timeout_contracts.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/live/diagnostics.py tests/test_aiops_diagnostics.py tests/test_live_diagnostic_adapter.py tests/test_live_nginx_timeout_contracts.py
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/tests/test_aiops_diagnostics.py apps/backend/tests/test_live_diagnostic_adapter.py apps/backend/tests/test_live_nginx_timeout_contracts.py
git commit -m "feat: gate aiops recovery proposals"
```

Expected: all commands pass; no infrastructure write is performed.

---

### Task 5: Preserve artifacts, scoring, reports, and full regressions

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: persisted phases and payloads from Tasks 2–4.
- Produces: scoreable artifacts that exclude invalidated decisions, truthful executed-step count, updated workflow documentation, and complete offline verification evidence.

- [ ] **Step 1: Write failing artifact compatibility tests**

Add an artifact test with a candidate `decision` followed by an invalid `decision_validation` and
assert:

```python
artifact = build_run_artifact(task, steps, evidence, tool_calls, reports)
assert artifact.decision is None
```

Add a valid case and assert the decision remains. Add a replanned workflow with six executor steps
and assert:

```python
assert artifact.plan_step_count == 6
```

- [ ] **Step 2: Run artifact tests and confirm red state**

```powershell
Set-Location apps/backend
& .\.venv\Scripts\python.exe -m pytest tests/test_evaluation_artifacts.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-artifacts-red
```

Expected: the current parser accepts the invalidated candidate and counts only the initial plan.

- [ ] **Step 3: Make artifact parsing validation-aware**

Change `_decision_from_steps` to read the planner's `workflowVersion`. For
`evidence-driven-v2`, track the most recent `decision_validation` following the most recent
candidate and return the candidate only when that validation payload has `status == "valid"`.
A missing validation in a partial/interrupted v2 run returns `None`. Only records with no v2 marker
use the legacy behavior, so existing stored runs remain readable without making modern partial
runs fail open.

Change `_plan_step_count` to count persisted `executor` steps rather than initial proposed steps:

```python
def _plan_step_count(steps: Sequence[DiagnosticStepRecord]) -> int:
    return sum(1 for step in steps if step.phase == "executor")
```

The workflow cap guarantees this remains from zero through six and preserves the existing scoring
threshold.

Add a partial v2 artifact test with a planner marker and candidate decision but no validation;
assert `artifact.decision is None`. Add a legacy artifact test without the marker or validation and
assert its historical decision remains readable.

- [ ] **Step 4: Update workflow documentation without exposing answer data**

Update `docs/aiops/agentpy-domainbench.md` with the new public graph, six-step/two-replan budgets,
decision-validation semantics, external scorer boundary, and proposal-only policy meaning. Do not
include scenario answers, private labels, raw logs, credentials, owner IDs, topic IDs, or run IDs.

- [ ] **Step 5: Run focused benchmark regressions**

Before the benchmark regression, extend the existing authenticated AIOps ownership test in
`test_aiops_diagnostics.py`. Keep the created `app` in a variable, and after the owner's stream
returns, use `app.state.memory_repositories` to persist completed owner-scoped steps and
checkpoints for `sufficiency_gate`, `decision_validation`, `recovery_planning`, and `policy_gate`.
Then assert the owner's evidence-chain response includes all four phases. A second owner's
evidence-chain request and SSE subscription must both return `403 AUTH_FORBIDDEN`, and the denied
SSE request must not invoke the diagnostic runner. Direct repository calls using the second user
ID must return empty step, checkpoint, evidence, and report lists for the owner's task.

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_evaluation_artifacts.py tests/test_evaluation_scoring.py tests/test_snapshot_benchmark_runner.py tests/test_aiops_diagnostics.py tests/test_live_diagnostic_adapter.py tests/test_live_evaluation_scoring.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-benchmark-regression
```

Expected: all selected tests pass and scoring weights/thresholds are unchanged.

- [ ] **Step 6: Run answer-isolation and failure-path regressions**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_answer_isolation.py tests/test_evaluation_safety.py tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py -q -p no:cacheprovider --basetemp=var/pytest-aiops-safety-regression
```

If one of the named isolation files does not exist, use `rg --files tests | rg "isolation|safety"`
and run every returned file together with the four explicitly named AIOps/Live files. This is a
file-discovery adjustment, not permission to skip isolation tests.

Expected: all selected tests pass; ordinary tests make no external calls.

- [ ] **Step 7: Run full offline verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=var/pytest-aiops-full
& .\.venv\Scripts\python.exe -m ruff check src tests
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
```

Expected: full ordinary suite passes with only documented marker skips; Ruff passes; Pyright
reports zero errors and zero warnings. Do not run `live_llm`, `live_cls`, or `live_docker` markers.

- [ ] **Step 8: Scan for answer and secret leakage**

```powershell
rg -n -i "ground_truth|primary_cause|secretid|secretkey|api[_-]?key|postgresql://|topic[-_ ]?id|run[-_ ]?id" apps/backend/src/super_ai/aiops apps/backend/src/super_ai/evaluation docs/aiops/agentpy-domainbench.md
```

Expected: only intentional schema/config references appear; no credential value, private scenario
answer, runtime identity, or raw model response is present in changed prompts or documentation.

- [ ] **Step 9: Commit compatibility and documentation**

```powershell
git add apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_diagnostics.py apps/backend/tests/test_live_diagnostic_adapter.py docs/aiops/agentpy-domainbench.md
git commit -m "docs: finalize evidence driven aiops workflow"
git status --short --branch
```

Expected: clean worktree on `feat/benchmark-rag-live-expansion`, ahead of the remote only by the
new reviewed commits. Do not push until the user asks.

## Final Acceptance

- The graph contains and persists `sufficiency_gate`, real `replanner`, `decision_validation`,
  `recovery_planning`, and `policy_gate` phases.
- The Agent can request missing evidence but cannot exceed two replans or six persisted executor
  attempts, including rejected duplicates and failed tools.
- Unsupported trigger or causal-chain decisions cannot reach the report as grounded conclusions.
- Proposal-only calls require a request-scoped whitelist and never grant infrastructure execution.
- PostgreSQL and Redis recovery policies remain outside the Agent graph.
- Snapshot and Live adapters continue to use `AiopsDiagnosticService`; the conversation Agent is
  unchanged.
- Artifact extraction and scoring remain backward compatible and keep `bounded_plan <= 6`.
- Full offline pytest, Ruff, and Pyright pass before any real-service acceptance is considered.
