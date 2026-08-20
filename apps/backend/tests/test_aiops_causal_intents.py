import inspect

from super_ai.aiops import causal_intents
from super_ai.aiops.causal_intents import (
    allowed_causal_intents,
    next_causal_refinement_index,
    repair_plan_causal_coverage,
    supported_causal_coverage,
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


def _hypothesis_step(
    step_id: str,
    tool: str,
    causal_intent: str,
    hypotheses: tuple[str, ...],
) -> JsonDict:
    step = _step(step_id, tool, causal_intent)
    step["testsHypotheses"] = list(hypotheses)
    return step


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
    assert allowed_causal_intents("InspectPostgresDeadlockAudit") == frozenset(
        {"trigger", "mechanism"}
    )
    assert allowed_causal_intents("InspectPostgresTransactionResult") == frozenset(
        {"impact"}
    )
    assert allowed_causal_intents("VerifyServiceHealth") == frozenset(
        {"context", "impact"}
    )
    assert allowed_causal_intents("GetServiceMetrics") == frozenset(
        {"context", "mechanism", "impact"}
    )
    assert allowed_causal_intents("InspectPostgres") == frozenset(
        {"trigger", "context", "mechanism"}
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


def test_plan_coverage_rejects_roles_split_across_hypotheses() -> None:
    result = repair_plan_causal_coverage(
        (
            _hypothesis_step(
                "pool",
                "InspectOrderPoolState",
                "context",
                ("lifecycle", "traffic"),
            ),
            _hypothesis_step(
                "sessions",
                "InspectOrderDatabaseSessions",
                "mechanism",
                ("slow", "lock"),
            ),
            _hypothesis_step(
                "health",
                "VerifyOrderDatabaseReachability",
                "impact",
                ("unavailable",),
            ),
            _hypothesis_step(
                "logs",
                "SearchLog",
                "trigger",
                ("slow", "lifecycle", "traffic"),
            ),
        )
    )

    assert result.complete is False
    assert result.target_hypothesis_id == "slow"
    assert result.missing_roles == ("impact",)


def test_plan_coverage_does_not_claim_completion_without_capable_tools() -> None:
    result = repair_plan_causal_coverage(
        (_step("metrics", "GetDatabaseMetrics", "context"),)
    )

    assert result.complete is False
    assert result.target_hypothesis_id == "postgres_deadlock"
    assert result.missing_roles == ("trigger", "impact")
    assert result.ambiguous_trigger is False


def test_plan_coverage_uses_metrics_for_impact_without_overwriting_other_roles() -> None:
    result = repair_plan_causal_coverage(
        (
            _step("pool", "InspectDatabasePool", "context"),
            _step("database", "InspectPostgres", "mechanism"),
            _step("metrics", "GetServiceMetrics", "context"),
            _step("changes", "GetDeploymentChanges", "trigger"),
        )
    )

    assert result.complete is True
    assert [item["causalIntent"] for item in result.steps] == [
        "context",
        "mechanism",
        "impact",
        "trigger",
    ]


def test_log_lifecycle_plan_can_supply_one_trigger_when_runtime_supplies_other_roles() -> None:
    result = repair_plan_causal_coverage(
        (
            _step("logs-1", "SearchLog", "mechanism"),
            _step("logs-2", "SearchLogs", "impact"),
            _step("logs-3", "SearchLog", "context"),
        )
    )

    assert "trigger" in allowed_causal_intents("SearchLog")
    assert result.complete is True
    assert result.missing_roles == ()


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


def test_supported_coverage_counts_only_linked_observations() -> None:
    coverage = supported_causal_coverage(
        hypothesis_states=(
            {
                "id": "database_failure",
                "status": "supported",
                "evidenceIds": ["ev-trigger", "ev-mechanism", "ev-impact"],
            },
        ),
        observation_decisions=(
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-trigger"],
                "causalRole": "trigger",
                "summary": "A bounded trigger was observed.",
            },
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-mechanism"],
                "causalRole": "mechanism",
                "summary": "A bounded mechanism was observed.",
            },
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-impact"],
                "causalRole": "impact",
                "summary": "A bounded impact was observed.",
            },
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-unlinked"],
                "causalRole": "trigger",
                "summary": "This evidence is not linked by the hypothesis state.",
            },
        ),
    )

    assert coverage.trigger_count == 1
    assert coverage.mechanism_count == 1
    assert coverage.impact_count == 1
    assert coverage.complete is True
    assert coverage.missing_roles == ()
    assert coverage.ambiguous_trigger is False


def test_supported_coverage_rejects_multiple_triggers() -> None:
    coverage = supported_causal_coverage(
        hypothesis_states=(
            {
                "id": "database_failure",
                "status": "supported",
                "evidenceIds": ["ev-trigger-1", "ev-trigger-2", "ev-mechanism"],
            },
        ),
        observation_decisions=(
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-trigger-1"],
                "causalRole": "trigger",
                "summary": "First trigger.",
            },
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-trigger-2"],
                "causalRole": "trigger",
                "summary": "Second trigger.",
            },
            {
                "supports": ["database_failure"],
                "evidenceIds": ["ev-mechanism"],
                "causalRole": "mechanism",
                "summary": "Mechanism.",
            },
        ),
    )

    assert coverage.trigger_count == 2
    assert coverage.complete is False
    assert coverage.ambiguous_trigger is True


def test_causal_refinement_selects_unexecuted_missing_role() -> None:
    plan = (
        _step("graph", "InspectPostgresWaitGraph", "mechanism"),
        _step("order", "InspectTransactionResourceOrder", "trigger"),
    )

    assert next_causal_refinement_index(
        plan=plan,
        plan_index=0,
        missing_roles=("trigger",),
        supported_hypothesis_id="postgres_deadlock",
        executed_fingerprints=(),
        fingerprint=lambda step: str(step["id"]),
    ) == 1
    assert next_causal_refinement_index(
        plan=plan,
        plan_index=0,
        missing_roles=("impact",),
        supported_hypothesis_id="postgres_deadlock",
        executed_fingerprints=(),
        fingerprint=lambda step: str(step["id"]),
    ) is None


def test_causal_refinement_revisits_earlier_unexecuted_capable_step() -> None:
    plan = (
        _step("database", "InspectPostgres", "mechanism"),
        _step("changes", "GetDeploymentChanges", "trigger"),
    )

    assert next_causal_refinement_index(
        plan=plan,
        plan_index=len(plan),
        missing_roles=("trigger",),
        supported_hypothesis_id="postgres_deadlock",
        executed_fingerprints=("changes",),
        fingerprint=lambda step: str(step["id"]),
    ) == 0
