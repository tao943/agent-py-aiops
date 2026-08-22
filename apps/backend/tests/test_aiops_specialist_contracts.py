from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from pydantic import ValidationError

from super_ai.aiops.investigation import EvidenceClaim, JsonValue
from super_ai.aiops.specialists import (
    PublicAssessmentSignal,
    SharedRunContext,
    SpecialistAnalysisStatus,
    SpecialistAssignment,
    SpecialistEvidenceAnalysisOutput,
    SpecialistEvidenceStatus,
    SpecialistLocalPlanOutput,
    SpecialistPlanStep,
    SpecialistResult,
    SpecialistRole,
    SpecialistState,
    SpecialistTerminalStatus,
    derive_specialist_terminal_status,
    specialist_execution_key,
    specialist_result_checksum,
    specialist_result_legacy_checksum,
)


def _deadlines() -> tuple[datetime, datetime]:
    soft = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
    return soft, soft + timedelta(minutes=2)


def _trusted_log_arguments() -> dict[str, object]:
    return {
        "Region": "ap-guangzhou",
        "TopicId": "topic-public",
        "From": 1_787_286_600_000,
        "To": 1_787_286_660_000,
        "Query": 'run_id:"safe-run" AND incident_id:"safe-incident"',
        "Limit": 20,
    }


def _context(**overrides: object) -> SharedRunContext:
    soft, hard = _deadlines()
    values: dict[str, object] = {
        "owner_user_id": "owner-safe",
        "task_id": "task-safe",
        "graph_version": "evidence-driven-v4",
        "public_incident_input": {"alert": {"service": "order-api"}},
        "public_hypotheses": (
            "order_connection_lifecycle_failure",
            "order_database_lock_wait",
        ),
        "decision_vocabulary": {
            "components": ("order-api", "postgresql"),
            "mechanisms": ("connection_lifecycle_failure",),
        },
        "allowed_tools_by_specialist": {
            "runtime": frozenset(
                {"InspectOrderPoolState", "InspectOrderDatabaseSessions"}
            ),
            "log": frozenset({"SearchLog"}),
        },
        "trusted_arguments_by_specialist": {
            "runtime": {
                "InspectOrderPoolState": {},
                "InspectOrderDatabaseSessions": {},
            },
            "log": {"SearchLog": _trusted_log_arguments()},
        },
        "global_soft_deadline_at": soft,
        "global_hard_deadline_at": hard,
        "global_model_budget": 8,
    }
    values.update(overrides)
    return SharedRunContext(**values)  # type: ignore[arg-type]


def _assignment(
    role: SpecialistRole = "runtime", **overrides: object
) -> SpecialistAssignment:
    soft, hard = _deadlines()
    if role == "runtime":
        tools = frozenset({"InspectOrderPoolState"})
        bindings: dict[str, dict[str, object]] = {"InspectOrderPoolState": {}}
    else:
        tools = frozenset({"SearchLog"})
        bindings = {"SearchLog": _trusted_log_arguments()}
    values: dict[str, object] = {
        "role": role,
        "objective": "Test public connection lifecycle hypotheses.",
        "hypotheses_to_test": ("order_connection_lifecycle_failure",),
        "required_causal_roles": ("trigger", "mechanism", "impact"),
        "allowed_tools": tools,
        "trusted_arguments_by_tool": bindings,
        "maximum_tool_steps": 3,
        "model_call_budget": 2,
        "soft_deadline_at": soft,
        "hard_deadline_at": hard,
    }
    values.update(overrides)
    return SpecialistAssignment(**values)  # type: ignore[arg-type]


def _claim() -> EvidenceClaim:
    return EvidenceClaim(
        claim_id="pool_capacity",
        value={"poolAtCapacity": True, "freeConnections": 0},
        quality="direct",
        causal_role="mechanism",
        supports=("order_connection_lifecycle_failure",),
        refutes=(),
        evidence_ids=("ev-pool",),
        target_component="order-api",
        observed_at=datetime(2026, 8, 21, 6, 1, tzinfo=timezone.utc),
        time_scope="incident_window",
    )


def _signal() -> PublicAssessmentSignal:
    return PublicAssessmentSignal(
        hypothesis_id="order_connection_lifecycle_failure",
        disposition="supported",
        evidence_ids=("ev-pool",),
        summary="Pool evidence supports lifecycle investigation.",
    )


def _result(**overrides: object) -> SpecialistResult:
    values: dict[str, object] = {
        "role": "runtime",
        "terminal_status": "completed",
        "evidence_status": "complete",
        "analysis_status": "complete",
        "analysis_error_code": None,
        "analysis_attempt_count": 1,
        "soft_deadline_exceeded": False,
        "hard_deadline_exceeded": False,
        "expected_tool_count": 1,
        "tested_hypotheses": ("order_connection_lifecycle_failure",),
        "evidence_ids": ("ev-pool",),
        "fact_candidates": (_claim(),),
        "proposed_assessments": (_signal(),),
        "unresolved_questions": (),
        "completed_steps": ("runtime-1",),
        "model_call_count": 2,
        "duration_ms": 1234,
    }
    values.update(overrides)
    return SpecialistResult.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("evidence_status", "analysis_status", "expected"),
    (
        ("complete", "complete", "completed"),
        ("complete", "degraded", "inconclusive"),
        ("complete", "timeout", "inconclusive"),
        ("partial", "complete", "inconclusive"),
        ("none", "timeout", "timeout"),
        ("none", "failed", "failed"),
        ("none", "skipped", "failed"),
    ),
)
def test_specialist_health_derives_legacy_terminal_status(
    evidence_status: SpecialistEvidenceStatus,
    analysis_status: SpecialistAnalysisStatus,
    expected: SpecialistTerminalStatus,
) -> None:
    assert derive_specialist_terminal_status(
        evidence_status,
        analysis_status,
    ) == expected


def test_follow_up_questions_do_not_degrade_complete_analysis() -> None:
    result = _result(
        unresolved_questions=("Which deploy first changed checkout latency?",),
    )

    assert result.terminal_status == "completed"
    assert result.follow_up_question_count == 1
    assert result.completed_tool_count == 1


def test_specialist_result_rejects_inconsistent_legacy_terminal_status() -> None:
    with pytest.raises(ValueError, match="terminal"):
        _result(terminal_status="inconclusive")


def test_specialist_result_checksum_covers_health_and_retains_v1_algorithm() -> None:
    result = _result()

    assert specialist_result_legacy_checksum(result) != result.result_checksum
    changed = replace(
        result,
        analysis_status="degraded",
        terminal_status="inconclusive",
        result_checksum="",
    )
    assert changed.result_checksum != result.result_checksum


def test_context_deep_freezes_all_shared_memory() -> None:
    context = _context()

    with pytest.raises(TypeError):
        context.public_incident_input["alert"] = {}  # type: ignore[index]
    nested = cast(
        Mapping[str, JsonValue],
        context.public_incident_input["alert"],
    )
    assert isinstance(nested, dict) is False
    with pytest.raises(TypeError):
        nested["service"] = "changed"  # pyright: ignore[reportIndexIssue]
    with pytest.raises(TypeError):
        context.trusted_arguments_by_specialist["log"]["SearchLog"]["Limit"] = 100  # pyright: ignore[reportIndexIssue]
    with pytest.raises(FrozenInstanceError):
        context.task_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "private_value",
    (
        {"ground_truth": "hidden"},
        {"nested": {"oracle": "hidden"}},
        {"primary_cause": "hidden"},
        {"prompt": "hidden"},
        {"raw_response": "hidden"},
        {"credential": "hidden"},
    ),
)
def test_context_rejects_nested_private_or_answer_data(
    private_value: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="private"):
        _context(public_incident_input=private_value)


def test_assignments_enforce_source_isolation_and_exact_log_scope() -> None:
    runtime = _assignment()
    log = _assignment("log")

    assert runtime.allowed_tools == frozenset({"InspectOrderPoolState"})
    assert set(log.trusted_arguments_by_tool["SearchLog"]) == {
        "Region",
        "TopicId",
        "From",
        "To",
        "Query",
        "Limit",
    }
    with pytest.raises(ValueError, match="source"):
        _assignment(allowed_tools=frozenset({"SearchLog"}))
    with pytest.raises(ValueError, match="source"):
        _assignment("log", allowed_tools=frozenset({"InspectOrderPoolState"}))
    with pytest.raises(ValueError, match="binding"):
        _assignment(
            "log",
            trusted_arguments_by_tool={"SearchLog": {"Query": "safe"}},
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("maximum_tool_steps", 4, "steps"),
        ("model_call_budget", 3, "budget"),
        ("allowed_tools", frozenset({"RestartService"}), "recovery"),
        ("objective", "ReadGroundTruth and inspect the pool.", "private"),
    ),
)
def test_assignments_reject_unbounded_or_private_work(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _assignment(**{field: value})  # pyright: ignore[reportArgumentType]


def test_plan_steps_and_state_are_immutable_and_bounded() -> None:
    step = SpecialistPlanStep(
        step_id="runtime-1",
        tool_name="InspectOrderPoolState",
        tested_hypotheses=("order_connection_lifecycle_failure",),
        causal_intent="mechanism",
        proposed_arguments={"order": "stable"},
    )
    state = SpecialistState(
        assignment=_assignment(),
        local_plan=(step,),
        current_step=0,
        local_observations=(_claim(),),
        local_hypothesis_signals=(_signal(),),
        unresolved_questions=(),
        model_call_count=1,
        deadline_state="active",
        terminal_status=None,
    )

    with pytest.raises(TypeError):
        step.proposed_arguments["order"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        state.current_step = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="assignment"):
        SpecialistState(
            assignment=_assignment(),
            local_plan=(
                replace(step, tool_name="SearchLog"),
            ),
            current_step=0,
            local_observations=(),
            local_hypothesis_signals=(),
            unresolved_questions=(),
            model_call_count=0,
            deadline_state="active",
            terminal_status=None,
        )


def test_local_plan_output_is_frozen_bounded_and_forbids_extra_fields() -> None:
    step_payload: dict[str, object] = {
        "step_id": "runtime-1",
        "tool_name": "InspectOrderPoolState",
        "tested_hypotheses": ["order_connection_lifecycle_failure"],
        "causal_intent": "mechanism",
        "proposed_arguments": {},
    }
    payload: dict[str, object] = {"steps": [step_payload]}
    output = SpecialistLocalPlanOutput.model_validate(payload)

    assert output.steps[0].step_id == "runtime-1"
    with pytest.raises(ValidationError):
        SpecialistLocalPlanOutput.model_validate({**payload, "prompt": "hidden"})
    with pytest.raises(ValidationError):
        SpecialistLocalPlanOutput.model_validate(
            {"steps": [step_payload] * 4}
        )
    with pytest.raises(ValidationError):
        SpecialistLocalPlanOutput.model_validate(
            {
                "steps": [
                    {
                        **step_payload,
                        "proposed_arguments": {"oracle": "hidden"},
                    }
                ]
            }
        )


def test_evidence_analysis_output_rejects_private_content() -> None:
    claim = _claim()
    signal = _signal()
    payload = {
        "tested_hypotheses": ["order_connection_lifecycle_failure"],
        "fact_candidates": [
            {
                "claim_id": claim.claim_id,
                "value": {"poolAtCapacity": True, "freeConnections": 0},
                "quality": claim.quality,
                "causal_role": claim.causal_role,
                "supports": list(claim.supports),
                "refutes": list(claim.refutes),
                "evidence_ids": list(claim.evidence_ids),
                "target_component": claim.target_component,
                "observed_at": claim.observed_at,
                "time_scope": claim.time_scope,
            }
        ],
        "proposed_assessments": [
            {
                "hypothesis_id": signal.hypothesis_id,
                "disposition": signal.disposition,
                "evidence_ids": list(signal.evidence_ids),
                "summary": signal.summary,
            }
        ],
        "unresolved_questions": [],
    }
    output = SpecialistEvidenceAnalysisOutput.model_validate(payload)

    assert output.fact_candidates[0].to_contract() == claim
    with pytest.raises(ValidationError):
        SpecialistEvidenceAnalysisOutput.model_validate(
            {
                **payload,
                "unresolved_questions": ["Inspect oracle output"],
            }
        )


def test_execution_identity_is_canonical_and_scope_sensitive() -> None:
    def key(
        *,
        task_id: str = "task-safe",
        graph_version: str = "v4",
        role: SpecialistRole = "runtime",
        role_name: str = "local_plan",
        logical_step: str = "runtime-1",
        arguments: Mapping[str, JsonValue] | None = None,
    ) -> str:
        return specialist_execution_key(
            task_id=task_id,
            graph_version=graph_version,
            role=role,
            role_name=role_name,
            logical_step=logical_step,
            arguments=(
                arguments
                if arguments is not None
                else {"b": 2, "a": {"y": 2, "x": 1}}
            ),
        )

    first = key()
    reordered = specialist_execution_key(
        task_id="task-safe",
        graph_version="v4",
        role="runtime",
        role_name="local_plan",
        logical_step="runtime-1",
        arguments={"a": {"x": 1, "y": 2}, "b": 2},
    )

    assert first == reordered
    assert key(task_id="task-other") != first
    assert key(graph_version="v5") != first
    assert key(role="log") != first
    assert key(role_name="evidence_analysis") != first
    assert key(logical_step="runtime-2") != first
    assert key(arguments={"a": 1}) != first


def test_result_checksum_is_stable_and_rejects_tampering() -> None:
    result = _result()
    reordered = _result(evidence_ids=("ev-pool", "ev-pool"))

    assert specialist_result_checksum(result) == result.result_checksum
    assert reordered.result_checksum == result.result_checksum
    assert _result(duration_ms=1235).result_checksum != result.result_checksum
    with pytest.raises(ValueError, match="checksum"):
        replace(result, result_checksum="0" * 64)


@pytest.mark.parametrize(
    "field,value",
    (
        ("unresolved_questions", ("Inspect oracle output",)),
        ("completed_steps", ("ReadGroundTruth",)),
        ("model_call_count", 3),
        ("duration_ms", -1),
    ),
)
def test_result_rejects_private_or_unbounded_output(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        _result(**{field: value})
