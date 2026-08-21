"""Immutable, answer-isolated contracts for bounded AIOps specialists."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic import (
    JsonValue as PydanticJsonValue,
)

from super_ai.aiops.causal_intents import CoreCausalRole
from super_ai.aiops.investigation import (
    TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    EvidenceClaim,
    JsonValue,
)

SpecialistRole = Literal["runtime", "log"]
SpecialistTerminalStatus = Literal[
    "completed", "inconclusive", "failed", "timeout", "cancelled"
]
SpecialistDeadlineState = Literal["active", "soft_expired", "hard_expired"]
PublicSignalDisposition = Literal[
    "supported", "refuted", "causally_inactive", "unresolved"
]

_ROLES = frozenset({"runtime", "log"})
_TERMINAL_STATUSES = frozenset(
    {"completed", "inconclusive", "failed", "timeout", "cancelled"}
)
_DEADLINE_STATES = frozenset({"active", "soft_expired", "hard_expired"})
_CAUSAL_ROLES = frozenset({"trigger", "mechanism", "impact"})
_LOG_SCOPE_KEYS = frozenset({"Region", "TopicId", "From", "To", "Query", "Limit"})
_PRIVATE_TOKENS = (
    "credential",
    "groundtruth",
    "oracle",
    "primarycause",
    "privatereasoning",
    "prompt",
    "rawresponse",
    "readgroundtruth",
    "recoveryaction",
    "scorerules",
    "secret",
)
_RECOVERY_TOOL_TOKENS = (
    "applyrecovery",
    "executecommand",
    "killprocess",
    "restartservice",
    "terminateconnection",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SharedRunContext:
    owner_user_id: str
    task_id: str
    graph_version: str
    public_incident_input: Mapping[str, JsonValue]
    public_hypotheses: tuple[str, ...]
    decision_vocabulary: Mapping[str, JsonValue]
    allowed_tools_by_specialist: Mapping[SpecialistRole, frozenset[str]]
    trusted_arguments_by_specialist: Mapping[
        SpecialistRole, Mapping[str, Mapping[str, JsonValue]]
    ]
    global_soft_deadline_at: datetime
    global_hard_deadline_at: datetime
    global_model_budget: int

    def __post_init__(self) -> None:
        _require_public_text(
            self.owner_user_id,
            self.task_id,
            self.graph_version,
            *self.public_hypotheses,
        )
        _validate_deadlines(
            self.global_soft_deadline_at,
            self.global_hard_deadline_at,
        )
        if self.global_model_budget <= 0:
            raise ValueError("Global model budget must be positive.")
        incident = _freeze_public_mapping(self.public_incident_input)
        vocabulary = _freeze_public_mapping(self.decision_vocabulary)
        tools = _freeze_allowed_tools(self.allowed_tools_by_specialist)
        bindings = _freeze_bindings(
            self.trusted_arguments_by_specialist,
            allowed_tools_by_role=tools,
        )
        object.__setattr__(self, "public_incident_input", incident)
        object.__setattr__(self, "public_hypotheses", _unique_text(self.public_hypotheses))
        object.__setattr__(self, "decision_vocabulary", vocabulary)
        object.__setattr__(self, "allowed_tools_by_specialist", tools)
        object.__setattr__(self, "trusted_arguments_by_specialist", bindings)


@dataclass(frozen=True, slots=True)
class SpecialistAssignment:
    role: SpecialistRole
    objective: str
    hypotheses_to_test: tuple[str, ...]
    required_causal_roles: tuple[CoreCausalRole, ...]
    allowed_tools: frozenset[str]
    trusted_arguments_by_tool: Mapping[str, Mapping[str, JsonValue]]
    maximum_tool_steps: int
    model_call_budget: int
    soft_deadline_at: datetime
    hard_deadline_at: datetime

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError("Specialist role is invalid.")
        _require_public_text(
            self.objective,
            *self.hypotheses_to_test,
            *self.allowed_tools,
        )
        if not self.hypotheses_to_test:
            raise ValueError("Specialist assignment requires hypotheses.")
        if (
            not self.required_causal_roles
            or any(item not in _CAUSAL_ROLES for item in self.required_causal_roles)
        ):
            raise ValueError("Specialist assignment causal roles are invalid.")
        if not 1 <= self.maximum_tool_steps <= 3:
            raise ValueError("Specialist assignment allows at most three tool steps.")
        if not 1 <= self.model_call_budget <= 2:
            raise ValueError("Specialist assignment model budget cannot exceed two calls.")
        _validate_deadlines(self.soft_deadline_at, self.hard_deadline_at)
        tools = _validate_tools_for_role(self.role, self.allowed_tools)
        bindings = _freeze_role_bindings(
            self.role,
            self.trusted_arguments_by_tool,
            allowed_tools=tools,
        )
        object.__setattr__(self, "hypotheses_to_test", _unique_text(self.hypotheses_to_test))
        object.__setattr__(
            self,
            "required_causal_roles",
            tuple(dict.fromkeys(self.required_causal_roles)),
        )
        object.__setattr__(self, "allowed_tools", tools)
        object.__setattr__(self, "trusted_arguments_by_tool", bindings)


@dataclass(frozen=True, slots=True)
class SpecialistPlanStep:
    step_id: str
    tool_name: str
    tested_hypotheses: tuple[str, ...]
    causal_intent: CoreCausalRole
    proposed_arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _require_public_text(self.step_id, self.tool_name, *self.tested_hypotheses)
        if not self.tested_hypotheses:
            raise ValueError("Specialist plan step requires tested hypotheses.")
        if self.causal_intent not in _CAUSAL_ROLES:
            raise ValueError("Specialist plan step causal intent is invalid.")
        object.__setattr__(
            self,
            "tested_hypotheses",
            _unique_text(self.tested_hypotheses),
        )
        object.__setattr__(
            self,
            "proposed_arguments",
            _freeze_public_mapping(self.proposed_arguments),
        )


class SpecialistPlanStepOutput(BaseModel):
    """Strict structured output for one model-proposed local plan step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    tool_name: str
    tested_hypotheses: tuple[str, ...]
    causal_intent: CoreCausalRole
    proposed_arguments: dict[str, PydanticJsonValue]

    @model_validator(mode="after")
    def validate_public_contract(self) -> SpecialistPlanStepOutput:
        self.to_contract()
        return self

    def to_contract(self) -> SpecialistPlanStep:
        return SpecialistPlanStep(
            step_id=self.step_id,
            tool_name=self.tool_name,
            tested_hypotheses=self.tested_hypotheses,
            causal_intent=self.causal_intent,
            proposed_arguments=cast(Mapping[str, JsonValue], self.proposed_arguments),
        )


class SpecialistLocalPlanOutput(BaseModel):
    """Bounded structured output for one Specialist Local Planner call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[SpecialistPlanStepOutput, ...] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_stable_step_ids(self) -> SpecialistLocalPlanOutput:
        step_ids = tuple(step.step_id for step in self.steps)
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("Specialist local plan step IDs must be unique.")
        return self


@dataclass(frozen=True, slots=True)
class PublicAssessmentSignal:
    hypothesis_id: str
    disposition: PublicSignalDisposition
    evidence_ids: tuple[str, ...]
    summary: str

    def __post_init__(self) -> None:
        _require_public_text(
            self.hypothesis_id,
            self.summary,
            *self.evidence_ids,
        )
        if self.disposition not in {
            "supported",
            "refuted",
            "causally_inactive",
            "unresolved",
        }:
            raise ValueError("Public assessment signal disposition is invalid.")
        if not self.evidence_ids:
            raise ValueError("Public assessment signal requires Evidence IDs.")
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))


class SpecialistEvidenceClaimOutput(BaseModel):
    """Strict structured representation of one public Evidence candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    value: PydanticJsonValue
    quality: Literal["direct", "context", "reference"]
    causal_role: CoreCausalRole | None
    supports: tuple[str, ...]
    refutes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    target_component: str
    observed_at: datetime | None
    time_scope: Literal["incident_window", "current", "historical"]

    @model_validator(mode="after")
    def validate_public_contract(self) -> SpecialistEvidenceClaimOutput:
        self.to_contract()
        return self

    def to_contract(self) -> EvidenceClaim:
        return EvidenceClaim(
            claim_id=self.claim_id,
            value=cast(JsonValue, self.value),
            quality=self.quality,
            causal_role=self.causal_role,
            supports=self.supports,
            refutes=self.refutes,
            evidence_ids=self.evidence_ids,
            target_component=self.target_component,
            observed_at=self.observed_at,
            time_scope=self.time_scope,
        )


class SpecialistAssessmentSignalOutput(BaseModel):
    """Strict structured representation of one untrusted assessment signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    disposition: PublicSignalDisposition
    evidence_ids: tuple[str, ...]
    summary: str

    @model_validator(mode="after")
    def validate_public_contract(self) -> SpecialistAssessmentSignalOutput:
        self.to_contract()
        return self

    def to_contract(self) -> PublicAssessmentSignal:
        return PublicAssessmentSignal(
            hypothesis_id=self.hypothesis_id,
            disposition=self.disposition,
            evidence_ids=self.evidence_ids,
            summary=self.summary,
        )


class SpecialistEvidenceAnalysisOutput(BaseModel):
    """Strict public output for one Specialist Evidence Analysis call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tested_hypotheses: tuple[str, ...]
    fact_candidates: tuple[SpecialistEvidenceClaimOutput, ...]
    proposed_assessments: tuple[SpecialistAssessmentSignalOutput, ...]
    unresolved_questions: tuple[str, ...]

    @model_validator(mode="after")
    def validate_public_content(self) -> SpecialistEvidenceAnalysisOutput:
        _require_public_text(
            *self.tested_hypotheses,
            *self.unresolved_questions,
        )
        if not self.tested_hypotheses:
            raise ValueError("Specialist Evidence Analysis requires tested hypotheses.")
        return self


@dataclass(frozen=True, slots=True)
class SpecialistState:
    assignment: SpecialistAssignment
    local_plan: tuple[SpecialistPlanStep, ...]
    current_step: int
    local_observations: tuple[EvidenceClaim, ...]
    local_hypothesis_signals: tuple[PublicAssessmentSignal, ...]
    unresolved_questions: tuple[str, ...]
    model_call_count: int
    deadline_state: SpecialistDeadlineState
    terminal_status: SpecialistTerminalStatus | None

    def __post_init__(self) -> None:
        if len(self.local_plan) > self.assignment.maximum_tool_steps:
            raise ValueError("Specialist local plan exceeds assignment step limit.")
        if any(step.tool_name not in self.assignment.allowed_tools for step in self.local_plan):
            raise ValueError("Specialist plan step is outside its assignment.")
        if not 0 <= self.current_step <= len(self.local_plan):
            raise ValueError("Specialist current step is out of range.")
        if not 0 <= self.model_call_count <= self.assignment.model_call_budget:
            raise ValueError("Specialist state exceeds its model budget.")
        if self.deadline_state not in _DEADLINE_STATES:
            raise ValueError("Specialist deadline state is invalid.")
        if self.terminal_status is not None and self.terminal_status not in _TERMINAL_STATUSES:
            raise ValueError("Specialist terminal status is invalid.")
        _require_public_text(*self.unresolved_questions)
        object.__setattr__(
            self,
            "unresolved_questions",
            _unique_text(self.unresolved_questions),
        )


@dataclass(frozen=True, slots=True)
class SpecialistResult:
    role: SpecialistRole
    terminal_status: SpecialistTerminalStatus
    tested_hypotheses: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    fact_candidates: tuple[EvidenceClaim, ...]
    proposed_assessments: tuple[PublicAssessmentSignal, ...]
    unresolved_questions: tuple[str, ...]
    completed_steps: tuple[str, ...]
    model_call_count: int
    duration_ms: int
    result_checksum: str = ""

    @classmethod
    def create(
        cls,
        *,
        role: SpecialistRole,
        terminal_status: SpecialistTerminalStatus,
        tested_hypotheses: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        fact_candidates: tuple[EvidenceClaim, ...],
        proposed_assessments: tuple[PublicAssessmentSignal, ...],
        unresolved_questions: tuple[str, ...],
        completed_steps: tuple[str, ...],
        model_call_count: int,
        duration_ms: int,
    ) -> SpecialistResult:
        return cls(
            role=role,
            terminal_status=terminal_status,
            tested_hypotheses=tested_hypotheses,
            evidence_ids=evidence_ids,
            fact_candidates=fact_candidates,
            proposed_assessments=proposed_assessments,
            unresolved_questions=unresolved_questions,
            completed_steps=completed_steps,
            model_call_count=model_call_count,
            duration_ms=duration_ms,
        )

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError("Specialist result role is invalid.")
        if self.terminal_status not in _TERMINAL_STATUSES:
            raise ValueError("Specialist result terminal status is invalid.")
        _require_public_text(
            *self.tested_hypotheses,
            *self.evidence_ids,
            *self.unresolved_questions,
            *self.completed_steps,
        )
        if not 0 <= self.model_call_count <= 2:
            raise ValueError("Specialist result exceeds its model-call budget.")
        if self.duration_ms < 0:
            raise ValueError("Specialist result duration cannot be negative.")
        object.__setattr__(
            self,
            "tested_hypotheses",
            tuple(sorted(set(self.tested_hypotheses))),
        )
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        object.__setattr__(
            self,
            "unresolved_questions",
            _unique_text(self.unresolved_questions),
        )
        if len(set(self.completed_steps)) != len(self.completed_steps):
            raise ValueError("Specialist result completed steps must be unique.")
        expected = _calculate_result_checksum(self)
        if self.result_checksum and self.result_checksum != expected:
            raise ValueError("Specialist result checksum does not match its content.")
        if self.result_checksum and _SHA256_PATTERN.fullmatch(self.result_checksum) is None:
            raise ValueError("Specialist result checksum is invalid.")
        object.__setattr__(self, "result_checksum", expected)


def specialist_execution_key(
    *,
    task_id: str,
    graph_version: str,
    role: SpecialistRole,
    role_name: str,
    logical_step: str,
    arguments: Mapping[str, JsonValue],
) -> str:
    """Return one stable execution identity for model or tool work."""
    if role not in _ROLES:
        raise ValueError("Specialist execution role is invalid.")
    _require_public_text(task_id, graph_version, role_name, logical_step)
    payload = {
        "arguments": _plain_json(_freeze_public_mapping(arguments)),
        "graphVersion": graph_version,
        "logicalStep": logical_step,
        "role": role,
        "roleName": role_name,
        "taskId": task_id,
    }
    return _sha256_json(payload)


def specialist_result_checksum(result: SpecialistResult) -> str:
    """Recompute a Specialist result checksum without trusting its stored value."""
    return _calculate_result_checksum(result)


def _calculate_result_checksum(result: SpecialistResult) -> str:
    payload = {
        "completedSteps": list(result.completed_steps),
        "durationMs": result.duration_ms,
        "evidenceIds": list(result.evidence_ids),
        "factCandidates": [_claim_payload(item) for item in result.fact_candidates],
        "modelCallCount": result.model_call_count,
        "proposedAssessments": [
            _signal_payload(item) for item in result.proposed_assessments
        ],
        "role": result.role,
        "terminalStatus": result.terminal_status,
        "testedHypotheses": list(result.tested_hypotheses),
        "unresolvedQuestions": list(result.unresolved_questions),
    }
    return _sha256_json(payload)


def _claim_payload(claim: EvidenceClaim) -> dict[str, object]:
    return {
        "causalRole": claim.causal_role,
        "claimId": claim.claim_id,
        "evidenceIds": list(claim.evidence_ids),
        "observedAt": claim.observed_at.isoformat() if claim.observed_at else None,
        "quality": claim.quality,
        "refutes": list(claim.refutes),
        "supports": list(claim.supports),
        "targetComponent": claim.target_component,
        "timeScope": claim.time_scope,
        "value": _plain_json(claim.value),
    }


def _signal_payload(signal: PublicAssessmentSignal) -> dict[str, object]:
    return {
        "disposition": signal.disposition,
        "evidenceIds": list(signal.evidence_ids),
        "hypothesisId": signal.hypothesis_id,
        "summary": signal.summary,
    }


def _freeze_allowed_tools(
    value: Mapping[SpecialistRole, frozenset[str]],
) -> Mapping[SpecialistRole, frozenset[str]]:
    if frozenset(value) != _ROLES:
        raise ValueError("Shared context requires runtime and log specialist tools.")
    frozen: dict[SpecialistRole, frozenset[str]] = {
        role: _validate_tools_for_role(role, value[role])
        for role in cast(Sequence[SpecialistRole], tuple(sorted(value)))
    }
    return MappingProxyType(frozen)


def _validate_tools_for_role(
    role: SpecialistRole,
    tools: frozenset[str],
) -> frozenset[str]:
    if not tools:
        raise ValueError("Specialist assignment requires source-scoped tools.")
    for tool in tools:
        normalized = _normalized(tool)
        if any(token in normalized for token in _RECOVERY_TOOL_TOKENS):
            raise ValueError("Specialist assignment cannot expose recovery tools.")
        capability = TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES.get(tool)
        if capability is None or capability.source_domain != role:
            raise ValueError("Specialist tool source does not match its role.")
    return frozenset(tools)


def _freeze_bindings(
    value: Mapping[SpecialistRole, Mapping[str, Mapping[str, JsonValue]]],
    *,
    allowed_tools_by_role: Mapping[SpecialistRole, frozenset[str]],
) -> Mapping[SpecialistRole, Mapping[str, Mapping[str, JsonValue]]]:
    if frozenset(value) != _ROLES:
        raise ValueError("Shared context requires bindings for both specialist roles.")
    return MappingProxyType(
        {
            role: _freeze_role_bindings(
                role,
                value[role],
                allowed_tools=allowed_tools_by_role[role],
            )
            for role in cast(Sequence[SpecialistRole], tuple(sorted(value)))
        }
    )


def _freeze_role_bindings(
    role: SpecialistRole,
    value: Mapping[str, Mapping[str, JsonValue]],
    *,
    allowed_tools: frozenset[str],
) -> Mapping[str, Mapping[str, JsonValue]]:
    if frozenset(value) != allowed_tools:
        raise ValueError("Specialist trusted binding must match every allowed tool exactly.")
    frozen: dict[str, Mapping[str, JsonValue]] = {}
    for tool in sorted(value):
        binding = _freeze_public_mapping(value[tool])
        if role == "log":
            if frozenset(binding) != _LOG_SCOPE_KEYS:
                raise ValueError("Log specialist binding must contain the exact CLS scope.")
            if not all(
                isinstance(binding[key], str) and cast(str, binding[key]).strip()
                for key in ("Region", "TopicId", "Query")
            ):
                raise ValueError("Log specialist binding strings must be non-empty.")
            if not all(
                isinstance(binding[key], int) and not isinstance(binding[key], bool)
                for key in ("From", "To", "Limit")
            ):
                raise ValueError("Log specialist binding numeric fields are invalid.")
        frozen[tool] = binding
    return MappingProxyType(frozen)


def _freeze_public_mapping(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return cast(Mapping[str, JsonValue], _freeze_public_json(value))


def _freeze_public_json(value: JsonValue) -> JsonValue:
    raw = cast(object, value)
    if raw is None or isinstance(raw, (bool, int)):
        return cast(JsonValue, raw)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise ValueError("Specialist public JSON numbers must be finite.")
        return raw
    if isinstance(raw, str):
        _require_public_text(raw)
        return raw
    if isinstance(raw, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in cast(Mapping[object, object], raw).items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Specialist public JSON keys must be non-empty strings.")
            _reject_private_text(key)
            frozen[key] = _freeze_public_json(cast(JsonValue, item))
        return cast(JsonValue, MappingProxyType(frozen))
    if isinstance(raw, (list, tuple)):
        items = cast(Sequence[object], raw)
        return tuple(_freeze_public_json(cast(JsonValue, item)) for item in items)
    raise ValueError("Specialist public context must contain JSON-compatible values.")


def _require_public_text(*values: str) -> None:
    for value in values:
        if not value.strip():
            raise ValueError("Specialist public text cannot be empty.")
        _reject_private_text(value)


def _reject_private_text(value: str) -> None:
    normalized = _normalized(value)
    if any(token in normalized for token in _PRIVATE_TOKENS):
        raise ValueError("Specialist contract contains private data.")


def _unique_text(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _validate_deadlines(soft: datetime, hard: datetime) -> None:
    if soft.tzinfo is None or hard.tzinfo is None or soft >= hard:
        raise ValueError("Specialist deadlines must be aware and ordered.")


def _normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {
            key: _plain_json(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return cast(object, value)


__all__ = [
    "PublicAssessmentSignal",
    "SharedRunContext",
    "SpecialistAssessmentSignalOutput",
    "SpecialistAssignment",
    "SpecialistDeadlineState",
    "SpecialistEvidenceAnalysisOutput",
    "SpecialistEvidenceClaimOutput",
    "SpecialistLocalPlanOutput",
    "SpecialistPlanStep",
    "SpecialistPlanStepOutput",
    "SpecialistResult",
    "SpecialistRole",
    "SpecialistState",
    "SpecialistTerminalStatus",
    "specialist_execution_key",
    "specialist_result_checksum",
]
