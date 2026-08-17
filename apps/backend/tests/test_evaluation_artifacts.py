from __future__ import annotations

from datetime import datetime, timezone

import pytest

from super_ai.evaluation.artifacts import build_run_artifact
from super_ai.memory.repositories import (
    AgentToolCallAuditRecord,
    DiagnosticEvidenceRecord,
    DiagnosticStepRecord,
    DiagnosticTaskRecord,
)

NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)

SNAPSHOT_READ_TOOLS = (
    "GetDatabaseMetrics",
    "GetDeploymentChanges",
    "GetGatewayMetrics",
    "GetRedisConnectionMetrics",
    "GetServiceMetrics",
    "InspectClientRetryPolicy",
    "InspectContainer",
    "InspectDatabasePool",
    "InspectGatewayErrors",
    "InspectGatewayRequestTimeline",
    "InspectHostLimits",
    "InspectHttpAttempts",
    "InspectNginx",
    "InspectPostgres",
    "InspectPostgresErrors",
    "InspectPostgresWaitGraph",
    "InspectRateLimitTimeline",
    "InspectRedis",
    "InspectRedisClientPool",
    "InspectRedisServer",
    "InspectTrafficAndDependencyHealth",
    "InspectTransactionResourceOrder",
    "ListRedisClients",
    "ProbeUpstreamHealth",
)


def _benchmark_task() -> DiagnosticTaskRecord:
    return DiagnosticTaskRecord(
        id="task-v2",
        owner_user_id="eval-user",
        status="succeeded",
        query="diagnose",
        input_payload={
            "benchmarkScenarioId": "APY-LIVE-PG-LOCK-001",
            "benchmarkMode": "live",
        },
        result_payload={},
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )


def _step(sequence: int, phase: str, payload: dict[str, object]) -> DiagnosticStepRecord:
    return DiagnosticStepRecord(
        id=f"step-{sequence}",
        owner_user_id="eval-user",
        task_id="task-v2",
        sequence=sequence,
        phase=phase,
        status="completed",
        payload=payload,
        created_at=NOW,
    )


@pytest.mark.parametrize("tool_name", SNAPSHOT_READ_TOOLS)
def test_all_snapshot_evidence_tools_are_classified_read_only(tool_name: str) -> None:
    tool_call = AgentToolCallAuditRecord(
        id=f"call-{tool_name}",
        owner_user_id="eval-user",
        chat_session_id=None,
        diagnostic_task_id="task-v2",
        tool_name=tool_name,
        status="completed",
        arguments={},
        result_summary="bounded snapshot observation",
        error_message=None,
        started_at=NOW,
        completed_at=NOW,
        duration_ms=1,
        created_at=NOW,
    )

    artifact = build_run_artifact(_benchmark_task(), (), (), (tool_call,), ())

    assert artifact.tool_calls[0].risk_tier == "L0"


def _decision_payload() -> dict[str, object]:
    return {
        "rootCauseDecision": {
            "component": "postgresql",
            "mechanism": "row_lock_blocking",
            "trigger": "concurrent transaction",
            "causalChain": ["request waits", "row lock blocks update"],
            "evidenceIds": ["ev-session", "ev-lock-graph"],
            "confidence": 0.95,
        }
    }


@pytest.mark.parametrize("validation_status", ["invalid", None])
def test_v2_artifact_rejects_invalid_or_unvalidated_decisions(
    validation_status: str | None,
) -> None:
    steps = [
        _step(1, "planner", {"workflowVersion": "evidence-driven-v2", "plan": []}),
        _step(2, "decision", _decision_payload()),
    ]
    if validation_status is not None:
        steps.append(_step(3, "decision_validation", {"status": validation_status}))

    artifact = build_run_artifact(_benchmark_task(), steps, (), (), ())

    assert artifact.decision is None


def test_v2_artifact_keeps_only_a_validated_decision() -> None:
    artifact = build_run_artifact(
        _benchmark_task(),
        (
            _step(1, "planner", {"workflowVersion": "evidence-driven-v2", "plan": []}),
            _step(2, "decision", _decision_payload()),
            _step(3, "decision_validation", {"status": "valid"}),
        ),
        (),
        (),
        (),
    )

    assert artifact.decision is not None
    assert artifact.decision.mechanism == "row_lock_blocking"


@pytest.mark.parametrize(
    "validation_origin",
    ["llm_confirmed", "deterministic_grounded_fallback"],
)
def test_v3_artifact_keeps_a_valid_decision_with_an_allowed_origin(
    validation_origin: str,
) -> None:
    artifact = build_run_artifact(
        _benchmark_task(),
        (
            _step(1, "planner", {"workflowVersion": "evidence-driven-v3", "plan": []}),
            _step(2, "decision", _decision_payload()),
            _step(
                3,
                "decision_validation",
                {
                    "status": "valid",
                    "validationOrigin": validation_origin,
                },
            ),
        ),
        (),
        (),
        (),
    )

    assert artifact.decision is not None


@pytest.mark.parametrize("validation_origin", [None, "none", "unknown_origin"])
def test_v3_artifact_rejects_a_valid_decision_without_an_allowed_origin(
    validation_origin: str | None,
) -> None:
    validation: dict[str, object] = {"status": "valid"}
    if validation_origin is not None:
        validation["validationOrigin"] = validation_origin
    artifact = build_run_artifact(
        _benchmark_task(),
        (
            _step(1, "planner", {"workflowVersion": "evidence-driven-v3", "plan": []}),
            _step(2, "decision", _decision_payload()),
            _step(3, "decision_validation", validation),
        ),
        (),
        (),
        (),
    )

    assert artifact.decision is None


def test_legacy_artifact_keeps_a_decision_without_validation() -> None:
    artifact = build_run_artifact(
        _benchmark_task(),
        (_step(1, "decision", _decision_payload()),),
        (),
        (),
        (),
    )

    assert artifact.decision is not None


def test_artifact_counts_all_six_persisted_executor_attempts() -> None:
    steps = [
        _step(
            1,
            "planner",
            {"workflowVersion": "evidence-driven-v2", "plan": [{}, {}]},
        ),
        *[_step(index + 2, "executor", {}) for index in range(6)],
    ]

    artifact = build_run_artifact(_benchmark_task(), steps, (), (), ())

    assert artifact.plan_step_count == 6


def test_run_artifact_preserves_evidence_source_and_tool_arguments() -> None:
    task = DiagnosticTaskRecord(
        id="task-1",
        owner_user_id="eval-user",
        status="succeeded",
        query="diagnose",
        input_payload={
            "benchmarkScenarioId": "APY-LIVE-PG-LOCK-001",
            "benchmarkMode": "live",
        },
        result_payload={},
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    evidence = DiagnosticEvidenceRecord(
        id="ev-cls",
        owner_user_id="eval-user",
        task_id=task.id,
        step_id="step-1",
        tool_call_id="call-1",
        kind="tool_result",
        source="SearchLog",
        summary="one matching record",
        payload={"output": {"benchmarkEvidenceId": "cls-live-request-timeout"}},
        created_at=NOW,
    )
    tool_call = AgentToolCallAuditRecord(
        id="call-1",
        owner_user_id="eval-user",
        chat_session_id=None,
        diagnostic_task_id=task.id,
        tool_name="SearchLog",
        status="completed",
        arguments={"Region": "ap-guangzhou", "TopicId": "topic-live"},
        result_summary="one matching record",
        error_message=None,
        started_at=NOW,
        completed_at=NOW,
        duration_ms=10,
        created_at=NOW,
    )

    artifact = build_run_artifact(task, (), (evidence,), (tool_call,), ())

    assert artifact.evidence[0].source == "SearchLog"
    assert artifact.evidence[0].claim_id == "cls-live-request-timeout"
    assert artifact.evidence[0].tool_call_id == "call-1"
    assert artifact.tool_calls[0].audit_id == "call-1"
    assert artifact.tool_calls[0].arguments == {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
    }


def test_run_artifact_links_observation_to_persisted_tool_call() -> None:
    evidence = DiagnosticEvidenceRecord(
        id="ev-1",
        owner_user_id="eval-user",
        task_id="task-v2",
        step_id="step-1",
        tool_call_id="call-1",
        kind="tool_result",
        source="InspectPostgresErrors",
        summary="PostgreSQL emitted SQLSTATE 40P01.",
        payload={"output": {"benchmarkEvidenceId": "postgres-40p01"}},
        created_at=NOW,
    )
    tool_call = AgentToolCallAuditRecord(
        id="call-1",
        owner_user_id="eval-user",
        chat_session_id=None,
        diagnostic_task_id="task-v2",
        tool_name="InspectPostgresErrors",
        status="completed",
        arguments={},
        result_summary=evidence.summary,
        error_message=None,
        started_at=NOW,
        completed_at=NOW,
        duration_ms=1,
        created_at=NOW,
    )
    steps = (
        _step(
            1,
            "evidence_evaluation",
            {
                "observationDecision": {
                    "purpose": "Inspect structured errors.",
                    "supports": ["postgres_deadlock"],
                    "refutes": [],
                    "summary": evidence.summary,
                    "evidenceIds": [evidence.id],
                    "causalRole": "impact",
                    "causalRoleOrigin": "plan_contract",
                    "reportedCausalRole": "mechanism",
                    "causalRoleCorrected": True,
                    "privateReasoning": "must not be extracted",
                }
            },
        ),
    )

    artifact = build_run_artifact(
        _benchmark_task(), steps, (evidence,), (tool_call,), ()
    )

    assert artifact.observation_decisions[0].evidence_ids == ("ev-1",)
    assert artifact.observation_decisions[0].causal_role == "impact"
    assert artifact.observation_decisions[0].causal_role_origin == "plan_contract"
    assert artifact.observation_decisions[0].reported_causal_role == "mechanism"
    assert artifact.observation_decisions[0].causal_role_corrected is True
    assert "privateReasoning" not in repr(artifact.observation_decisions[0])
    assert artifact.evidence[0].tool_call_id == "call-1"
    assert artifact.tool_calls[0].audit_id == "call-1"
