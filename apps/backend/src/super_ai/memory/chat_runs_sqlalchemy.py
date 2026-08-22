"""PostgreSQL repositories for durable Conversation Agent execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.models import (
    BackgroundJobModel,
    ChatAgentRunModel,
    ChatMessageModel,
    ChatRunEventModel,
    ChatRunToolExecutionModel,
    ChatSessionModel,
    RecoveryApprovalRequestModel,
    utc_now,
)
from super_ai.memory.repositories import (
    ChatRunCreateResult,
    ChatRunEventRecord,
    ChatRunIdempotencyConflict,
    ChatRunRecord,
    ChatRunStatus,
    ChatToolExecutionClaim,
    ChatToolExecutionClaimResult,
    ChatToolExecutionRecord,
    ChatToolExecutionStatus,
    JsonDict,
    RecoveryApprovalRequestRecord,
)


class SQLAlchemyChatRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_or_get(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        client_request_id: str,
        request_fingerprint: str,
        content: str,
        metadata: JsonDict,
    ) -> ChatRunCreateResult:
        _require_fingerprint(request_fingerprint)
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            parent = (
                await session.scalars(
                    select(ChatSessionModel)
                    .where(
                        ChatSessionModel.id == session_id,
                        ChatSessionModel.owner_user_id == owner_user_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if parent is None:
                raise LookupError("owned chat session not found")
            existing = await _find_run_by_request(
                session,
                owner_user_id=owner_user_id,
                session_id=session_id,
                client_request_id=client_request_id,
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ChatRunIdempotencyConflict(client_request_id)
                return ChatRunCreateResult(
                    run=_chat_run_record(existing),
                    background_job_id=existing.background_job_id,
                    reused=True,
                )

            run_id = f"chat_run_{uuid4().hex}"
            message_id = f"message_{uuid4().hex}"
            job_id = f"job_{uuid4().hex}"
            safe_metadata = {key: value for key, value in metadata.items() if key != "reasoning"}
            message_row = ChatMessageModel(
                id=message_id,
                owner_user_id=owner_user_id,
                session_id=session_id,
                role="user",
                content=content,
                metadata_json=safe_metadata,
                created_at=now,
            )
            job_row = BackgroundJobModel(
                id=job_id,
                owner_user_id=owner_user_id,
                kind="chat_agent_run",
                resource_type="chat_agent_run",
                resource_id=run_id,
                status="queued",
                payload={"runId": run_id, "sessionId": session_id},
                attempt=0,
                max_attempts=3,
                timeout_seconds=900,
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                cancel_requested_at=None,
                retry_of_job_id=None,
                error_message=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
            session.add_all((message_row, job_row))
            await session.flush((message_row, job_row))
            row = ChatAgentRunModel(
                id=run_id,
                owner_user_id=owner_user_id,
                chat_session_id=session_id,
                client_request_id=client_request_id,
                request_fingerprint=request_fingerprint,
                user_message_id=message_id,
                assistant_message_id=None,
                background_job_id=job_id,
                status="queued",
                attempt_count=0,
                last_event_sequence=0,
                error_code=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
            session.add(row)
            parent.updated_at = now
            await session.flush()
            return ChatRunCreateResult(
                run=_chat_run_record(row), background_job_id=job_id, reused=False
            )

    async def get_owned(
        self, *, owner_user_id: str, session_id: str, run_id: str
    ) -> ChatRunRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(ChatAgentRunModel).where(
                        ChatAgentRunModel.id == run_id,
                        ChatAgentRunModel.owner_user_id == owner_user_id,
                        ChatAgentRunModel.chat_session_id == session_id,
                    )
                )
            ).one_or_none()
        return _chat_run_record(row) if row is not None else None

    async def get_active(self, *, owner_user_id: str, session_id: str) -> ChatRunRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(ChatAgentRunModel)
                    .where(
                        ChatAgentRunModel.owner_user_id == owner_user_id,
                        ChatAgentRunModel.chat_session_id == session_id,
                        ChatAgentRunModel.status.in_(("queued", "running")),
                    )
                    .order_by(ChatAgentRunModel.updated_at.desc())
                    .limit(1)
                )
            ).first()
        return _chat_run_record(row) if row is not None else None

    async def claim_attempt(self, *, owner_user_id: str, run_id: str) -> ChatRunRecord | None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            row = await _lock_owned_run(session, owner_user_id, run_id)
            if row is None:
                return None
            if row.status in {"succeeded", "failed", "cancelled"}:
                return _chat_run_record(row)
            row.status = "running"
            row.attempt_count += 1
            row.started_at = row.started_at or now
            row.updated_at = now
            await session.flush()
            return _chat_run_record(row)

    async def append_event(
        self,
        *,
        owner_user_id: str,
        run_id: str,
        event_type: str,
        public_payload: JsonDict,
    ) -> ChatRunEventRecord:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            run = await _lock_owned_run(session, owner_user_id, run_id)
            if run is None:
                raise LookupError("owned chat run not found")
            run.last_event_sequence += 1
            run.updated_at = now
            row = ChatRunEventModel(
                run_id=run_id,
                owner_user_id=owner_user_id,
                sequence=run.last_event_sequence,
                event_type=event_type,
                public_payload=public_payload,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _chat_run_event_record(row)

    async def list_events(
        self, *, owner_user_id: str, run_id: str, after_sequence: int = 0
    ) -> list[ChatRunEventRecord]:
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ChatRunEventModel)
                        .where(
                            ChatRunEventModel.owner_user_id == owner_user_id,
                            ChatRunEventModel.run_id == run_id,
                            ChatRunEventModel.sequence > max(0, after_sequence),
                        )
                        .order_by(ChatRunEventModel.sequence.asc())
                    )
                ).all()
            )
        return [_chat_run_event_record(row) for row in rows]

    async def complete(
        self, *, owner_user_id: str, run_id: str, assistant_message_id: str
    ) -> ChatRunRecord:
        return await self._finish(
            owner_user_id=owner_user_id,
            run_id=run_id,
            status="succeeded",
            assistant_message_id=assistant_message_id,
            error_code=None,
        )

    async def fail(self, *, owner_user_id: str, run_id: str, error_code: str) -> ChatRunRecord:
        return await self._finish(
            owner_user_id=owner_user_id,
            run_id=run_id,
            status="failed",
            assistant_message_id=None,
            error_code=error_code,
        )

    async def _finish(
        self,
        *,
        owner_user_id: str,
        run_id: str,
        status: ChatRunStatus,
        assistant_message_id: str | None,
        error_code: str | None,
    ) -> ChatRunRecord:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            row = await _lock_owned_run(session, owner_user_id, run_id)
            if row is None:
                raise LookupError("owned chat run not found")
            row.status = status
            row.assistant_message_id = assistant_message_id or row.assistant_message_id
            row.error_code = error_code
            row.completed_at = now
            row.updated_at = now
            await session.flush()
            return _chat_run_record(row)


class SQLAlchemyChatToolExecutionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(self, claim: ChatToolExecutionClaim) -> ChatToolExecutionClaimResult:
        now = utc_now()
        _require_fingerprint(claim.tool_call_key)
        _require_fingerprint(claim.arguments_fingerprint)
        async with self._session_factory() as session, session.begin():
            inserted = (
                await session.execute(
                    postgresql_insert(ChatRunToolExecutionModel)
                    .values(
                        tool_call_key=claim.tool_call_key,
                        owner_user_id=claim.owner_user_id,
                        chat_run_id=claim.chat_run_id,
                        logical_step=claim.logical_step,
                        tool_name=claim.tool_name,
                        arguments_fingerprint=claim.arguments_fingerprint,
                        status="running",
                        attempt_count=1,
                        lease_owner=claim.lease_owner,
                        lease_expires_at=claim.lease_expires_at,
                        side_effecting=claim.side_effecting,
                        outcome_known=False,
                        public_result={},
                        safe_error_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["tool_call_key"])
                    .returning(ChatRunToolExecutionModel.tool_call_key)
                )
            ).scalar_one_or_none()
            row = await session.get(
                ChatRunToolExecutionModel,
                claim.tool_call_key,
                with_for_update=True,
            )
            if row is None:
                raise RuntimeError("tool execution claim disappeared")
            if inserted is not None:
                action = "acquired"
            elif row.status == "completed":
                action = "reuse"
            elif (
                row.status in {"running", "uncertain"}
                and row.lease_expires_at is not None
                and _ensure_utc(row.lease_expires_at) > now
            ):
                action = "wait"
            elif row.side_effecting and not row.outcome_known:
                action = "manual_review"
            else:
                row.status = "running"
                row.attempt_count += 1
                row.lease_owner = claim.lease_owner
                row.lease_expires_at = claim.lease_expires_at
                row.safe_error_code = None
                row.updated_at = now
                action = "acquired"
            await session.flush()
            return ChatToolExecutionClaimResult(
                action=action,
                execution=_chat_tool_execution_record(row),
            )

    async def complete(
        self, *, tool_call_key: str, lease_owner: str, public_result: JsonDict
    ) -> ChatToolExecutionRecord:
        return await self._finish(
            tool_call_key=tool_call_key,
            lease_owner=lease_owner,
            status="completed",
            public_result=public_result,
            safe_error_code=None,
            outcome_known=True,
        )

    async def fail(
        self,
        *,
        tool_call_key: str,
        lease_owner: str,
        safe_error_code: str,
        retryable: bool,
    ) -> ChatToolExecutionRecord:
        return await self._finish(
            tool_call_key=tool_call_key,
            lease_owner=lease_owner,
            status="failed",
            public_result={},
            safe_error_code=safe_error_code,
            outcome_known=True,
        )

    async def mark_uncertain(
        self, *, tool_call_key: str, lease_owner: str, safe_error_code: str
    ) -> ChatToolExecutionRecord:
        return await self._finish(
            tool_call_key=tool_call_key,
            lease_owner=lease_owner,
            status="uncertain",
            public_result={},
            safe_error_code=safe_error_code,
            outcome_known=False,
        )

    async def _finish(
        self,
        *,
        tool_call_key: str,
        lease_owner: str,
        status: ChatToolExecutionStatus,
        public_result: JsonDict,
        safe_error_code: str | None,
        outcome_known: bool,
    ) -> ChatToolExecutionRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.get(ChatRunToolExecutionModel, tool_call_key, with_for_update=True)
            if row is None or row.lease_owner != lease_owner:
                raise LookupError("owned tool execution lease not found")
            row.status = status
            row.public_result = public_result
            row.safe_error_code = safe_error_code
            row.outcome_known = outcome_known
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utc_now()
            await session.flush()
            return _chat_tool_execution_record(row)


class SQLAlchemyRecoveryApprovalRequestRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_or_get(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
        proposal_fingerprint: str,
        request_reason: str,
        chat_run_id: str | None,
    ) -> RecoveryApprovalRequestRecord:
        _require_fingerprint(proposal_fingerprint)
        now = utc_now()
        approval_id = f"approval_{uuid4().hex}"
        async with self._session_factory() as session, session.begin():
            inserted = (
                await session.execute(
                    postgresql_insert(RecoveryApprovalRequestModel)
                    .values(
                        id=approval_id,
                        owner_user_id=owner_user_id,
                        diagnostic_task_id=diagnostic_task_id,
                        proposal_fingerprint=proposal_fingerprint,
                        request_reason=request_reason[:1000],
                        chat_run_id=chat_run_id,
                        status="pending",
                        execution_permitted=False,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "owner_user_id",
                            "diagnostic_task_id",
                            "proposal_fingerprint",
                        ]
                    )
                    .returning(RecoveryApprovalRequestModel.id)
                )
            ).scalar_one_or_none()
            row = (
                await session.scalars(
                    select(RecoveryApprovalRequestModel).where(
                        RecoveryApprovalRequestModel.owner_user_id == owner_user_id,
                        RecoveryApprovalRequestModel.diagnostic_task_id == diagnostic_task_id,
                        RecoveryApprovalRequestModel.proposal_fingerprint == proposal_fingerprint,
                    )
                )
            ).one()
        return _recovery_approval_record(row, reused=inserted is None)


async def _find_run_by_request(
    session: AsyncSession,
    *,
    owner_user_id: str,
    session_id: str,
    client_request_id: str,
) -> ChatAgentRunModel | None:
    return (
        await session.scalars(
            select(ChatAgentRunModel).where(
                ChatAgentRunModel.owner_user_id == owner_user_id,
                ChatAgentRunModel.chat_session_id == session_id,
                ChatAgentRunModel.client_request_id == client_request_id,
            )
        )
    ).one_or_none()


async def _lock_owned_run(
    session: AsyncSession, owner_user_id: str, run_id: str
) -> ChatAgentRunModel | None:
    return (
        await session.scalars(
            select(ChatAgentRunModel)
            .where(
                ChatAgentRunModel.id == run_id,
                ChatAgentRunModel.owner_user_id == owner_user_id,
            )
            .with_for_update()
        )
    ).one_or_none()


def _chat_run_record(row: ChatAgentRunModel) -> ChatRunRecord:
    return ChatRunRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        session_id=row.chat_session_id,
        client_request_id=row.client_request_id,
        request_fingerprint=row.request_fingerprint,
        user_message_id=row.user_message_id,
        assistant_message_id=row.assistant_message_id,
        background_job_id=row.background_job_id,
        status=cast(ChatRunStatus, row.status),
        attempt_count=row.attempt_count,
        last_event_sequence=row.last_event_sequence,
        error_code=row.error_code,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
        started_at=_ensure_utc(row.started_at) if row.started_at else None,
        completed_at=_ensure_utc(row.completed_at) if row.completed_at else None,
    )


def _chat_run_event_record(row: ChatRunEventModel) -> ChatRunEventRecord:
    return ChatRunEventRecord(
        run_id=row.run_id,
        owner_user_id=row.owner_user_id,
        sequence=row.sequence,
        event_type=row.event_type,
        public_payload=dict(row.public_payload),
        created_at=_ensure_utc(row.created_at),
    )


def _chat_tool_execution_record(
    row: ChatRunToolExecutionModel,
) -> ChatToolExecutionRecord:
    return ChatToolExecutionRecord(
        tool_call_key=row.tool_call_key,
        owner_user_id=row.owner_user_id,
        chat_run_id=row.chat_run_id,
        logical_step=row.logical_step,
        tool_name=row.tool_name,
        arguments_fingerprint=row.arguments_fingerprint,
        status=cast(ChatToolExecutionStatus, row.status),
        attempt_count=row.attempt_count,
        lease_owner=row.lease_owner,
        lease_expires_at=(_ensure_utc(row.lease_expires_at) if row.lease_expires_at else None),
        side_effecting=row.side_effecting,
        outcome_known=row.outcome_known,
        public_result=dict(row.public_result),
        safe_error_code=row.safe_error_code,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


def _recovery_approval_record(
    row: RecoveryApprovalRequestModel,
    *,
    reused: bool,
) -> RecoveryApprovalRequestRecord:
    return RecoveryApprovalRequestRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        diagnostic_task_id=row.diagnostic_task_id,
        proposal_fingerprint=row.proposal_fingerprint,
        request_reason=row.request_reason,
        chat_run_id=row.chat_run_id,
        status="pending",
        execution_permitted=False,
        reused=reused,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


def _require_fingerprint(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("fingerprint must be 64 lowercase hexadecimal characters")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
