from __future__ import annotations

from dataclasses import replace

import pytest

from super_ai.aiops.validator_routing import (
    ValidatorRiskContext,
    requires_llm_validation,
)

BASE_CONTEXT = ValidatorRiskContext(deterministic_valid=True)


@pytest.mark.parametrize(
    "override",
    [
        {"used_llm_adjudication": True},
        {"execution_requested": True},
        {"max_risk_tier": "L2"},
        {"compound_root_cause": True},
        {"causal_components": ("nginx", "checkout")},
        {"has_high_quality_conflict": True},
    ],
)
def test_each_risk_condition_requires_llm_validator(
    override: dict[str, object],
) -> None:
    decision = requires_llm_validation(replace(BASE_CONTEXT, **override))  # pyright: ignore[reportCallIssue]

    assert decision.required is True
    assert len(decision.reason_codes) == 1


def test_pure_deterministic_path_skips_llm_validator() -> None:
    decision = requires_llm_validation(BASE_CONTEXT)

    assert decision.required is False
    assert decision.reason_codes == ()
    assert decision.skip_reason == "no_semantic_risk"


def test_failed_deterministic_validation_fails_closed_before_risk_routing() -> None:
    decision = requires_llm_validation(
        replace(
            BASE_CONTEXT,
            deterministic_valid=False,
            used_llm_adjudication=True,
            execution_requested=True,
            max_risk_tier="L3",
        )
    )

    assert decision.required is False
    assert decision.reason_codes == ()
    assert decision.skip_reason == "deterministic_validation_failed"
