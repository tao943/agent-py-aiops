from __future__ import annotations

from datetime import datetime, timezone

from super_ai.evaluation.artifacts import build_run_artifact
from super_ai.memory.repositories import (
    AgentToolCallAuditRecord,
    DiagnosticEvidenceRecord,
    DiagnosticTaskRecord,
)

NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


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
    assert artifact.tool_calls[0].arguments == {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
    }
