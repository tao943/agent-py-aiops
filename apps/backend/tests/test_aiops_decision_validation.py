from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import httpx
import openai
import pytest
from pydantic import ValidationError

from super_ai.aiops.decision_validation import (
    SafeModelFailure,
    SafeModelInvocationFailure,
    _RootCauseDecisionSchema,  # pyright: ignore[reportPrivateUsage]
    _RootCauseValidationSchema,  # pyright: ignore[reportPrivateUsage]
    can_replan_deterministic_gap,
    classify_model_failure,
    deterministic_checks_payload,
    invoke_structured_root_cause_decision,
    invoke_structured_root_cause_validation,
    validate_grounded_candidate,
)
from super_ai.aiops.reasoning import RootCauseDecision


def _decision(
    *,
    component: str = "order-service",
    mechanism: str = "opposite_order_transaction_deadlock",
    trigger: str = "Transactions acquired the same rows in opposite order.",
    causal_chain: tuple[str, ...] = (
        "Transactions acquired the same rows in opposite order.",
        "The wait graph contained a two-session cycle.",
        "PostgreSQL emitted SQLSTATE 40P01.",
    ),
    evidence_ids: tuple[str, ...] = ("ev-error", "ev-cycle", "ev-order"),
    confidence: float = 0.97,
) -> RootCauseDecision:
    return RootCauseDecision(
        component=component,
        mechanism=mechanism,
        trigger=trigger,
        causal_chain=causal_chain,
        evidence_ids=evidence_ids,
        confidence=confidence,
    )


def _states() -> list[dict[str, object]]:
    return [
        {
            "id": "postgres_deadlock",
            "status": "supported",
            "confidence": 1.0,
            "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
        },
        {
            "id": "postgres_lock_wait",
            "status": "refuted",
            "confidence": 0.1,
            "evidenceIds": ["ev-cycle"],
        },
        {
            "id": "postgres_slow_query",
            "status": "refuted",
            "confidence": 0.1,
            "evidenceIds": ["ev-error"],
        },
    ]


def _observations() -> list[dict[str, object]]:
    return [
        {
            "supports": ["postgres_deadlock"],
            "refutes": ["postgres_slow_query"],
            "evidenceIds": ["ev-error"],
            "summary": "PostgreSQL emitted SQLSTATE 40P01.",
            "causalRole": "impact",
        },
        {
            "supports": ["postgres_deadlock"],
            "refutes": ["postgres_lock_wait"],
            "evidenceIds": ["ev-cycle"],
            "summary": "The wait graph contained a two-session cycle.",
            "causalRole": "mechanism",
        },
        {
            "supports": ["postgres_deadlock"],
            "refutes": [],
            "evidenceIds": ["ev-order"],
            "summary": "Transactions acquired the same rows in opposite order.",
            "causalRole": "trigger",
        },
    ]


def _vocabulary() -> dict[str, object]:
    return {
        "labelsByHypothesis": {
            "postgres_deadlock": {
                "component": "order-service",
                "mechanism": "opposite_order_transaction_deadlock",
            },
            "postgres_lock_wait": {
                "component": "postgresql",
                "mechanism": "long_transaction_lock_blocking",
            },
            "postgres_slow_query": {
                "component": "postgresql",
                "mechanism": "slow_query_without_lock",
            },
        }
    }


def _validate(
    candidate: RootCauseDecision,
    *,
    states: list[dict[str, object]] | None = None,
    observations: list[dict[str, object]] | None = None,
    available_evidence_ids: set[str] | None = None,
):
    return validate_grounded_candidate(
        candidate=candidate,
        available_evidence_ids=available_evidence_ids
        or {"ev-error", "ev-cycle", "ev-order"},
        hypothesis_states=states or _states(),
        observation_decisions=observations or _observations(),
        decision_vocabulary=_vocabulary(),
    )


def test_grounded_candidate_passes_every_public_evidence_check() -> None:
    result = _validate(_decision())

    assert result.passed is True
    assert result.supported_hypothesis_id == "postgres_deadlock"
    assert result.unsupported_fields == ()
    assert result.missing_evidence == ()
    assert all(check.passed for check in result.checks)
    assert deterministic_checks_payload(result) == [
        {"code": check.code, "passed": True} for check in result.checks
    ]


@pytest.mark.parametrize(
    ("candidate", "observations", "failed_checks"),
    [
        (
            _decision(),
            [
                *_observations(),
                {
                    "supports": ["postgres_deadlock"],
                    "evidenceIds": ["ev-order"],
                    "summary": "A second trigger summary.",
                    "causalRole": "trigger",
                },
            ],
            {"trigger_present", "grounded_causal_chain"},
        ),
        (
            replace(_decision(), trigger="A different trigger summary."),
            _observations(),
            {"trigger_present"},
        ),
        (
            replace(
                _decision(),
                causal_chain=(
                    "Transactions acquired the same rows in opposite order.",
                    "The wait graph contained a two-session cycle.",
                ),
            ),
            _observations(),
            {"grounded_causal_chain"},
        ),
        (
            replace(
                _decision(),
                causal_chain=(
                    "The wait graph contained a two-session cycle.",
                    "Transactions acquired the same rows in opposite order.",
                    "PostgreSQL emitted SQLSTATE 40P01.",
                ),
            ),
            _observations(),
            {"grounded_causal_chain"},
        ),
    ],
)
def test_grounded_candidate_enforces_unique_ordered_causal_roles(
    candidate: RootCauseDecision,
    observations: list[dict[str, object]],
    failed_checks: set[str],
) -> None:
    result = _validate(candidate, observations=observations)

    failures = {check.code for check in result.checks if not check.passed}
    assert failed_checks.issubset(failures)


@pytest.mark.parametrize(
    ("case", "expected_check"),
    [
        ("multiple_supported", "unique_supported_hypothesis"),
        ("open_competitor", "no_open_competitor"),
        ("wrong_public_label", "public_label_match"),
        ("foreign_evidence_id", "task_evidence_only"),
        ("available_but_non_supporting_evidence_id", "supporting_evidence_only"),
        ("one_positive_evidence", "independent_positive_evidence"),
        ("one_supporting_observation", "supporting_observations"),
        ("invented_causal_step", "grounded_causal_chain"),
        ("blank_trigger", "trigger_present"),
        ("confidence_out_of_range", "confidence_in_range"),
    ],
)
def test_grounded_candidate_fails_closed_for_each_missing_condition(
    case: str,
    expected_check: str,
) -> None:
    candidate = _decision()
    states = _states()
    observations = _observations()
    available = {"ev-error", "ev-cycle", "ev-order"}

    if case == "multiple_supported":
        states[1] = {**states[1], "status": "supported"}
    elif case == "open_competitor":
        states[1] = {**states[1], "status": "open"}
    elif case == "wrong_public_label":
        candidate = replace(candidate, mechanism="generic_deadlock")
    elif case == "foreign_evidence_id":
        candidate = replace(candidate, evidence_ids=("ev-error", "ev-foreign"))
    elif case == "available_but_non_supporting_evidence_id":
        available.add("ev-alert")
        candidate = replace(candidate, evidence_ids=("ev-alert",))
    elif case == "one_positive_evidence":
        observations = [observations[0]]
        candidate = replace(
            candidate,
            causal_chain=(observations[0]["summary"], observations[0]["summary"]),
            evidence_ids=("ev-error",),
        )
    elif case == "one_supporting_observation":
        observations = [
            {
                **observations[0],
                "evidenceIds": ["ev-error", "ev-cycle"],
            }
        ]
        candidate = replace(
            candidate,
            causal_chain=(observations[0]["summary"], observations[0]["summary"]),
            evidence_ids=("ev-error", "ev-cycle"),
        )
    elif case == "invented_causal_step":
        candidate = replace(
            candidate,
            causal_chain=(candidate.causal_chain[0], "An invented causal assertion."),
        )
    elif case == "blank_trigger":
        candidate = replace(candidate, trigger="   ")
    elif case == "confidence_out_of_range":
        candidate = replace(candidate, confidence=1.1)

    result = _validate(
        candidate,
        states=states,
        observations=observations,
        available_evidence_ids=available,
    )

    assert result.passed is False
    assert expected_check in {check.code for check in result.checks if not check.passed}


def test_knowledge_like_available_id_cannot_count_as_positive_evidence() -> None:
    candidate = replace(_decision(), evidence_ids=("ev-knowledge", "ev-alert"))

    result = _validate(
        candidate,
        available_evidence_ids={
            "ev-error",
            "ev-cycle",
            "ev-order",
            "ev-knowledge",
            "ev-alert",
        },
    )

    assert result.passed is False
    assert "supporting_evidence_only" in {
        check.code for check in result.checks if not check.passed
    }


VALIDATION_JSON = (
    '{"status":"valid","evidenceIds":["ev-1","ev-2"],'
    '"unsupportedFields":[],"missingEvidence":[],'
    '"summary":"The public observations support every field."}'
)


class RaisingChatModel:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def ainvoke(self, input: object) -> object:
        _ = input
        self.calls += 1
        raise self.error


class SequenceChatModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0

    async def ainvoke(self, input: object) -> object:
        _ = input
        response = self.responses[self.calls]
        self.calls += 1
        return response


class StructuredRunnable:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls = 0

    async def ainvoke(self, input: object) -> object:
        _ = input
        response = self.responses[self.calls]
        self.calls += 1
        return response


class StructuredCapableChatModel:
    def __init__(
        self,
        responses: list[dict[str, object]],
        *,
        expected_method: str = "function_calling",
    ) -> None:
        self.structured = StructuredRunnable(responses)
        self.raw_calls = 0
        self.wrapper_calls = 0
        self.expected_method = expected_method

    def with_structured_output(
        self,
        _schema: type[object],
        **kwargs: Any,
    ) -> StructuredRunnable:
        assert kwargs == {"method": self.expected_method, "include_raw": True}
        self.wrapper_calls += 1
        return self.structured

    async def ainvoke(self, input: object) -> object:
        _ = input
        self.raw_calls += 1
        raise AssertionError("Raw fallback must not run for a structured-capable model.")


class FailingStructuredSetupChatModel:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.raw_calls = 0

    def with_structured_output(self, _schema: type[object], **_kwargs: Any) -> object:
        raise self.error

    async def ainvoke(self, input: object) -> object:
        _ = input
        self.raw_calls += 1
        raise AssertionError("Raw fallback must not run after structured setup failure.")


def _validation_schema(*, evidence_ids: list[str] | None = None):
    return _RootCauseValidationSchema(
        status="valid",
        evidenceIds=evidence_ids or ["ev-1", "ev-2"],
        unsupportedFields=[],
        missingEvidence=[],
        summary="The public observations support every field.",
    )


def _decision_schema(*, evidence_ids: list[str] | None = None):
    return _RootCauseDecisionSchema(
        component="order-service",
        mechanism="opposite_order_transaction_deadlock",
        trigger="Transactions acquired the same rows in opposite order.",
        causalChain=[
            "Transactions acquired the same rows in opposite order.",
            "The wait graph contained a two-session cycle.",
            "PostgreSQL emitted SQLSTATE 40P01.",
        ],
        evidenceIds=evidence_ids or ["ev-trigger", "ev-mechanism", "ev-impact"],
        confidence=0.97,
    )


def _decision_json() -> str:
    return _decision_schema().model_dump_json(by_alias=True)


@pytest.mark.asyncio
async def test_structured_decision_unpacks_langchain_envelope() -> None:
    model = StructuredCapableChatModel(
        [{"raw": object(), "parsed": _decision_schema(), "parsing_error": None}]
    )

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
    )

    assert outcome.decision is not None
    assert outcome.attempts == 1
    assert outcome.error_category is None
    assert model.wrapper_calls == 1
    assert model.raw_calls == 0


@pytest.mark.asyncio
async def test_structured_decision_uses_configured_json_mode() -> None:
    model = StructuredCapableChatModel(
        [{"raw": object(), "parsed": _decision_schema(), "parsing_error": None}],
        expected_method="json_mode",
    )

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
        structured_output_method="json_mode",
    )

    assert outcome.decision is not None
    assert outcome.error_category is None
    assert model.wrapper_calls == 1
    assert model.raw_calls == 0


@pytest.mark.asyncio
async def test_structured_validator_uses_callback_for_format_retry() -> None:
    model = StructuredCapableChatModel(
        [
            {"raw": object(), "parsed": None, "parsing_error": ValueError()},
            {"raw": object(), "parsed": _validation_schema(), "parsing_error": None},
        ],
        expected_method="json_mode",
    )
    calls: list[object] = []

    async def invoke(invoker: object, prompt: object) -> object:
        calls.append(prompt)
        return await cast(Any, invoker).ainvoke(prompt)

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="Validate public evidence.",
        available_evidence_ids={"ev-1", "ev-2"},
        structured_output_method="json_mode",
        invoke=invoke,
    )

    assert outcome.decision is not None
    assert outcome.attempts == 2
    assert len(calls) == 2
    assert model.wrapper_calls == 1
    assert calls[1] != calls[0]


@pytest.mark.asyncio
async def test_structured_validator_preserves_safe_callback_failure() -> None:
    model = StructuredCapableChatModel(
        [{"raw": object(), "parsed": _validation_schema(), "parsing_error": None}],
        expected_method="json_mode",
    )

    async def invoke(_invoker: object, _prompt: object) -> object:
        raise SafeModelInvocationFailure(
            SafeModelFailure(
                code="model_call_budget_exhausted",
                phase="model_invoke",
                retryable=False,
            )
        )

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="Validate public evidence.",
        available_evidence_ids={"ev-1", "ev-2"},
        structured_output_method="json_mode",
        invoke=invoke,
    )

    assert outcome.decision is None
    assert outcome.error_category == "model_call_failed"
    assert outcome.error_code == "model_call_budget_exhausted"
    assert outcome.error_phase == "model_invoke"
    assert outcome.retryable is False
    assert outcome.attempts == 1


@pytest.mark.asyncio
async def test_structured_decision_keeps_raw_model_compatibility() -> None:
    model = SequenceChatModel([_decision_json()])

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
    )

    assert outcome.decision is not None
    assert outcome.attempts == 1
    assert outcome.error_category is None


@pytest.mark.asyncio
async def test_structured_decision_reasks_once_after_parse_failure() -> None:
    model = SequenceChatModel(["not-json", _decision_json()])

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
    )

    assert outcome.decision is not None
    assert outcome.attempts == 2
    assert model.calls == 2


@pytest.mark.asyncio
async def test_structured_decision_stops_after_one_format_retry() -> None:
    model = SequenceChatModel(["secret raw response", "still invalid"])

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "retry_exhausted"
    assert outcome.attempts == 2
    assert outcome.error_codes == ("invalid_json_or_schema",)
    assert "secret" not in repr(outcome)


@pytest.mark.asyncio
async def test_structured_decision_classifies_api_timeout_safely() -> None:
    model = RaisingChatModel(
        openai.APITimeoutError(
            request=httpx.Request("POST", "https://provider.test/v1/chat/completions")
        )
    )

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "model_call_failed"
    assert outcome.error_code == "timeout"
    assert outcome.error_phase == "model_invoke"
    assert outcome.retryable is True
    assert outcome.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        NotImplementedError("secret unsupported details"),
        TypeError("secret schema incompatibility"),
    ),
)
async def test_structured_decision_rejects_unsupported_setup_safely(
    error: Exception,
) -> None:
    model = FailingStructuredSetupChatModel(error)

    outcome = await invoke_structured_root_cause_decision(
        model=model,
        prompt="public decision prompt",
        available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "model_call_failed"
    assert outcome.error_code == "structured_output_unsupported"
    assert outcome.error_phase == "structured_invoker_setup"
    assert outcome.retryable is False
    assert outcome.attempts == 0
    assert "secret" not in repr(outcome)
    assert model.raw_calls == 0


@pytest.mark.asyncio
async def test_structured_validator_classifies_provider_failure_without_retry() -> None:
    model = RaisingChatModel(TimeoutError("provider timeout"))

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "model_call_failed"
    assert outcome.error_code == "timeout"
    assert outcome.error_phase == "model_invoke"
    assert outcome.retryable is True
    assert outcome.http_status_class is None
    assert outcome.attempts == 1
    assert model.calls == 1


def _status_error(
    error_type: type[openai.APIStatusError],
    status_code: int,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return error_type("secret provider body", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "code", "retryable", "status_class"),
    (
        (TimeoutError("secret timeout text"), "timeout", True, None),
        (
            openai.APIConnectionError(
                request=httpx.Request("POST", "https://provider.test")
            ),
            "connection",
            True,
            None,
        ),
        (_status_error(openai.AuthenticationError, 401), "authentication", False, "4xx"),
        (
            _status_error(openai.PermissionDeniedError, 403),
            "permission_denied",
            False,
            "4xx",
        ),
        (_status_error(openai.RateLimitError, 429), "rate_limit", True, "4xx"),
        (_status_error(openai.BadRequestError, 400), "provider_4xx", False, "4xx"),
        (
            _status_error(openai.InternalServerError, 500),
            "provider_5xx",
            True,
            "5xx",
        ),
        (RuntimeError("api-key-and-response-body"), "unknown", False, None),
    ),
)
def test_model_failure_classification_is_allowlisted(
    error: Exception,
    code: str,
    retryable: bool,
    status_class: str | None,
) -> None:
    result = classify_model_failure(error, phase="model_invoke")

    assert result.code == code
    assert result.retryable is retryable
    assert result.http_status_class == status_class
    assert "api-key" not in repr(result)
    assert "response-body" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    (
        (NotImplementedError("secret unsupported details"), "structured_output_unsupported"),
        (RuntimeError("secret setup failure"), "unknown"),
    ),
)
async def test_structured_validator_classifies_setup_failure_safely(
    error: Exception,
    code: str,
) -> None:
    model = FailingStructuredSetupChatModel(error)

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "model_call_failed"
    assert outcome.error_code == code
    assert outcome.error_phase == "structured_invoker_setup"
    assert outcome.retryable is False
    assert outcome.http_status_class is None
    assert "secret" not in repr(outcome)
    assert model.raw_calls == 0


@pytest.mark.asyncio
async def test_structured_validator_reasks_once_after_parse_failure() -> None:
    model = SequenceChatModel(["not-json", VALIDATION_JSON])

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is not None
    assert outcome.error_category is None
    assert outcome.attempts == 2
    assert outcome.error_codes == ("invalid_json",)
    assert model.calls == 2


@pytest.mark.asyncio
async def test_structured_validator_stops_after_one_format_retry() -> None:
    model = SequenceChatModel(["not-json", "still-not-json"])

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "retry_exhausted"
    assert outcome.attempts == 2
    assert outcome.error_codes == ("invalid_json",)


@pytest.mark.asyncio
async def test_structured_validator_preserves_explicit_model_rejection() -> None:
    model = SequenceChatModel(
        [
            (
                '{"status":"invalid","evidenceIds":["ev-1","ev-2"],'
                '"unsupportedFields":["trigger"],'
                '"missingEvidence":["Trigger evidence is missing."],'
                '"summary":"The trigger is unsupported."}'
            )
        ]
    )

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is not None
    assert outcome.decision.status == "invalid"
    assert outcome.error_category == "model_rejected"
    assert outcome.attempts == 1


@pytest.mark.asyncio
async def test_structured_validator_unpacks_langchain_envelope() -> None:
    model = StructuredCapableChatModel(
        [{"raw": object(), "parsed": _validation_schema(), "parsing_error": None}]
    )

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is not None
    assert outcome.decision.status == "valid"
    assert outcome.error_category is None
    assert outcome.attempts == 1
    assert model.wrapper_calls == 1
    assert model.structured.calls == 1
    assert model.raw_calls == 0


@pytest.mark.asyncio
async def test_structured_validator_rejects_unknown_envelope_evidence_after_retry() -> None:
    invalid_envelope = {
        "raw": object(),
        "parsed": _validation_schema(evidence_ids=["ev-foreign"]),
        "parsing_error": None,
    }
    model = StructuredCapableChatModel([invalid_envelope, invalid_envelope])

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "retry_exhausted"
    assert outcome.attempts == 2
    assert outcome.error_codes == ("unknown_evidence_id",)
    assert model.structured.calls == 2


def _schema_error(payload: object) -> ValidationError:
    try:
        _RootCauseValidationSchema.model_validate(payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("Test payload must fail schema validation.")


def _structured_parse_case(code: str) -> dict[str, object]:
    base = {
        "status": "valid",
        "evidenceIds": ["ev-1", "ev-2"],
        "unsupportedFields": [],
        "missingEvidence": [],
        "summary": "sentinel-public-summary",
    }
    if code == "invalid_json":
        return {
            "raw": object(),
            "parsed": None,
            "parsing_error": json.JSONDecodeError(
                "sentinel-json-error", "sentinel-raw-value", 0
            ),
        }
    if code == "structured_envelope_mismatch":
        return {"raw": object(), "parsing_error": None}
    if code == "missing_required_field":
        payload = {key: value for key, value in base.items() if key != "summary"}
        return {
            "raw": object(),
            "parsed": None,
            "parsing_error": _schema_error(payload),
        }
    if code == "invalid_enum":
        return {
            "raw": object(),
            "parsed": None,
            "parsing_error": _schema_error({**base, "status": "sentinel-invalid"}),
        }
    if code == "wrong_container_type":
        return {
            "raw": object(),
            "parsed": None,
            "parsing_error": _schema_error(
                {**base, "evidenceIds": "sentinel-not-a-list"}
            ),
        }
    if code == "extra_field":
        return {
            "raw": object(),
            "parsed": None,
            "parsing_error": _schema_error({**base, "sentinelExtra": True}),
        }
    if code == "unknown_evidence_id":
        return {
            "raw": object(),
            "parsed": _validation_schema(evidence_ids=["ev-sentinel-foreign"]),
            "parsing_error": None,
        }
    if code == "invalid_json_or_schema":
        return {
            "raw": object(),
            "parsed": {
                **base,
                "status": "valid",
                "unsupportedFields": ["trigger"],
            },
            "parsing_error": None,
        }
    raise AssertionError(f"Unknown test case: {code}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    (
        "invalid_json",
        "structured_envelope_mismatch",
        "missing_required_field",
        "invalid_enum",
        "wrong_container_type",
        "extra_field",
        "unknown_evidence_id",
        "invalid_json_or_schema",
    ),
)
async def test_structured_validator_reports_safe_parse_error_code(code: str) -> None:
    invalid = _structured_parse_case(code)
    model = StructuredCapableChatModel([invalid, invalid])

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is None
    assert outcome.error_category == "retry_exhausted"
    assert outcome.error_phase == "structured_parse"
    assert outcome.error_codes == (code,)
    assert "sentinel" not in repr(outcome)


@pytest.mark.asyncio
async def test_structured_validator_keeps_first_parse_code_after_correction() -> None:
    invalid = _structured_parse_case("missing_required_field")
    valid = {
        "raw": object(),
        "parsed": _validation_schema(),
        "parsing_error": None,
    }
    model = StructuredCapableChatModel([invalid, valid])

    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )

    assert outcome.decision is not None
    assert outcome.error_category is None
    assert outcome.attempts == 2
    assert outcome.error_codes == ("missing_required_field",)


def test_deterministic_evidence_gap_replans_only_with_unexecuted_discovered_tool() -> None:
    result = _validate(
        replace(
            _decision(),
            causal_chain=(
                "PostgreSQL emitted SQLSTATE 40P01.",
                "PostgreSQL emitted SQLSTATE 40P01.",
            ),
            evidence_ids=("ev-error",),
        ),
        observations=[_observations()[0]],
    )

    assert can_replan_deterministic_gap(
        result,
        recommended_tools=("InspectPostgresWaitGraph",),
        available_tools={"InspectPostgresWaitGraph"},
        executed_tools={"InspectPostgresErrors"},
    ) is True
    assert can_replan_deterministic_gap(
        result,
        recommended_tools=(),
        available_tools={"InspectPostgresWaitGraph"},
        executed_tools={"InspectPostgresErrors"},
    ) is False
    assert can_replan_deterministic_gap(
        result,
        recommended_tools=("InspectPostgresWaitGraph",),
        available_tools={"InspectPostgresWaitGraph"},
        executed_tools={"InspectPostgresWaitGraph"},
    ) is False


def test_deterministic_integrity_gap_never_replans_for_an_evidence_tool() -> None:
    result = _validate(replace(_decision(), mechanism="unpublished_mechanism"))

    assert can_replan_deterministic_gap(
        result,
        recommended_tools=("InspectPostgresWaitGraph",),
        available_tools={"InspectPostgresWaitGraph"},
        executed_tools=set(),
    ) is False
