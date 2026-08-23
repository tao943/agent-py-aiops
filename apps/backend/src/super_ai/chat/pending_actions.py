"""Application service for frozen, owner-scoped Pending Chat Actions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from super_ai.memory.models import utc_now
from super_ai.memory.repositories import (
    JsonDict,
    PendingChatActionRecord,
    PendingChatActionRepository,
    PendingChatActionType,
)


class PendingActionNotFound(LookupError):
    """The action is absent or belongs to another owner."""


class PendingChatActionService:
    def __init__(self, repository: PendingChatActionRepository) -> None:
        self._repository = repository

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
        arguments: JsonDict = {"incidentId": incident_id}
        bounded_note = (note or "").strip()[:1000]
        if bounded_note:
            arguments["note"] = bounded_note
        return await self._preview(
            owner_user_id=owner_user_id,
            session_id=session_id,
            chat_run_id=chat_run_id,
            action_type="start_diagnostic",
            target_resource_id=incident_id,
            public_arguments=arguments,
            expires_at=expires_at,
        )

    async def preview_recovery_approval(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        diagnostic_task_id: str,
        reason: str,
        chat_run_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> PendingChatActionRecord:
        bounded_reason = reason.strip()[:1000]
        if not bounded_reason:
            raise ValueError("Recovery approval reason is required.")
        return await self._preview(
            owner_user_id=owner_user_id,
            session_id=session_id,
            chat_run_id=chat_run_id,
            action_type="create_recovery_approval",
            target_resource_id=diagnostic_task_id,
            public_arguments={
                "diagnosticTaskId": diagnostic_task_id,
                "reason": bounded_reason,
            },
            expires_at=expires_at,
        )

    async def confirm(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord:
        action = await self._repository.confirm_and_enqueue(
            owner_user_id=owner_user_id,
            action_id=action_id,
            now=utc_now(),
        )
        if action is None:
            raise PendingActionNotFound(action_id)
        return action

    async def cancel(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord:
        action = await self._repository.cancel(
            owner_user_id=owner_user_id,
            action_id=action_id,
            now=utc_now(),
        )
        if action is None:
            raise PendingActionNotFound(action_id)
        return action

    async def list_pending(
        self, *, owner_user_id: str, session_id: str
    ) -> list[PendingChatActionRecord]:
        return await self._repository.list_pending(
            owner_user_id=owner_user_id,
            session_id=session_id,
        )

    async def _preview(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        chat_run_id: str | None,
        action_type: PendingChatActionType,
        target_resource_id: str,
        public_arguments: JsonDict,
        expires_at: datetime | None,
    ) -> PendingChatActionRecord:
        fingerprint = pending_action_fingerprint(
            action_type=action_type,
            target_resource_id=target_resource_id,
            public_arguments=public_arguments,
        )
        return await self._repository.create_or_get(
            action_id=f"chat_action_{uuid4().hex}",
            owner_user_id=owner_user_id,
            session_id=session_id,
            chat_run_id=chat_run_id,
            action_type=action_type,
            target_resource_id=target_resource_id,
            public_arguments=public_arguments,
            action_fingerprint=fingerprint,
            expires_at=expires_at or (utc_now() + timedelta(minutes=15)),
        )


def pending_action_fingerprint(
    *,
    action_type: PendingChatActionType,
    target_resource_id: str,
    public_arguments: JsonDict,
) -> str:
    canonical = json.dumps(
        {
            "actionType": action_type,
            "targetResourceId": target_resource_id,
            "publicArguments": public_arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
