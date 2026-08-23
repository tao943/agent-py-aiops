"""PostgreSQL repository for durable Pending Chat Actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.models import (
    BackgroundJobModel,
    ChatSessionModel,
    PendingChatActionModel,
    utc_now,
)
from super_ai.memory.repositories import (
    JsonDict,
    PendingChatActionRecord,
    PendingChatActionStatus,
    PendingChatActionType,
)


class SQLAlchemyPendingChatActionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_or_get(
        self,
        *,
        action_id: str,
        owner_user_id: str,
        session_id: str,
        chat_run_id: str | None,
        action_type: PendingChatActionType,
        target_resource_id: str,
        public_arguments: JsonDict,
        action_fingerprint: str,
        expires_at: datetime,
    ) -> PendingChatActionRecord:
        _require_fingerprint(action_fingerprint)
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            owned_session = (
                await session.scalars(
                    select(ChatSessionModel.id).where(
                        ChatSessionModel.id == session_id,
                        ChatSessionModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
            if owned_session is None:
                raise LookupError("owned chat session not found")
            await session.execute(
                postgresql_insert(PendingChatActionModel)
                .values(
                    id=action_id,
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    chat_run_id=chat_run_id,
                    action_type=action_type,
                    target_resource_id=target_resource_id,
                    public_arguments=public_arguments,
                    action_fingerprint=action_fingerprint,
                    status="pending",
                    expires_at=expires_at,
                    confirmed_at=None,
                    execution_result_id=None,
                    background_job_id=None,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["owner_user_id", "action_fingerprint"],
                    index_where=text("status IN ('pending','confirmed')"),
                )
            )
            row = (
                await session.scalars(
                    select(PendingChatActionModel).where(
                        PendingChatActionModel.owner_user_id == owner_user_id,
                        PendingChatActionModel.action_fingerprint == action_fingerprint,
                        PendingChatActionModel.status.in_(("pending", "confirmed")),
                    )
                )
            ).one()
            return _record(row)

    async def get_owned(
        self, *, owner_user_id: str, action_id: str
    ) -> PendingChatActionRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(PendingChatActionModel).where(
                        PendingChatActionModel.id == action_id,
                        PendingChatActionModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
        return _record(row) if row is not None else None

    async def list_pending(
        self, *, owner_user_id: str, session_id: str
    ) -> list[PendingChatActionRecord]:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PendingChatActionModel)
                .where(
                    PendingChatActionModel.owner_user_id == owner_user_id,
                    PendingChatActionModel.session_id == session_id,
                    PendingChatActionModel.status == "pending",
                    PendingChatActionModel.expires_at <= now,
                )
                .values(status="expired", updated_at=now)
            )
            rows = list(
                (
                    await session.scalars(
                        select(PendingChatActionModel)
                        .where(
                            PendingChatActionModel.owner_user_id == owner_user_id,
                            PendingChatActionModel.session_id == session_id,
                            PendingChatActionModel.status.in_(("pending", "confirmed")),
                        )
                        .order_by(
                            PendingChatActionModel.created_at.asc(),
                            PendingChatActionModel.id.asc(),
                        )
                    )
                ).all()
            )
        return [_record(row) for row in rows]

    async def confirm_and_enqueue(
        self, *, owner_user_id: str, action_id: str, now: datetime
    ) -> PendingChatActionRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await _lock_owned(session, owner_user_id, action_id)
            if row is None:
                return None
            if row.status == "pending" and _ensure_utc(row.expires_at) <= now:
                row.status = "expired"
                row.updated_at = now
                await session.flush()
                return _record(row)
            if row.status in {"confirmed", "executed"}:
                return _record(row)
            if row.status != "pending":
                return _record(row)

            job_id = f"job_chat_action_{row.id}"
            job = await session.get(BackgroundJobModel, job_id)
            if job is None:
                session.add(
                    BackgroundJobModel(
                        id=job_id,
                        owner_user_id=owner_user_id,
                        kind="pending_chat_action",
                        resource_type="pending_chat_action",
                        resource_id=row.id,
                        status="queued",
                        payload={"actionId": row.id},
                        attempt=0,
                        max_attempts=3,
                        timeout_seconds=30,
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
                )
                await session.flush()
            row.status = "confirmed"
            row.confirmed_at = row.confirmed_at or now
            row.background_job_id = job_id
            row.updated_at = now
            await session.flush()
            return _record(row)

    async def cancel(
        self, *, owner_user_id: str, action_id: str, now: datetime
    ) -> PendingChatActionRecord | None:
        async with self._session_factory() as session, session.begin():
            row = await _lock_owned(session, owner_user_id, action_id)
            if row is None:
                return None
            if row.status == "pending":
                row.status = "cancelled"
                row.updated_at = now
                await session.flush()
            return _record(row)

    async def mark_executed(
        self,
        *,
        owner_user_id: str,
        action_id: str,
        execution_result_id: str,
        now: datetime,
    ) -> PendingChatActionRecord:
        async with self._session_factory() as session, session.begin():
            row = await _lock_owned(session, owner_user_id, action_id)
            if row is None:
                raise LookupError("owned pending chat action not found")
            if row.status == "executed":
                if row.execution_result_id != execution_result_id:
                    raise RuntimeError("pending action result conflict")
                return _record(row)
            if row.status != "confirmed":
                raise RuntimeError("pending action is not confirmed")
            row.status = "executed"
            row.execution_result_id = execution_result_id
            row.updated_at = now
            await session.flush()
            return _record(row)

    async def mark_manual_review(
        self, *, owner_user_id: str, action_id: str, now: datetime
    ) -> PendingChatActionRecord:
        async with self._session_factory() as session, session.begin():
            row = await _lock_owned(session, owner_user_id, action_id)
            if row is None:
                raise LookupError("owned pending chat action not found")
            if row.status == "confirmed":
                row.status = "manual_review"
                row.updated_at = now
                await session.flush()
            return _record(row)


async def _lock_owned(
    session: AsyncSession, owner_user_id: str, action_id: str
) -> PendingChatActionModel | None:
    return (
        await session.scalars(
            select(PendingChatActionModel)
            .where(
                PendingChatActionModel.id == action_id,
                PendingChatActionModel.owner_user_id == owner_user_id,
            )
            .with_for_update()
        )
    ).one_or_none()


def _record(row: PendingChatActionModel) -> PendingChatActionRecord:
    return PendingChatActionRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        session_id=row.session_id,
        chat_run_id=row.chat_run_id,
        action_type=cast(PendingChatActionType, row.action_type),
        target_resource_id=row.target_resource_id,
        public_arguments=dict(row.public_arguments),
        action_fingerprint=row.action_fingerprint,
        status=cast(PendingChatActionStatus, row.status),
        expires_at=_ensure_utc(row.expires_at),
        confirmed_at=_ensure_utc(row.confirmed_at) if row.confirmed_at else None,
        execution_result_id=row.execution_result_id,
        background_job_id=row.background_job_id,
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
