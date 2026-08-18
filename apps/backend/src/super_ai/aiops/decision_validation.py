"""Resilient root-cause validation using only public, persisted evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from super_ai.aiops.reasoning import (
    RootCauseDecision,
    RootCauseValidationDecision,
    parse_root_cause_decision,
    parse_root_cause_validation,
)
from super_ai.llm import ChatModel
from super_ai.llm.config import StructuredOutputMethod

DecisionValidationErrorCategory = Literal[
    "candidate_missing",
    "deterministic_gap",
    "model_call_failed",
    "invalid_model_output",
    "model_rejected",
    "retry_exhausted",
]
DeterministicCheckCode = Literal[
    "unique_supported_hypothesis",
    "no_open_competitor",
    "public_label_match",
    "task_evidence_only",
    "supporting_evidence_only",
    "independent_positive_evidence",
    "supporting_observations",
    "grounded_causal_chain",
    "trigger_present",
    "confidence_in_range",
]
RootCauseField = Literal["component", "mechanism", "trigger", "causalChain"]
ValidationErrorCode = Literal[
    "timeout",
    "connection",
    "authentication",
    "permission_denied",
    "rate_limit",
    "provider_4xx",
    "provider_5xx",
    "structured_output_unsupported",
    "unknown",
]
ValidationErrorPhase = Literal[
    "structured_invoker_setup",
    "model_invoke",
    "structured_parse",
]
ValidationHttpStatusClass = Literal["4xx", "5xx"]
StructuredParseErrorCode = Literal[
    "invalid_json",
    "structured_envelope_mismatch",
    "missing_required_field",
    "invalid_enum",
    "wrong_container_type",
    "extra_field",
    "unknown_evidence_id",
    "invalid_json_or_schema",
]


class _StructuredParseFailure(ValueError):
    """Carry only allowlisted parse codes, never model data or error text."""

    def __init__(self, *codes: StructuredParseErrorCode) -> None:
        super().__init__()
        self.codes = codes or ("invalid_json_or_schema",)


@dataclass(frozen=True, slots=True)
class DeterministicCheck:
    code: DeterministicCheckCode
    passed: bool


@dataclass(frozen=True, slots=True)
class DeterministicValidationResult:
    passed: bool
    supported_hypothesis_id: str | None
    checks: tuple[DeterministicCheck, ...]
    unsupported_fields: tuple[RootCauseField, ...]
    missing_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SafeModelFailure:
    code: ValidationErrorCode
    phase: ValidationErrorPhase
    retryable: bool
    http_status_class: ValidationHttpStatusClass | None = None


@dataclass(frozen=True, slots=True)
class StructuredValidationOutcome:
    decision: RootCauseValidationDecision | None
    error_category: DecisionValidationErrorCategory | None
    attempts: int
    error_codes: tuple[str, ...]
    error_code: ValidationErrorCode | None = None
    error_phase: ValidationErrorPhase | None = None
    retryable: bool | None = None
    http_status_class: ValidationHttpStatusClass | None = None


@dataclass(frozen=True, slots=True)
class StructuredDecisionOutcome:
    decision: RootCauseDecision | None
    error_category: DecisionValidationErrorCategory | None
    attempts: int
    error_codes: tuple[str, ...]
    error_code: ValidationErrorCode | None = None
    error_phase: ValidationErrorPhase | None = None
    retryable: bool | None = None
    http_status_class: ValidationHttpStatusClass | None = None


class _RootCauseDecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    causal_chain: list[str] = Field(alias="causalChain", min_length=2, max_length=6)
    evidence_ids: list[str] = Field(alias="evidenceIds", min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class _RootCauseValidationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: Literal["valid", "invalid"]
    evidence_ids: list[str] = Field(alias="evidenceIds")
    unsupported_fields: list[RootCauseField] = Field(alias="unsupportedFields")
    missing_evidence: list[str] = Field(alias="missingEvidence")
    summary: str = Field(min_length=1)


class _AsyncInvoker(Protocol):
    async def ainvoke(self, input: object) -> object:
        """Invoke a structured or raw chat model."""
        ...


_CORRECTION_SUFFIX = (
    "\n\nThe previous response did not match the required validation schema. "
    "Return JSON only with status, evidenceIds, unsupportedFields, missingEvidence, "
    "and summary. Schema errors: invalid_json_or_schema."
)
_DECISION_CORRECTION_SUFFIX = (
    "\n\nThe previous response did not match the required root-cause decision schema. "
    "Return JSON only with component, mechanism, trigger, causalChain, evidenceIds, "
    "and confidence. Schema errors: invalid_json_or_schema."
)
_REPLANABLE_DETERMINISTIC_GAPS: frozenset[DeterministicCheckCode] = frozenset(
    {
        "no_open_competitor",
        "independent_positive_evidence",
        "supporting_observations",
        "grounded_causal_chain",
        "trigger_present",
    }
)


def can_replan_deterministic_gap(
    result: DeterministicValidationResult,
    *,
    recommended_tools: Sequence[str],
    available_tools: Set[str],
    executed_tools: Set[str],
) -> bool:
    """Allow only concrete, unexecuted tools for evidence-remediable gaps."""
    failed_codes = {check.code for check in result.checks if not check.passed}
    if not failed_codes or not failed_codes.issubset(_REPLANABLE_DETERMINISTIC_GAPS):
        return False
    return any(
        tool in available_tools and tool not in executed_tools
        for tool in recommended_tools
    )


async def invoke_structured_root_cause_validation(
    *,
    model: ChatModel,
    prompt: str,
    available_evidence_ids: Set[str],
    structured_output_method: StructuredOutputMethod = "function_calling",
) -> StructuredValidationOutcome:
    """Invoke one Validator with one format-only correction and safe audit output."""
    try:
        structured = _structured_invoker(
            model,
            _RootCauseValidationSchema,
            method=structured_output_method,
        )
    except Exception as exc:
        failure = classify_model_failure(exc, phase="structured_invoker_setup")
        return _model_failure_outcome(failure, attempts=0)
    invoker: _AsyncInvoker = structured or model
    current_prompt = prompt
    parse_codes: list[str] = []
    for attempt in (1, 2):
        try:
            response = await invoker.ainvoke(current_prompt)
        except Exception as exc:
            failure = classify_model_failure(exc, phase="model_invoke")
            return _model_failure_outcome(failure, attempts=attempt)
        try:
            decision = _validation_decision_from_response(
                response,
                structured=structured is not None,
                available_evidence_ids=available_evidence_ids,
            )
        except _StructuredParseFailure as exc:
            _append_parse_codes(parse_codes, exc.codes)
            if attempt == 1:
                current_prompt = prompt + _CORRECTION_SUFFIX
                continue
            return StructuredValidationOutcome(
                decision=None,
                error_category="retry_exhausted",
                attempts=attempt,
                error_codes=tuple(parse_codes),
                error_phase="structured_parse",
                retryable=False,
            )
        except (TypeError, ValueError):
            _append_parse_codes(parse_codes, ("invalid_json_or_schema",))
            if attempt == 1:
                current_prompt = prompt + _CORRECTION_SUFFIX
                continue
            return StructuredValidationOutcome(
                decision=None,
                error_category="retry_exhausted",
                attempts=attempt,
                error_codes=tuple(parse_codes),
                error_phase="structured_parse",
                retryable=False,
            )
        return StructuredValidationOutcome(
            decision=decision,
            error_category="model_rejected" if decision.status == "invalid" else None,
            attempts=attempt,
            error_codes=tuple(parse_codes),
        )
    raise AssertionError("The bounded validation loop must return within two attempts.")


async def invoke_structured_root_cause_decision(
    *,
    model: ChatModel,
    prompt: str,
    available_evidence_ids: Set[str],
    structured_output_method: StructuredOutputMethod = "function_calling",
) -> StructuredDecisionOutcome:
    """Generate one bounded structured decision with secret-safe failure metadata."""
    try:
        structured = _structured_invoker(
            model,
            _RootCauseDecisionSchema,
            method=structured_output_method,
        )
    except Exception as exc:
        failure = classify_model_failure(exc, phase="structured_invoker_setup")
        return _decision_model_failure_outcome(failure, attempts=0)
    invoker: _AsyncInvoker = structured or model
    current_prompt = prompt
    for attempt in (1, 2):
        try:
            response = await invoker.ainvoke(current_prompt)
        except Exception as exc:
            failure = classify_model_failure(exc, phase="model_invoke")
            return _decision_model_failure_outcome(failure, attempts=attempt)
        try:
            decision = _root_cause_decision_from_response(
                response,
                structured=structured is not None,
                available_evidence_ids=available_evidence_ids,
            )
        except (TypeError, ValueError):
            if attempt == 1:
                current_prompt = prompt + _DECISION_CORRECTION_SUFFIX
                continue
            return StructuredDecisionOutcome(
                decision=None,
                error_category="retry_exhausted",
                attempts=attempt,
                error_codes=("invalid_json_or_schema",),
                error_phase="structured_parse",
                retryable=False,
            )
        return StructuredDecisionOutcome(
            decision=decision,
            error_category=None,
            attempts=attempt,
            error_codes=(),
        )
    raise AssertionError("The bounded decision loop must return within two attempts.")


def classify_model_failure(
    exc: Exception,
    *,
    phase: ValidationErrorPhase,
) -> SafeModelFailure:
    """Map exception identity to an allowlisted record without reading its message."""
    if phase == "structured_invoker_setup" and isinstance(
        exc, (NotImplementedError, TypeError)
    ):
        return SafeModelFailure("structured_output_unsupported", phase, False)
    if isinstance(exc, (TimeoutError, openai.APITimeoutError)):
        return SafeModelFailure("timeout", phase, True)
    if isinstance(exc, openai.AuthenticationError):
        return SafeModelFailure("authentication", phase, False, "4xx")
    if isinstance(exc, openai.PermissionDeniedError):
        return SafeModelFailure("permission_denied", phase, False, "4xx")
    if isinstance(exc, openai.RateLimitError):
        return SafeModelFailure("rate_limit", phase, True, "4xx")
    if isinstance(exc, openai.APIConnectionError):
        return SafeModelFailure("connection", phase, True)
    if isinstance(exc, openai.APIStatusError):
        status_class: ValidationHttpStatusClass = (
            "5xx" if exc.status_code >= 500 else "4xx"
        )
        return SafeModelFailure(
            "provider_5xx" if status_class == "5xx" else "provider_4xx",
            phase,
            status_class == "5xx",
            status_class,
        )
    return SafeModelFailure("unknown", phase, False)


def _model_failure_outcome(
    failure: SafeModelFailure,
    *,
    attempts: int,
) -> StructuredValidationOutcome:
    return StructuredValidationOutcome(
        decision=None,
        error_category="model_call_failed",
        attempts=attempts,
        error_codes=(failure.code,),
        error_code=failure.code,
        error_phase=failure.phase,
        retryable=failure.retryable,
        http_status_class=failure.http_status_class,
    )


def _decision_model_failure_outcome(
    failure: SafeModelFailure,
    *,
    attempts: int,
) -> StructuredDecisionOutcome:
    return StructuredDecisionOutcome(
        decision=None,
        error_category="model_call_failed",
        attempts=attempts,
        error_codes=(failure.code,),
        error_code=failure.code,
        error_phase=failure.phase,
        retryable=failure.retryable,
        http_status_class=failure.http_status_class,
    )


def _structured_invoker(
    model: ChatModel,
    schema: type[BaseModel],
    *,
    method: StructuredOutputMethod,
) -> _AsyncInvoker | None:
    method_value = getattr(model, "with_structured_output", None)
    if not callable(method_value):
        return None
    return cast(
        _AsyncInvoker,
        method_value(
            schema,
            method=method,
            include_raw=True,
        ),
    )


def _root_cause_decision_from_response(
    response: object,
    *,
    structured: bool,
    available_evidence_ids: Set[str],
) -> RootCauseDecision:
    if structured:
        if not isinstance(response, Mapping):
            raise ValueError("Structured decision response must be an envelope.")
        envelope = cast(Mapping[object, object], response)
        if envelope.get("parsing_error") is not None:
            raise ValueError("Structured decision envelope contains a parsing error.")
        parsed = envelope.get("parsed")
        if isinstance(parsed, _RootCauseDecisionSchema):
            schema = parsed
        else:
            schema = _RootCauseDecisionSchema.model_validate(parsed)
        text = json.dumps(schema.model_dump(by_alias=True), ensure_ascii=False)
    else:
        text = _model_text(response)
    return parse_root_cause_decision(
        text,
        available_evidence_ids=available_evidence_ids,
    )


def _validation_decision_from_response(
    response: object,
    *,
    structured: bool,
    available_evidence_ids: Set[str],
) -> RootCauseValidationDecision:
    if structured:
        if not isinstance(response, Mapping):
            raise _StructuredParseFailure("structured_envelope_mismatch")
        envelope = cast(Mapping[object, object], response)
        if "parsed" not in envelope or "parsing_error" not in envelope:
            raise _StructuredParseFailure("structured_envelope_mismatch")
        parsing_error = envelope["parsing_error"]
        if parsing_error is not None:
            raise _StructuredParseFailure(*_parsing_error_codes(parsing_error))
        parsed = envelope["parsed"]
        if isinstance(parsed, _RootCauseValidationSchema):
            schema = parsed
        else:
            try:
                schema = _RootCauseValidationSchema.model_validate(parsed)
            except ValidationError as exc:
                raise _StructuredParseFailure(*_pydantic_error_codes(exc)) from None
        text = json.dumps(schema.model_dump(by_alias=True), ensure_ascii=False)
    else:
        try:
            parsed_json = json.loads(_model_text(response))
        except json.JSONDecodeError:
            raise _StructuredParseFailure("invalid_json") from None
        try:
            schema = _RootCauseValidationSchema.model_validate(parsed_json)
        except ValidationError as exc:
            raise _StructuredParseFailure(*_pydantic_error_codes(exc)) from None
        text = json.dumps(schema.model_dump(by_alias=True), ensure_ascii=False)
    if not set(schema.evidence_ids).issubset(available_evidence_ids):
        raise _StructuredParseFailure("unknown_evidence_id")
    try:
        return parse_root_cause_validation(
            text,
            available_evidence_ids=available_evidence_ids,
        )
    except (TypeError, ValueError):
        raise _StructuredParseFailure("invalid_json_or_schema") from None


def _parsing_error_codes(error: object) -> tuple[StructuredParseErrorCode, ...]:
    if isinstance(error, ValidationError):
        return _pydantic_error_codes(error)
    if isinstance(error, json.JSONDecodeError):
        return ("invalid_json",)
    return ("structured_envelope_mismatch",)


def _pydantic_error_codes(
    error: ValidationError,
) -> tuple[StructuredParseErrorCode, ...]:
    mapped: list[StructuredParseErrorCode] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        error_type = item.get("type")
        code: StructuredParseErrorCode
        if error_type == "missing":
            code = "missing_required_field"
        elif error_type == "literal_error":
            code = "invalid_enum"
        elif error_type in {"list_type", "tuple_type", "set_type", "frozen_set_type"}:
            code = "wrong_container_type"
        elif error_type == "extra_forbidden":
            code = "extra_field"
        else:
            code = "invalid_json_or_schema"
        if code not in mapped:
            mapped.append(code)
        if len(mapped) == 6:
            break
    return tuple(mapped) or ("invalid_json_or_schema",)


def _append_parse_codes(
    target: list[str],
    codes: Sequence[StructuredParseErrorCode],
) -> None:
    for code in codes:
        if code not in target:
            target.append(code)
        if len(target) == 6:
            return


def validate_grounded_candidate(
    *,
    candidate: RootCauseDecision,
    available_evidence_ids: Set[str],
    hypothesis_states: Sequence[Mapping[str, object]],
    observation_decisions: Sequence[Mapping[str, object]],
    decision_vocabulary: Mapping[str, object],
) -> DeterministicValidationResult:
    """Validate one candidate without model output, RAG prose, or hidden answers."""
    supported = _hypothesis_ids_with_status(hypothesis_states, "supported")
    supported_hypothesis_id = supported[0] if len(supported) == 1 else None
    open_hypotheses = _hypothesis_ids_with_status(hypothesis_states, "open")
    unique_supported = supported_hypothesis_id is not None
    no_open_competitor = not open_hypotheses

    labels = _mapping(decision_vocabulary.get("labelsByHypothesis"))
    public_label = _mapping(
        labels.get(supported_hypothesis_id) if supported_hypothesis_id is not None else None
    )
    public_label_match = bool(public_label) and (
        public_label.get("component") == candidate.component
        and public_label.get("mechanism") == candidate.mechanism
    )

    candidate_evidence = set(candidate.evidence_ids)
    task_evidence_only = bool(candidate_evidence) and candidate_evidence.issubset(
        set(available_evidence_ids)
    )
    supporting_observations = _supporting_observations(
        observation_decisions,
        supported_hypothesis_id=supported_hypothesis_id,
    )
    positive_evidence_ids = {
        evidence_id
        for observation in supporting_observations
        for evidence_id in _string_items(observation.get("evidenceIds"))
        if evidence_id in available_evidence_ids
    }
    supporting_evidence_only = bool(candidate_evidence) and candidate_evidence.issubset(
        positive_evidence_ids
    )
    independent_positive_evidence = len(positive_evidence_ids) >= 2
    enough_supporting_observations = len(supporting_observations) >= 2
    supporting_roles_by_summary = {
        summary.strip(): role
        for observation in supporting_observations
        if isinstance((summary := observation.get("summary")), str)
        and summary.strip()
        and (role := observation.get("causalRole"))
        in {"trigger", "mechanism", "impact", "context"}
    }
    trigger_summaries = tuple(
        summary
        for summary, role in supporting_roles_by_summary.items()
        if role == "trigger"
    )
    chain_roles = tuple(
        supporting_roles_by_summary.get(item.strip())
        for item in candidate.causal_chain
    )
    role_order = {"trigger": 0, "mechanism": 1, "context": 2, "impact": 3}
    grounded_causal_chain = (
        2 <= len(candidate.causal_chain) <= 6
        and len(trigger_summaries) == 1
        and all(role in role_order for role in chain_roles)
        and chain_roles.count("trigger") == 1
        and "mechanism" in chain_roles
        and "impact" in chain_roles
        and chain_roles[-1] == "impact"
        and all(
            role_order[cast(str, left)] <= role_order[cast(str, right)]
            for left, right in zip(chain_roles, chain_roles[1:], strict=False)
        )
    )
    trigger_present = (
        len(trigger_summaries) == 1
        and candidate.trigger.strip() == trigger_summaries[0]
    )
    confidence_in_range = 0.0 <= candidate.confidence <= 1.0

    checks = (
        DeterministicCheck("unique_supported_hypothesis", unique_supported),
        DeterministicCheck("no_open_competitor", no_open_competitor),
        DeterministicCheck("public_label_match", public_label_match),
        DeterministicCheck("task_evidence_only", task_evidence_only),
        DeterministicCheck("supporting_evidence_only", supporting_evidence_only),
        DeterministicCheck(
            "independent_positive_evidence", independent_positive_evidence
        ),
        DeterministicCheck(
            "supporting_observations", enough_supporting_observations
        ),
        DeterministicCheck("grounded_causal_chain", grounded_causal_chain),
        DeterministicCheck("trigger_present", trigger_present),
        DeterministicCheck("confidence_in_range", confidence_in_range),
    )
    unsupported_fields: list[RootCauseField] = []
    if not public_label_match:
        unsupported_fields.extend(("component", "mechanism"))
    if not grounded_causal_chain:
        unsupported_fields.append("causalChain")
    if not trigger_present:
        unsupported_fields.append("trigger")
    evidence_check_codes = {
        "unique_supported_hypothesis",
        "no_open_competitor",
        "task_evidence_only",
        "supporting_evidence_only",
        "independent_positive_evidence",
        "supporting_observations",
    }
    missing_evidence = tuple(
        check.code
        for check in checks
        if not check.passed and check.code in evidence_check_codes
    )
    return DeterministicValidationResult(
        passed=all(check.passed for check in checks),
        supported_hypothesis_id=supported_hypothesis_id,
        checks=checks,
        unsupported_fields=tuple(dict.fromkeys(unsupported_fields)),
        missing_evidence=missing_evidence,
    )


def deterministic_checks_payload(
    result: DeterministicValidationResult,
) -> list[dict[str, object]]:
    """Return a secret-safe allowlisted audit representation."""
    return [{"code": check.code, "passed": check.passed} for check in result.checks]


def _hypothesis_ids_with_status(
    states: Sequence[Mapping[str, object]],
    status: str,
) -> tuple[str, ...]:
    return tuple(
        identifier
        for item in states
        if item.get("status") == status
        and isinstance((identifier := item.get("id")), str)
        and identifier
    )


def _supporting_observations(
    observations: Sequence[Mapping[str, object]],
    *,
    supported_hypothesis_id: str | None,
) -> tuple[Mapping[str, object], ...]:
    if supported_hypothesis_id is None:
        return ()
    return tuple(
        item
        for item in observations
        if supported_hypothesis_id in _string_items(item.get("supports"))
    )


def _string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in cast(Sequence[object], value) if isinstance(item, str))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in cast(Mapping[object, object], value).items()
        if isinstance(key, str)
    }


def _model_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(cast(Mapping[object, object], item).get("text", ""))
            if isinstance(item, Mapping)
            else str(item)
            for item in cast(list[object], content)
        )
    return str(content)
