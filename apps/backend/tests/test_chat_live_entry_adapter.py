from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from super_ai.chat.aiops_bridge import IncidentSummary, PublicDiagnosticReport
from super_ai.evaluation.live.chat_entry import ChatLiveEntryAdapter
from super_ai.memory.repositories import PendingChatActionRecord

NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class PendingActions:
    def __init__(self) -> None:
        self.action: PendingChatActionRecord | None = None

    async def preview_start(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        incident_id: str,
        chat_run_id: str | None = None,
        note: str | None = None,
        expires_at: datetime | None = None,
    ) -> PendingChatActionRecord:
        del note, expires_at
        if self.action is None:
            self.action = PendingChatActionRecord(
                id="chat_action_live_1",
                owner_user_id=owner_user_id,
                session_id=session_id,
                chat_run_id=chat_run_id,
                action_type="start_diagnostic",
                target_resource_id=incident_id,
                public_arguments={"incidentId": incident_id},
                action_fingerprint="a" * 64,
                status="pending",
                expires_at=NOW + timedelta(minutes=15),
                confirmed_at=None,
                execution_result_id=None,
                background_job_id=None,
                created_at=NOW,
                updated_at=NOW,
            )
        return self.action

    async def confirm(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord:
        assert self.action is not None
        assert self.action.owner_user_id == owner_user_id
        assert self.action.id == action_id
        if self.action.status == "pending":
            self.action = replace(
                self.action,
                status="confirmed",
                confirmed_at=NOW + timedelta(seconds=2),
                background_job_id="chat-job-1",
            )
        return self.action


class ExecutionAwaiter:
    def __init__(self, pending: PendingActions) -> None:
        self.pending = pending
        self.diagnostic_create_count = 0

    async def await_executed(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord:
        action = self.pending.action
        assert action is not None
        assert action.owner_user_id == owner_user_id
        assert action.id == action_id
        if action.status != "executed":
            self.diagnostic_create_count += 1
            action = replace(
                action,
                status="executed",
                execution_result_id="diagnostic_live_1",
            )
            self.pending.action = action
        return action


class Bridge:
    async def get_incident(
        self, *, owner_user_id: str, incident_id: str
    ) -> IncidentSummary:
        assert owner_user_id == "owner-live"
        assert incident_id == "incident-live-1"
        return IncidentSummary(
            id=incident_id,
            status="active",
            alert_name="PostgresLockWait",
            service="order-service",
            severity="warning",
            last_seen_at=NOW,
            diagnostic_task_id=None,
        )

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport:
        assert owner_user_id == "owner-live"
        assert task_id == "diagnostic_live_1"
        return PublicDiagnosticReport(
            id="report_live_1",
            task_id=task_id,
            title="PostgreSQL lock wait",
            content="Verified public report.",
            root_cause={"component": "postgresql", "mechanism": "lock_wait"},
            recovery_mode="manual_review",
            execution_permitted=False,
            human_approval_required=True,
            validator_status="deterministic_grounded_fallback",
            evidence_ids=("evidence_live_1", "evidence_live_2"),
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_chat_live_entry_requires_confirmation_and_reuses_diagnostic() -> None:
    pending = PendingActions()
    awaiter = ExecutionAwaiter(pending)
    adapter = ChatLiveEntryAdapter(
        pending_actions=pending,
        bridge=Bridge(),
        execution_awaiter=awaiter,
    )

    action = await adapter.request_start_from_incident(
        owner_user_id="owner-live",
        incident_id="incident-live-1",
        client_request_id="live-run-1",
    )

    assert action.status == "pending"
    assert action.execution_result_id is None
    assert awaiter.diagnostic_create_count == 0

    first = await adapter.confirm_start(
        owner_user_id="owner-live", action_id=action.id
    )
    second = await adapter.confirm_start(
        owner_user_id="owner-live", action_id=action.id
    )

    assert first.diagnostic_task_id == "diagnostic_live_1"
    assert second.diagnostic_task_id == first.diagnostic_task_id
    assert first.background_job_id == "chat-job-1"
    assert first.confirmation_latency_ms == 2000
    assert first.metrics.model_call_count == 0
    assert first.metrics.tool_call_count == 2
    assert awaiter.diagnostic_create_count == 1

    final_report = await adapter.read_final_report(
        owner_user_id="owner-live",
        diagnostic_task_id=first.diagnostic_task_id,
    )
    assert final_report.report_id == "report_live_1"
    assert final_report.diagnostic_task_id == first.diagnostic_task_id
    assert final_report.evidence_ids == ("evidence_live_1", "evidence_live_2")


def test_chat_live_entry_never_exposes_cls_tools() -> None:
    adapter = ChatLiveEntryAdapter(
        pending_actions=PendingActions(),
        bridge=Bridge(),
        execution_awaiter=ExecutionAwaiter(PendingActions()),
    )

    assert set(adapter.exposed_tools).isdisjoint(
        {"SearchLog", "search_log", "search_cls_logs", "cls_search"}
    )
    assert "start_incident_diagnostic" in adapter.exposed_tools
