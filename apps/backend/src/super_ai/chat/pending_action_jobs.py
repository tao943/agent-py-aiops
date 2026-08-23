"""Leased worker handler for confirmed Pending Chat Actions."""

from __future__ import annotations

from typing import Protocol

from super_ai.alert_ingestion.repositories import DiagnosticScheduleResult
from super_ai.chat.aiops_bridge import RecoveryApprovalRequest
from super_ai.jobs import BackgroundJobContext, TerminalBackgroundJobError
from super_ai.memory.models import utc_now
from super_ai.memory.repositories import MemoryRepositories


class PendingActionBridge(Protocol):
    async def start_incident_diagnostic(
        self, *, owner_user_id: str, incident_id: str, note: str | None
    ) -> DiagnosticScheduleResult: ...

    async def create_recovery_approval_request(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        reason: str,
        chat_run_id: str | None,
    ) -> RecoveryApprovalRequest: ...


class PendingChatActionJobHandler:
    def __init__(
        self, *, repositories: MemoryRepositories, bridge: PendingActionBridge
    ) -> None:
        self._repositories = repositories
        self._bridge = bridge

    async def __call__(self, context: BackgroundJobContext) -> None:
        repository = self._repositories.pending_chat_actions
        if repository is None:
            raise TerminalBackgroundJobError("PENDING_CHAT_ACTION_REPOSITORY_UNAVAILABLE")
        owner_user_id = context.job.owner_user_id
        action_id = context.job.resource_id
        action = await repository.get_owned(
            owner_user_id=owner_user_id,
            action_id=action_id,
        )
        if action is None:
            raise TerminalBackgroundJobError("PENDING_CHAT_ACTION_NOT_FOUND")
        if action.status == "executed":
            return
        if action.status == "manual_review":
            raise TerminalBackgroundJobError("PENDING_CHAT_ACTION_MANUAL_REVIEW")
        if action.status != "confirmed":
            raise TerminalBackgroundJobError("PENDING_CHAT_ACTION_NOT_CONFIRMED")

        try:
            if action.action_type == "start_diagnostic":
                note_value = action.public_arguments.get("note")
                result = await self._bridge.start_incident_diagnostic(
                    owner_user_id=owner_user_id,
                    incident_id=action.target_resource_id,
                    note=note_value if isinstance(note_value, str) else None,
                )
                execution_result_id = result.diagnostic_task_id
            else:
                reason_value = action.public_arguments.get("reason")
                if not isinstance(reason_value, str) or not reason_value:
                    raise TerminalBackgroundJobError(
                        "PENDING_CHAT_ACTION_ARGUMENTS_INVALID"
                    )
                result = await self._bridge.create_recovery_approval_request(
                    owner_user_id=owner_user_id,
                    task_id=action.target_resource_id,
                    reason=reason_value,
                    chat_run_id=action.chat_run_id,
                )
                execution_result_id = result.id
            await repository.mark_executed(
                owner_user_id=owner_user_id,
                action_id=action_id,
                execution_result_id=execution_result_id,
                now=utc_now(),
            )
        except TerminalBackgroundJobError:
            raise
        except Exception as exc:
            if context.job.attempt >= context.job.max_attempts:
                await repository.mark_manual_review(
                    owner_user_id=owner_user_id,
                    action_id=action_id,
                    now=utc_now(),
                )
                raise TerminalBackgroundJobError(
                    "PENDING_CHAT_ACTION_OUTCOME_UNCERTAIN"
                ) from exc
            raise
