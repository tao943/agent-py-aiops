from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from super_ai.chat.aiops_bridge import IncidentSummary, PublicDiagnosticReport
from super_ai.evaluation import ArtifactEvidence, RunArtifact
from super_ai.evaluation.live.chat_entry import (
    ChatEntryLiveDiagnosticAdapter,
    ChatLiveEntryAdapter,
)
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveEvidenceContext,
    LiveFaultObservation,
    LiveScenario,
)
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


class PendingRepository:
    def __init__(self) -> None:
        self.action: PendingChatActionRecord | None = None

    async def create_or_get(self, **values: object) -> PendingChatActionRecord:
        if self.action is None:
            self.action = PendingChatActionRecord(
                id=str(values["action_id"]),
                owner_user_id=str(values["owner_user_id"]),
                session_id=str(values["session_id"]),
                chat_run_id=str(values["chat_run_id"]),
                action_type="start_diagnostic",
                target_resource_id=str(values["target_resource_id"]),
                public_arguments={"incidentId": str(values["target_resource_id"])},
                action_fingerprint=str(values["action_fingerprint"]),
                status="pending",
                expires_at=NOW + timedelta(minutes=15),
                confirmed_at=None,
                execution_result_id=None,
                background_job_id=None,
                created_at=NOW,
                updated_at=NOW,
            )
        return self.action

    async def get_owned(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord | None:
        if (
            self.action is not None
            and self.action.owner_user_id == owner_user_id
            and self.action.id == action_id
        ):
            return self.action
        return None

    async def list_pending(self, **values: object) -> list[PendingChatActionRecord]:
        del values
        return [self.action] if self.action is not None else []

    async def confirm_and_enqueue(
        self, *, owner_user_id: str, action_id: str, now: datetime
    ) -> PendingChatActionRecord | None:
        action = await self.get_owned(owner_user_id=owner_user_id, action_id=action_id)
        if action is not None and action.status == "pending":
            self.action = replace(
                action,
                status="confirmed",
                confirmed_at=now,
                background_job_id="chat-live-job",
            )
        return self.action

    async def cancel(self, **values: object) -> PendingChatActionRecord | None:
        del values
        return self.action

    async def mark_executed(
        self,
        *,
        owner_user_id: str,
        action_id: str,
        execution_result_id: str,
        now: datetime,
    ) -> PendingChatActionRecord:
        del owner_user_id, action_id, now
        assert self.action is not None
        self.action = replace(
            self.action,
            status="executed",
            execution_result_id=execution_result_id,
        )
        return self.action

    async def mark_manual_review(self, **values: object) -> PendingChatActionRecord:
        del values
        raise AssertionError("manual review is not expected")


class DiagnosticDelegate:
    def __init__(self) -> None:
        self.calls = 0

    async def diagnose(self, **values: object) -> RunArtifact:
        self.calls += 1
        scenario = values["scenario"]
        assert isinstance(scenario, LiveScenario)
        return RunArtifact(
            scenario_id=scenario.id,
            mode="live",
            completed=True,
            report_produced=True,
            decision=None,
            evidence=(
                ArtifactEvidence("evidence_live_1", "claim-1", True),
                ArtifactEvidence("evidence_live_2", "claim-2", True),
            ),
            hypothesis_states=(),
            observation_decisions=(),
            tool_calls=(),
            plan_step_count=1,
            duration_ms=10,
            safety_events=(),
            diagnostic_task_id="diagnostic_live_1",
        )


@pytest.mark.asyncio
async def test_live_diagnostic_adapter_delegates_to_aiops_and_keeps_scores_separate() -> None:
    repository = PendingRepository()
    delegate = DiagnosticDelegate()
    adapter = ChatEntryLiveDiagnosticAdapter(
        owner_user_id="owner-live",
        pending_repository=repository,
        report_bridge=Bridge(),
        diagnostic_delegate=delegate,
    )
    scenario = LiveScenario(
        id="APY-LIVE-PG-LOCK-001",
        title="lock wait",
        symptom_family="database_lock",
        difficulty="medium",
        modes=("live",),
        driver="postgres_lock_wait",
        alert={"alertname": "PostgresLockWait", "service": "order-service"},
        hypotheses=(),
    )

    artifact = await adapter.diagnose(
        run_id="chat-live-run-1",
        scenario=scenario,
        observation=LiveFaultObservation(
            scenario_id=scenario.id,
            checks=(LiveCheck("fault_present", True),),
        ),
        evidence_context=LiveEvidenceContext.local(incident_id="incident-live-1"),
    )

    assert delegate.calls == 1
    assert artifact.diagnostic_task_id == "diagnostic_live_1"
    assert adapter.conversation_metrics()["confirmationAccuracy"] == 1.0
    assert "total" not in adapter.conversation_metrics()
