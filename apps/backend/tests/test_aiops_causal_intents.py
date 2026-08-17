import inspect

from super_ai.aiops import causal_intents
from super_ai.aiops.causal_intents import (
    allowed_causal_intents,
    repair_plan_causal_coverage,
)
from super_ai.memory.repositories import JsonDict


def _step(
    step_id: str,
    tool: str,
    causal_intent: str,
    *,
    origin: str = "model",
) -> JsonDict:
    return {
        "id": step_id,
        "tool": tool,
        "arguments": {},
        "purpose": f"Inspect {step_id}.",
        "testsHypotheses": ["postgres_deadlock"],
        "causalIntent": causal_intent,
        "causalIntentOrigin": origin,
    }


def test_tool_capabilities_describe_public_observation_semantics() -> None:
    assert allowed_causal_intents("InspectTransactionResourceOrder") == frozenset(
        {"trigger", "mechanism"}
    )
    assert allowed_causal_intents("InspectPostgresWaitGraph") == frozenset(
        {"mechanism"}
    )
    assert allowed_causal_intents("InspectPostgresErrors") == frozenset(
        {"mechanism", "impact"}
    )
    assert allowed_causal_intents("VerifyServiceHealth") == frozenset(
        {"context", "impact"}
    )
    assert allowed_causal_intents("RestartTestService") == frozenset()
    assert allowed_causal_intents("InspectFutureSubsystem") == frozenset({"context"})


def test_tool_capability_registry_is_answer_isolated() -> None:
    source = inspect.getsource(causal_intents).casefold()

    for forbidden in (
        "apy-",
        "ground_truth",
        "primary_cause",
        "root_cause_semantics",
        "oracle",
        "opposite_order_transaction_deadlock",
    ):
        assert forbidden not in source


def test_plan_coverage_minimally_repairs_all_mechanism_plan() -> None:
    result = repair_plan_causal_coverage(
        (
            _step("errors", "InspectPostgresErrors", "mechanism"),
            _step("graph", "InspectPostgresWaitGraph", "mechanism"),
            _step("order", "InspectTransactionResourceOrder", "mechanism"),
        )
    )

    assert result.complete is True
    assert [item["causalIntent"] for item in result.steps] == [
        "impact",
        "mechanism",
        "trigger",
    ]
    assert [item["causalIntentOrigin"] for item in result.steps] == [
        "coverage_repair",
        "model",
        "coverage_repair",
    ]
    assert result.missing_roles == ()
    assert result.ambiguous_trigger is False


def test_plan_coverage_does_not_claim_completion_without_capable_tools() -> None:
    result = repair_plan_causal_coverage(
        (_step("metrics", "GetDatabaseMetrics", "context"),)
    )

    assert result.complete is False
    assert result.missing_roles == ("trigger", "mechanism", "impact")
    assert result.ambiguous_trigger is False


def test_log_only_plan_cannot_be_repaired_into_a_trigger() -> None:
    result = repair_plan_causal_coverage(
        (
            _step("logs-1", "SearchLog", "mechanism"),
            _step("logs-2", "SearchLogs", "impact"),
            _step("logs-3", "SearchLog", "context"),
        )
    )

    assert "trigger" not in allowed_causal_intents("SearchLog")
    assert result.complete is False
    assert result.missing_roles == ("trigger",)


def test_plan_coverage_reports_ambiguous_trigger_when_it_cannot_repair() -> None:
    result = repair_plan_causal_coverage(
        (
            _step("order-1", "InspectTransactionResourceOrder", "trigger"),
            _step("order-2", "GetDeploymentChanges", "trigger"),
        )
    )

    assert result.complete is False
    assert result.missing_roles == ("trigger", "mechanism", "impact")
    assert result.ambiguous_trigger is True
