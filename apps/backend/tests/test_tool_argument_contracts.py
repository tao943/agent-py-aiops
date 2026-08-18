from __future__ import annotations

import pytest
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from super_ai.mcp.tool_arguments import (
    ToolArgumentContract,
    ToolArgumentContractError,
    constrain_tool_definitions,
    normalize_tool_arguments,
    tool_step_fingerprint,
)
from super_ai.mcp_client import McpToolDefinition


def postgres_error_contract() -> ToolArgumentContract:
    return ToolArgumentContract(
        tool_name="InspectPostgresErrors",
        registered_calls=(
            {"service": "order-service", "windowMinutes": 15},
        ),
    )


def retry_policy_contract() -> ToolArgumentContract:
    return ToolArgumentContract(
        tool_name="InspectClientRetryPolicy",
        registered_calls=(
            {"client": "checkout-client", "view": "effective-policy"},
            {"client": "checkout-client", "view": "sampled-timeline"},
        ),
    )


@pytest.mark.parametrize(
    "model_arguments",
    [
        {},
        {"service": "order-service", "windowMinutes": 30},
        {"service": "wrong-service", "windowMinutes": 60},
    ],
)
def test_singleton_contract_replaces_runtime_owned_arguments(
    model_arguments: dict[str, object],
) -> None:
    contract = postgres_error_contract()

    normalized = normalize_tool_arguments(
        contract.tool_name,
        model_arguments,
        {contract.tool_name: contract},
    )

    assert normalized == {"service": "order-service", "windowMinutes": 15}


@pytest.mark.parametrize("view", ["effective-policy", "sampled-timeline"])
def test_multi_call_contract_preserves_a_registered_variant(view: str) -> None:
    contract = retry_policy_contract()

    normalized = normalize_tool_arguments(
        contract.tool_name,
        {"client": "wrong-client", "view": view},
        {contract.tool_name: contract},
    )

    assert normalized == {"client": "checkout-client", "view": view}


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"client": "checkout-client"}, "ambiguous_variant"),
        (
            {"client": "checkout-client", "view": "unregistered"},
            "invalid_variant",
        ),
        (
            {
                "client": "checkout-client",
                "view": "effective-policy",
                "oracle": True,
            },
            "unknown_field",
        ),
    ],
)
def test_multi_call_contract_rejects_unsafe_selection(
    arguments: dict[str, object],
    code: str,
) -> None:
    contract = retry_policy_contract()

    with pytest.raises(ToolArgumentContractError) as captured:
        normalize_tool_arguments(
            contract.tool_name,
            arguments,
            {contract.tool_name: contract},
        )

    assert captured.value.code == code
    assert "unregistered" not in str(captured.value)


def test_tool_without_runtime_contract_is_copied_unchanged() -> None:
    arguments = {"route": "checkout"}

    normalized = normalize_tool_arguments("InspectGateway", arguments, {})

    assert normalized == arguments
    assert normalized is not arguments


def test_constrained_schema_composes_original_rules_with_exact_calls() -> None:
    definition = McpToolDefinition(
        name="InspectClientRetryPolicy",
        description="Inspect a bounded retry-policy view.",
        input_schema={
            "type": "object",
            "required": ["client", "view"],
            "additionalProperties": False,
            "properties": {
                "client": {"type": "string", "minLength": 3},
                "view": {"type": "string"},
            },
        },
        server_name="snapshot",
    )
    contract = retry_policy_contract()

    constrained = constrain_tool_definitions(
        [definition],
        {contract.tool_name: contract},
    )[0]

    assert constrained.name == definition.name
    assert constrained.description == definition.description
    assert constrained.server_name == definition.server_name
    assert "allOf" in constrained.input_schema
    validator_class = validator_for(constrained.input_schema)
    validator_class.check_schema(constrained.input_schema)
    validator = validator_class(constrained.input_schema)
    validator.validate(
        {"client": "checkout-client", "view": "effective-policy"}
    )
    validator.validate(
        {"client": "checkout-client", "view": "sampled-timeline"}
    )
    with pytest.raises(ValidationError):
        validator.validate({"client": "checkout-client"})
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "client": "checkout-client",
                "view": "effective-policy",
                "extra": True,
            }
        )
    with pytest.raises(ValidationError):
        validator.validate(
            {"client": "checkout-client", "view": "unregistered"}
        )


def test_fingerprint_is_canonical_and_keeps_valid_variants_distinct() -> None:
    effective = {"service": "order-service", "windowMinutes": 15}
    reversed_effective = dict(reversed(list(effective.items())))

    assert tool_step_fingerprint(
        "InspectPostgresErrors", effective
    ) == tool_step_fingerprint("InspectPostgresErrors", reversed_effective)
    assert tool_step_fingerprint(
        "InspectClientRetryPolicy",
        {"client": "checkout-client", "view": "effective-policy"},
    ) != tool_step_fingerprint(
        "InspectClientRetryPolicy",
        {"client": "checkout-client", "view": "sampled-timeline"},
    )
