"""SQLAlchemy memory repository implementations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar, cast
from uuid import uuid4

from sqlalchemy import Select, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.models import (
    AgentToolCallAuditModel,
    ChatMessageModel,
    ChatSessionModel,
    DiagnosticCaseModel,
    DiagnosticEvidenceModel,
    DiagnosticReportModel,
    DiagnosticStepModel,
    DiagnosticTaskModel,
    DocumentIndexTaskModel,
    EvaluationResultModel,
    EvaluationRunModel,
    GraphCheckpointModel,
    KnowledgeDocumentModel,
    ReportEvidenceLinkModel,
    ToolCallAuditModel,
    UserChatConfigurationModel,
    UserChatPromptModel,
    UserChatSkillModel,
    utc_now,
)
from super_ai.memory.repositories import (
    EVALUATION_FAILURE_CATEGORIES,
    AgentToolCallAuditRecord,
    ChatMessageRecord,
    ChatSessionRecord,
    DiagnosticCaseRecord,
    DiagnosticEvidenceRecord,
    DiagnosticReportRecord,
    DiagnosticStepRecord,
    DiagnosticTaskRecord,
    DocumentIndexTaskRecord,
    EvaluationFailureStatus,
    EvaluationResultRecord,
    EvaluationRunRecord,
    GraphCheckpointRecord,
    JsonDict,
    KnowledgeDocumentRecord,
    MemoryRepositories,
    ReportEvidenceLinkRecord,
    TenantScopeError,
    TimeRangeFilter,
    ToolCallAuditRecord,
    UserChatConfigurationRecord,
    UserChatPromptRecord,
    UserChatSkillRecord,
)

ModelT = TypeVar("ModelT")
_UNSET = object()


class SQLAlchemyChatMemoryRepository:
    """SQLAlchemy implementation of chat memory persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_session(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        title: str | None = None,
        created_at: datetime | None = None,
    ) -> ChatSessionRecord:
        timestamp = created_at or utc_now()
        row = ChatSessionModel(
            id=session_id,
            owner_user_id=owner_user_id,
            title=title,
            created_at=timestamp,
            updated_at=timestamp,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        return _chat_session_record(row)

    async def get_session(
        self,
        *,
        owner_user_id: str,
        session_id: str,
    ) -> ChatSessionRecord | None:
        stmt = select(ChatSessionModel).where(
            ChatSessionModel.id == session_id,
            ChatSessionModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _chat_session_record(row) if row is not None else None

    async def update_session_title(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        title: str,
        updated_at: datetime | None = None,
    ) -> ChatSessionRecord | None:
        timestamp = updated_at or utc_now()
        async with self._session_factory() as session:
            row = await _find_chat_session(session, owner_user_id, session_id)
            if row is None:
                return None
            row.title = title
            row.updated_at = timestamp
            await session.commit()
        return _chat_session_record(row)

    async def update_memory_state(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        memory_mode: str | None = None,
        memory_summary: str | None = None,
        compacted_message_count: int | None = None,
        context_tokens: int | None = None,
        last_compacted_at: datetime | None = None,
        clear_compaction: bool = False,
        updated_at: datetime | None = None,
    ) -> ChatSessionRecord | None:
        timestamp = updated_at or utc_now()
        async with self._session_factory() as session:
            row = await _find_chat_session(session, owner_user_id, session_id)
            if row is None:
                return None
            if memory_mode is not None:
                row.memory_mode = memory_mode
            if clear_compaction:
                row.memory_summary = None
                row.compacted_message_count = 0
                row.context_tokens = 0
                row.last_compacted_at = None
            else:
                if memory_summary is not None:
                    row.memory_summary = memory_summary
                if compacted_message_count is not None:
                    row.compacted_message_count = compacted_message_count
                if context_tokens is not None:
                    row.context_tokens = context_tokens
                if last_compacted_at is not None:
                    row.last_compacted_at = last_compacted_at
            row.updated_at = timestamp
            await session.commit()
        return _chat_session_record(row)

    async def list_sessions(
        self,
        *,
        owner_user_id: str,
        time_range: TimeRangeFilter | None = None,
    ) -> list[ChatSessionRecord]:
        stmt = select(ChatSessionModel).where(ChatSessionModel.owner_user_id == owner_user_id)
        stmt = _apply_time_range(stmt, ChatSessionModel.created_at, time_range)
        stmt = stmt.order_by(ChatSessionModel.updated_at.desc(), ChatSessionModel.id.asc())
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_chat_session_record(row) for row in rows]

    async def append_message(
        self,
        *,
        owner_user_id: str,
        message_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: JsonDict | None = None,
        created_at: datetime | None = None,
    ) -> ChatMessageRecord:
        timestamp = created_at or utc_now()
        row = ChatMessageModel(
            id=message_id,
            owner_user_id=owner_user_id,
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
            created_at=timestamp,
        )
        async with self._session_factory() as session:
            parent = await _require_chat_session(session, owner_user_id, session_id)
            parent.updated_at = timestamp
            session.add(row)
            await session.commit()
        return _chat_message_record(row)

    async def clear_messages(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        updated_at: datetime | None = None,
    ) -> int:
        timestamp = updated_at or utc_now()
        async with self._session_factory() as session:
            parent = await _find_chat_session(session, owner_user_id, session_id)
            if parent is None:
                return 0
            message_ids = list(
                (
                    await session.scalars(
                        select(ChatMessageModel.id).where(
                            ChatMessageModel.owner_user_id == owner_user_id,
                            ChatMessageModel.session_id == session_id,
                        )
                    )
                ).all()
            )
            await session.execute(
                sql_delete(ChatMessageModel).where(
                    ChatMessageModel.owner_user_id == owner_user_id,
                    ChatMessageModel.session_id == session_id,
                )
            )
            parent.updated_at = timestamp
            parent.memory_summary = None
            parent.compacted_message_count = 0
            parent.context_tokens = 0
            parent.last_compacted_at = None
            await session.commit()
        return len(message_ids)

    async def delete_session(
        self,
        *,
        owner_user_id: str,
        session_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            row = await _find_chat_session(session, owner_user_id, session_id)
            if row is None:
                return False
            await session.execute(
                sql_delete(ChatMessageModel).where(
                    ChatMessageModel.owner_user_id == owner_user_id,
                    ChatMessageModel.session_id == session_id,
                )
            )
            await session.delete(row)
            await session.commit()
        return True

    async def list_messages(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        time_range: TimeRangeFilter | None = None,
    ) -> list[ChatMessageRecord]:
        stmt = select(ChatMessageModel).where(
            ChatMessageModel.owner_user_id == owner_user_id,
            ChatMessageModel.session_id == session_id,
        )
        stmt = _apply_time_range(stmt, ChatMessageModel.created_at, time_range)
        stmt = stmt.order_by(ChatMessageModel.created_at.asc(), ChatMessageModel.id.asc())
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_chat_message_record(row) for row in rows]

    async def get_message(
        self,
        *,
        owner_user_id: str,
        message_id: str,
    ) -> ChatMessageRecord | None:
        stmt = select(ChatMessageModel).where(
            ChatMessageModel.id == message_id,
            ChatMessageModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _chat_message_record(row) if row is not None else None


class SQLAlchemyUserChatConfigurationRepository:
    """SQLAlchemy owner-scoped chat assembly persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_or_create(
        self, *, owner_user_id: str, system_prompt_id: str, skill_ids: list[str]
    ) -> UserChatConfigurationRecord:
        async with self._session_factory() as session:
            row = await session.get(UserChatConfigurationModel, owner_user_id)
            if row is None:
                timestamp = utc_now()
                row = UserChatConfigurationModel(
                    owner_user_id=owner_user_id,
                    system_prompt_id=system_prompt_id,
                    skill_ids=list(skill_ids),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(row)
                await session.commit()
        return _user_chat_configuration_record(row)

    async def update(
        self, *, owner_user_id: str, system_prompt_id: str, skill_ids: list[str]
    ) -> UserChatConfigurationRecord:
        async with self._session_factory() as session:
            row = await session.get(UserChatConfigurationModel, owner_user_id)
            timestamp = utc_now()
            if row is None:
                row = UserChatConfigurationModel(
                    owner_user_id=owner_user_id,
                    system_prompt_id=system_prompt_id,
                    skill_ids=list(skill_ids),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(row)
            else:
                row.system_prompt_id = system_prompt_id
                row.skill_ids = list(skill_ids)
                row.updated_at = timestamp
            await session.commit()
        return _user_chat_configuration_record(row)


class SQLAlchemyUserChatPromptRepository:
    """SQLAlchemy owner-scoped editable prompt persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_default(
        self,
        *,
        owner_user_id: str,
        label: str,
        content: str,
    ) -> UserChatPromptRecord:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(UserChatPromptModel).where(
                        UserChatPromptModel.owner_user_id == owner_user_id,
                        UserChatPromptModel.is_default.is_(True),
                    )
                )
            ).first()
            if row is None:
                timestamp = utc_now()
                row = UserChatPromptModel(
                    id=f"prompt_{_uuid_hex()}",
                    owner_user_id=owner_user_id,
                    label=label,
                    content=content,
                    is_default=True,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(row)
                await session.commit()
        return _user_chat_prompt_record(row)

    async def create(
        self,
        *,
        owner_user_id: str,
        prompt_id: str,
        label: str,
        content: str,
        is_default: bool = False,
    ) -> UserChatPromptRecord:
        timestamp = utc_now()
        row = UserChatPromptModel(
            id=prompt_id,
            owner_user_id=owner_user_id,
            label=label,
            content=content,
            is_default=is_default,
            created_at=timestamp,
            updated_at=timestamp,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        return _user_chat_prompt_record(row)

    async def get(
        self,
        *,
        owner_user_id: str,
        prompt_id: str,
    ) -> UserChatPromptRecord | None:
        stmt = select(UserChatPromptModel).where(
            UserChatPromptModel.id == prompt_id,
            UserChatPromptModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _user_chat_prompt_record(row) if row is not None else None

    async def list(self, *, owner_user_id: str) -> list[UserChatPromptRecord]:
        stmt = (
            select(UserChatPromptModel)
            .where(UserChatPromptModel.owner_user_id == owner_user_id)
            .order_by(
                UserChatPromptModel.is_default.desc(),
                UserChatPromptModel.updated_at.desc(),
                UserChatPromptModel.id.asc(),
            )
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_user_chat_prompt_record(row) for row in rows]

    async def update(
        self,
        *,
        owner_user_id: str,
        prompt_id: str,
        label: str,
        content: str,
    ) -> UserChatPromptRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(UserChatPromptModel).where(
                        UserChatPromptModel.id == prompt_id,
                        UserChatPromptModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            row.label = label
            row.content = content
            row.updated_at = utc_now()
            await session.commit()
        return _user_chat_prompt_record(row)

    async def delete(self, *, owner_user_id: str, prompt_id: str) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(UserChatPromptModel).where(
                        UserChatPromptModel.id == prompt_id,
                        UserChatPromptModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True


class SQLAlchemyUserChatSkillRepository:
    """SQLAlchemy owner-scoped uploaded Skill persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        filename: str,
        name: str,
        description: str,
        content: str,
        size_bytes: int,
    ) -> UserChatSkillRecord:
        timestamp = utc_now()
        row = UserChatSkillModel(
            id=skill_id,
            owner_user_id=owner_user_id,
            filename=filename,
            name=name,
            description=description,
            content=content,
            size_bytes=size_bytes,
            created_at=timestamp,
            updated_at=timestamp,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(f"Skill name '{name}' 已存在，请使用不同的 name。") from exc
        return _user_chat_skill_record(row)

    async def get(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
    ) -> UserChatSkillRecord | None:
        stmt = select(UserChatSkillModel).where(
            UserChatSkillModel.id == skill_id,
            UserChatSkillModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _user_chat_skill_record(row) if row is not None else None

    async def list(self, *, owner_user_id: str) -> list[UserChatSkillRecord]:
        stmt = (
            select(UserChatSkillModel)
            .where(UserChatSkillModel.owner_user_id == owner_user_id)
            .order_by(UserChatSkillModel.updated_at.desc(), UserChatSkillModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_user_chat_skill_record(row) for row in rows]

    async def delete(self, *, owner_user_id: str, skill_id: str) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(UserChatSkillModel).where(
                        UserChatSkillModel.id == skill_id,
                        UserChatSkillModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True


class SQLAlchemyToolCallAuditRepository:
    """SQLAlchemy implementation of generic Agent tool call audit persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_for_chat_session(
        self,
        *,
        owner_user_id: str,
        audit_id: str,
        chat_session_id: str,
        tool_name: str,
        arguments: JsonDict | None = None,
        started_at: datetime | None = None,
    ) -> AgentToolCallAuditRecord:
        timestamp = started_at or utc_now()
        row = AgentToolCallAuditModel(
            id=audit_id,
            owner_user_id=owner_user_id,
            chat_session_id=chat_session_id,
            diagnostic_task_id=None,
            tool_name=tool_name,
            status="started",
            arguments=arguments or {},
            result_summary=None,
            error_message=None,
            started_at=timestamp,
            completed_at=None,
            duration_ms=None,
            created_at=timestamp,
        )
        async with self._session_factory() as session:
            await _require_chat_session(session, owner_user_id, chat_session_id)
            session.add(row)
            await session.commit()
        return _agent_tool_call_audit_record(row)

    async def create_for_diagnostic_task(
        self,
        *,
        owner_user_id: str,
        audit_id: str,
        diagnostic_task_id: str,
        tool_name: str,
        arguments: JsonDict | None = None,
        started_at: datetime | None = None,
    ) -> AgentToolCallAuditRecord:
        timestamp = started_at or utc_now()
        values = {
            "id": audit_id,
            "owner_user_id": owner_user_id,
            "chat_session_id": None,
            "diagnostic_task_id": diagnostic_task_id,
            "tool_name": tool_name,
            "status": "started",
            "arguments": arguments or {},
            "result_summary": None,
            "error_message": None,
            "started_at": timestamp,
            "completed_at": None,
            "duration_ms": None,
            "created_at": timestamp,
        }
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, diagnostic_task_id)
            await session.execute(
                postgresql_insert(AgentToolCallAuditModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        async with self._session_factory() as session:
            row = await session.get(AgentToolCallAuditModel, audit_id)
        if (
            row is None
            or row.owner_user_id != owner_user_id
            or row.diagnostic_task_id != diagnostic_task_id
        ):
            raise TenantScopeError("Tool audit identity is outside diagnostic task scope.")
        return _agent_tool_call_audit_record(row)

    async def finalize(
        self,
        *,
        owner_user_id: str,
        audit_id: str,
        status: str,
        result_summary: str | None = None,
        error_message: str | None = None,
        completed_at: datetime | None = None,
    ) -> AgentToolCallAuditRecord | None:
        timestamp = completed_at or utc_now()
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(AgentToolCallAuditModel).where(
                        AgentToolCallAuditModel.id == audit_id,
                        AgentToolCallAuditModel.owner_user_id == owner_user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            row.status = status
            row.result_summary = result_summary
            row.error_message = error_message
            row.completed_at = timestamp
            row.duration_ms = max(
                0,
                round((timestamp - _ensure_utc(row.started_at)).total_seconds() * 1000),
            )
            await session.commit()
        return _agent_tool_call_audit_record(row)

    async def list_for_chat_session(
        self,
        *,
        owner_user_id: str,
        chat_session_id: str,
    ) -> list[AgentToolCallAuditRecord]:
        async with self._session_factory() as session:
            await _require_chat_session(session, owner_user_id, chat_session_id)
            rows = list(
                (
                    await session.scalars(
                        select(AgentToolCallAuditModel)
                        .where(
                            AgentToolCallAuditModel.owner_user_id == owner_user_id,
                            AgentToolCallAuditModel.chat_session_id == chat_session_id,
                        )
                        .order_by(
                            AgentToolCallAuditModel.created_at.asc(),
                            AgentToolCallAuditModel.id.asc(),
                        )
                    )
                ).all()
            )
        return [_agent_tool_call_audit_record(row) for row in rows]

    async def list_for_diagnostic_task(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
    ) -> list[AgentToolCallAuditRecord]:
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, diagnostic_task_id)
            rows = list(
                (
                    await session.scalars(
                        select(AgentToolCallAuditModel)
                        .where(
                            AgentToolCallAuditModel.owner_user_id == owner_user_id,
                            AgentToolCallAuditModel.diagnostic_task_id == diagnostic_task_id,
                        )
                        .order_by(
                            AgentToolCallAuditModel.created_at.asc(),
                            AgentToolCallAuditModel.id.asc(),
                        )
                    )
                ).all()
            )
        return [_agent_tool_call_audit_record(row) for row in rows]


class SQLAlchemyKnowledgeDocumentRepository:
    """SQLAlchemy implementation of knowledge document metadata persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_document(
        self,
        *,
        owner_user_id: str,
        document_id: str,
        knowledge_base_id: str,
        filename: str,
        size_bytes: int,
        mime_type: str,
        content_hash: str,
        status: str = "ready",
        index_status: str = "pending",
        metadata: JsonDict | None = None,
        source: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> KnowledgeDocumentRecord:
        timestamp = uploaded_at or utc_now()
        row = KnowledgeDocumentModel(
            id=document_id,
            owner_user_id=owner_user_id,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            size_bytes=size_bytes,
            mime_type=mime_type,
            content_hash=content_hash,
            status=status,
            index_status=index_status,
            source=source,
            metadata_json=metadata or {},
            uploaded_at=timestamp,
            updated_at=timestamp,
            deleted_at=None,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        return _knowledge_document_record(row)

    async def get_document(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        document_id: str,
        include_deleted: bool = False,
    ) -> KnowledgeDocumentRecord | None:
        stmt = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.id == document_id,
            KnowledgeDocumentModel.owner_user_id == owner_user_id,
            KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
        )
        stmt = _apply_deleted_filter(stmt, include_deleted)
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _knowledge_document_record(row) if row is not None else None

    async def list_documents(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        time_range: TimeRangeFilter | None = None,
        include_deleted: bool = False,
    ) -> list[KnowledgeDocumentRecord]:
        stmt = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.owner_user_id == owner_user_id,
            KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
        )
        stmt = _apply_deleted_filter(stmt, include_deleted)
        stmt = _apply_time_range(stmt, KnowledgeDocumentModel.uploaded_at, time_range)
        stmt = stmt.order_by(
            KnowledgeDocumentModel.uploaded_at.desc(),
            KnowledgeDocumentModel.id.asc(),
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_knowledge_document_record(row) for row in rows]

    async def find_active_by_hash(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        content_hash: str,
    ) -> KnowledgeDocumentRecord | None:
        stmt = (
            select(KnowledgeDocumentModel)
            .where(
                KnowledgeDocumentModel.owner_user_id == owner_user_id,
                KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
                KnowledgeDocumentModel.content_hash == content_hash,
                KnowledgeDocumentModel.deleted_at.is_(None),
                KnowledgeDocumentModel.status != "deleted",
            )
            .order_by(KnowledgeDocumentModel.uploaded_at.desc(), KnowledgeDocumentModel.id.asc())
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).first()
        return _knowledge_document_record(row) if row is not None else None

    async def find_active_by_filename(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        filename: str,
    ) -> KnowledgeDocumentRecord | None:
        stmt = (
            select(KnowledgeDocumentModel)
            .where(
                KnowledgeDocumentModel.owner_user_id == owner_user_id,
                KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
                KnowledgeDocumentModel.filename == filename,
                KnowledgeDocumentModel.deleted_at.is_(None),
                KnowledgeDocumentModel.status != "deleted",
            )
            .order_by(KnowledgeDocumentModel.uploaded_at.desc(), KnowledgeDocumentModel.id.asc())
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).first()
        return _knowledge_document_record(row) if row is not None else None

    async def mark_document_deleted(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        document_id: str,
        deleted_at: datetime | None = None,
    ) -> KnowledgeDocumentRecord | None:
        timestamp = deleted_at or utc_now()
        stmt = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.id == document_id,
            KnowledgeDocumentModel.owner_user_id == owner_user_id,
            KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
            if row is None:
                return None
            row.status = "deleted"
            row.updated_at = timestamp
            row.deleted_at = timestamp
            await session.commit()
        return _knowledge_document_record(row)

    async def update_index_status(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        document_id: str,
        index_status: str,
        updated_at: datetime | None = None,
    ) -> KnowledgeDocumentRecord | None:
        timestamp = updated_at or utc_now()
        stmt = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.id == document_id,
            KnowledgeDocumentModel.owner_user_id == owner_user_id,
            KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
            if row is None:
                return None
            row.index_status = index_status
            row.updated_at = timestamp
            await session.commit()
        return _knowledge_document_record(row)

    async def get_knowledge_base_cache_version(
        self,
        *,
        owner_user_id: str,
        knowledge_base_ids: Sequence[str],
    ) -> str:
        """Hash canonical document identity/revision facts in one owner scope."""
        knowledge_base_ids = tuple(sorted(set(knowledge_base_ids)))
        stmt = (
            select(KnowledgeDocumentModel)
            .where(
                KnowledgeDocumentModel.owner_user_id == owner_user_id,
                KnowledgeDocumentModel.knowledge_base_id.in_(knowledge_base_ids),
            )
            .order_by(
                KnowledgeDocumentModel.knowledge_base_id.asc(),
                KnowledgeDocumentModel.id.asc(),
            )
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all()) if knowledge_base_ids else []
        facts = {
            "ownerUserId": owner_user_id,
            "knowledgeBaseIds": knowledge_base_ids,
            "documents": [
                {
                    "knowledgeBaseId": row.knowledge_base_id,
                    "documentId": row.id,
                    "contentHash": row.content_hash,
                    "status": row.status,
                    "indexStatus": row.index_status,
                    "updatedAt": _ensure_utc(row.updated_at).isoformat(),
                    "deletedAt": (
                        _ensure_utc(row.deleted_at).isoformat()
                        if row.deleted_at is not None
                        else None
                    ),
                }
                for row in rows
            ],
        }
        serialized = json.dumps(facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SQLAlchemyDocumentIndexTaskRepository:
    """SQLAlchemy implementation of document index task persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        knowledge_base_id: str,
        document_id: str,
        status: str = "pending",
        retry_of_task_id: str | None = None,
        created_at: datetime | None = None,
    ) -> DocumentIndexTaskRecord:
        timestamp = created_at or utc_now()
        row = DocumentIndexTaskModel(
            id=task_id,
            owner_user_id=owner_user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            status=status,
            failure_reason=None,
            retry_of_task_id=retry_of_task_id,
            created_at=timestamp,
            updated_at=timestamp,
            started_at=None,
            completed_at=None,
        )
        async with self._session_factory() as session:
            await _require_document(session, owner_user_id, knowledge_base_id, document_id)
            session.add(row)
            await session.commit()
        return _document_index_task_record(row)

    async def create_retry(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        retry_of_task_id: str,
        created_at: datetime | None = None,
    ) -> DocumentIndexTaskRecord:
        async with self._session_factory() as session:
            prior = await _require_document_index_task(session, owner_user_id, retry_of_task_id)
            timestamp = created_at or utc_now()
            row = DocumentIndexTaskModel(
                id=task_id,
                owner_user_id=owner_user_id,
                knowledge_base_id=prior.knowledge_base_id,
                document_id=prior.document_id,
                status="pending",
                failure_reason=None,
                retry_of_task_id=prior.id,
                created_at=timestamp,
                updated_at=timestamp,
                started_at=None,
                completed_at=None,
            )
            session.add(row)
            await session.commit()
        return _document_index_task_record(row)

    async def get_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> DocumentIndexTaskRecord | None:
        stmt = select(DocumentIndexTaskModel).where(
            DocumentIndexTaskModel.id == task_id,
            DocumentIndexTaskModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _document_index_task_record(row) if row is not None else None

    async def list_tasks_for_document(
        self,
        *,
        owner_user_id: str,
        knowledge_base_id: str,
        document_id: str,
    ) -> list[DocumentIndexTaskRecord]:
        stmt = (
            select(DocumentIndexTaskModel)
            .where(
                DocumentIndexTaskModel.owner_user_id == owner_user_id,
                DocumentIndexTaskModel.knowledge_base_id == knowledge_base_id,
                DocumentIndexTaskModel.document_id == document_id,
            )
            .order_by(DocumentIndexTaskModel.created_at.asc(), DocumentIndexTaskModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_document_index_task_record(row) for row in rows]

    async def mark_running(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        started_at: datetime | None = None,
    ) -> DocumentIndexTaskRecord | None:
        timestamp = started_at or utc_now()
        return await self._update_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="running",
            updated_at=timestamp,
            started_at=timestamp,
            completed_at=None,
            failure_reason=None,
        )

    async def mark_succeeded(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        completed_at: datetime | None = None,
    ) -> DocumentIndexTaskRecord | None:
        timestamp = completed_at or utc_now()
        return await self._update_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="succeeded",
            updated_at=timestamp,
            completed_at=timestamp,
            failure_reason=None,
        )

    async def mark_failed(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        failure_reason: str,
        completed_at: datetime | None = None,
    ) -> DocumentIndexTaskRecord | None:
        timestamp = completed_at or utc_now()
        return await self._update_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="failed",
            updated_at=timestamp,
            completed_at=timestamp,
            failure_reason=failure_reason,
        )

    async def _update_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        status: str,
        updated_at: datetime,
        started_at: datetime | None | object = _UNSET,
        completed_at: datetime | None | object = _UNSET,
        failure_reason: str | None | object = _UNSET,
    ) -> DocumentIndexTaskRecord | None:
        stmt = select(DocumentIndexTaskModel).where(
            DocumentIndexTaskModel.id == task_id,
            DocumentIndexTaskModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
            if row is None:
                return None
            row.status = status
            row.updated_at = updated_at
            if started_at is not _UNSET:
                row.started_at = cast(datetime | None, started_at)
            if completed_at is not _UNSET:
                row.completed_at = cast(datetime | None, completed_at)
            if failure_reason is not _UNSET:
                row.failure_reason = cast(str | None, failure_reason)
            await session.commit()
        return _document_index_task_record(row)

    async def mark_cancelled(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        completed_at: datetime | None = None,
    ) -> DocumentIndexTaskRecord | None:
        timestamp = completed_at or utc_now()
        return await self._update_task(
            owner_user_id=owner_user_id,
            task_id=task_id,
            status="cancelled",
            updated_at=timestamp,
            completed_at=timestamp,
        )


class SQLAlchemyDiagnosticMemoryRepository:
    """SQLAlchemy implementation of AIOps diagnostic memory persistence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        status: str,
        query: str,
        input_payload: JsonDict | None = None,
        result_payload: JsonDict | None = None,
        created_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> DiagnosticTaskRecord:
        timestamp = created_at or utc_now()
        row = DiagnosticTaskModel(
            id=task_id,
            owner_user_id=owner_user_id,
            status=status,
            query=query,
            input_payload=input_payload or {},
            result_payload=result_payload or {},
            created_at=timestamp,
            updated_at=timestamp,
            completed_at=completed_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        return _diagnostic_task_record(row)

    async def get_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> DiagnosticTaskRecord | None:
        stmt = select(DiagnosticTaskModel).where(
            DiagnosticTaskModel.id == task_id,
            DiagnosticTaskModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _diagnostic_task_record(row) if row is not None else None

    async def update_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        status: str,
        result_payload: JsonDict | None = None,
        completed_at: datetime | None = None,
    ) -> DiagnosticTaskRecord | None:
        async with self._session_factory() as session:
            row = await _find_diagnostic_task(session, owner_user_id, task_id)
            if row is None:
                return None
            row.status = status
            row.updated_at = utc_now()
            if result_payload is not None:
                row.result_payload = result_payload
            if completed_at is not None:
                row.completed_at = completed_at
            await session.commit()
        return _diagnostic_task_record(row)

    async def list_tasks(
        self,
        *,
        owner_user_id: str,
        time_range: TimeRangeFilter | None = None,
    ) -> list[DiagnosticTaskRecord]:
        stmt = _apply_time_range(
            select(DiagnosticTaskModel).where(DiagnosticTaskModel.owner_user_id == owner_user_id),
            DiagnosticTaskModel.created_at,
            time_range,
        )
        stmt = stmt.order_by(DiagnosticTaskModel.created_at.asc(), DiagnosticTaskModel.id.asc())
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_diagnostic_task_record(row) for row in rows]

    async def add_report(
        self,
        *,
        owner_user_id: str,
        report_id: str,
        task_id: str,
        title: str,
        content: str,
        payload: JsonDict | None = None,
        created_at: datetime | None = None,
    ) -> DiagnosticReportRecord:
        values = {
            "id": report_id,
            "owner_user_id": owner_user_id,
            "task_id": task_id,
            "title": title,
            "content": content,
            "payload": payload or {},
            "created_at": created_at or utc_now(),
        }
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            await session.execute(
                postgresql_insert(DiagnosticReportModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        async with self._session_factory() as session:
            row = await session.get(DiagnosticReportModel, report_id)
        if row is None or row.owner_user_id != owner_user_id or row.task_id != task_id:
            raise TenantScopeError("Diagnostic report identity is outside task scope.")
        return _diagnostic_report_record(row)

    async def list_reports(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> list[DiagnosticReportRecord]:
        stmt = (
            select(DiagnosticReportModel)
            .where(
                DiagnosticReportModel.owner_user_id == owner_user_id,
                DiagnosticReportModel.task_id == task_id,
            )
            .order_by(DiagnosticReportModel.created_at.asc(), DiagnosticReportModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_diagnostic_report_record(row) for row in rows]

    async def get_report(
        self,
        *,
        owner_user_id: str,
        report_id: str,
    ) -> DiagnosticReportRecord | None:
        stmt = select(DiagnosticReportModel).where(
            DiagnosticReportModel.id == report_id,
            DiagnosticReportModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _diagnostic_report_record(row) if row is not None else None

    async def create_case(
        self,
        *,
        owner_user_id: str,
        case_id: str,
        task_id: str,
        report_id: str,
        document_id: str,
        index_task_id: str,
        alert_name: str,
        service: str,
        keywords: list[str],
        root_cause: str,
        remediation: str,
        summary: str,
        evidence_ids: list[str],
    ) -> DiagnosticCaseRecord:
        row = DiagnosticCaseModel(
            id=case_id,
            owner_user_id=owner_user_id,
            task_id=task_id,
            report_id=report_id,
            document_id=document_id,
            index_task_id=index_task_id,
            alert_name=alert_name,
            service=service,
            keywords=keywords,
            root_cause=root_cause,
            remediation=remediation,
            summary=summary,
            evidence_ids=evidence_ids,
        )
        async with self._session_factory() as session:
            existing = await _find_diagnostic_case_for_task(session, owner_user_id, task_id)
            if existing is not None:
                return _diagnostic_case_record(existing)
            await _require_diagnostic_report(session, owner_user_id, task_id, report_id)
            index_task = await _require_document_index_task(session, owner_user_id, index_task_id)
            if index_task.document_id != document_id:
                raise TenantScopeError(
                    f"Document index task does not match document: {index_task_id}"
                )
            await _require_document(
                session,
                owner_user_id,
                index_task.knowledge_base_id,
                document_id,
            )
            session.add(row)
            await session.commit()
        return _diagnostic_case_record(row)

    async def get_case_for_task(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> DiagnosticCaseRecord | None:
        async with self._session_factory() as session:
            row = await _find_diagnostic_case_for_task(session, owner_user_id, task_id)
        return _diagnostic_case_record(row) if row is not None else None

    async def get_case(
        self,
        *,
        owner_user_id: str,
        case_id: str,
    ) -> DiagnosticCaseRecord | None:
        async with self._session_factory() as session:
            row = await _find_diagnostic_case(session, owner_user_id, case_id)
        return _diagnostic_case_record(row) if row is not None else None

    async def list_cases(self, *, owner_user_id: str) -> list[DiagnosticCaseRecord]:
        stmt = (
            select(DiagnosticCaseModel)
            .where(DiagnosticCaseModel.owner_user_id == owner_user_id)
            .order_by(DiagnosticCaseModel.created_at.desc(), DiagnosticCaseModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_diagnostic_case_record(row) for row in rows]

    async def create_step(
        self,
        *,
        owner_user_id: str,
        step_id: str,
        task_id: str,
        sequence: int,
        phase: str,
        status: str,
        payload: JsonDict | None = None,
        created_at: datetime | None = None,
    ) -> DiagnosticStepRecord:
        values = {
            "id": step_id,
            "owner_user_id": owner_user_id,
            "task_id": task_id,
            "sequence": sequence,
            "phase": phase,
            "status": status,
            "payload": payload or {},
            "created_at": created_at or utc_now(),
        }
        async with self._session_factory() as session:
            task = (
                await session.scalars(
                    select(DiagnosticTaskModel)
                    .where(
                        DiagnosticTaskModel.id == task_id,
                        DiagnosticTaskModel.owner_user_id == owner_user_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if task is None:
                raise TenantScopeError("Diagnostic task is outside owner scope.")
            existing = await session.get(DiagnosticStepModel, step_id)
            if existing is None:
                latest_sequence = (
                    await session.scalars(
                        select(DiagnosticStepModel.sequence)
                        .where(
                            DiagnosticStepModel.owner_user_id == owner_user_id,
                            DiagnosticStepModel.task_id == task_id,
                        )
                        .order_by(DiagnosticStepModel.sequence.desc())
                        .limit(1)
                    )
                ).one_or_none()
                values["sequence"] = max(sequence, (latest_sequence or 0) + 1)
            await session.execute(
                postgresql_insert(DiagnosticStepModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        async with self._session_factory() as session:
            row = await session.get(DiagnosticStepModel, step_id)
        if row is None or row.owner_user_id != owner_user_id or row.task_id != task_id:
            raise TenantScopeError("Diagnostic step identity is outside task scope.")
        return _diagnostic_step_record(row)

    async def list_steps(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> list[DiagnosticStepRecord]:
        stmt = (
            select(DiagnosticStepModel)
            .where(
                DiagnosticStepModel.owner_user_id == owner_user_id,
                DiagnosticStepModel.task_id == task_id,
            )
            .order_by(DiagnosticStepModel.sequence.asc(), DiagnosticStepModel.id.asc())
        )
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            rows = list((await session.scalars(stmt)).all())
        return [_diagnostic_step_record(row) for row in rows]

    async def get_step(
        self,
        *,
        owner_user_id: str,
        step_id: str,
    ) -> DiagnosticStepRecord | None:
        stmt = select(DiagnosticStepModel).where(
            DiagnosticStepModel.id == step_id,
            DiagnosticStepModel.owner_user_id == owner_user_id,
        )
        async with self._session_factory() as session:
            row = (await session.scalars(stmt)).one_or_none()
        return _diagnostic_step_record(row) if row is not None else None

    async def create_evidence(
        self,
        *,
        owner_user_id: str,
        evidence_id: str,
        task_id: str,
        kind: str,
        source: str,
        summary: str,
        payload: JsonDict | None = None,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        created_at: datetime | None = None,
    ) -> DiagnosticEvidenceRecord:
        values = {
            "id": evidence_id,
            "owner_user_id": owner_user_id,
            "task_id": task_id,
            "step_id": step_id,
            "tool_call_id": tool_call_id,
            "kind": kind,
            "source": source,
            "summary": summary,
            "payload": payload or {},
            "created_at": created_at or utc_now(),
        }
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            if step_id is not None:
                await _require_diagnostic_step(session, owner_user_id, task_id, step_id)
            await session.execute(
                postgresql_insert(DiagnosticEvidenceModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        async with self._session_factory() as session:
            row = await session.get(DiagnosticEvidenceModel, evidence_id)
        if (
            row is None
            or row.owner_user_id != owner_user_id
            or row.task_id != task_id
        ):
            raise TenantScopeError("Diagnostic evidence identity is outside task scope.")
        return _diagnostic_evidence_record(row)

    async def list_evidence(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> list[DiagnosticEvidenceRecord]:
        stmt = (
            select(DiagnosticEvidenceModel)
            .where(
                DiagnosticEvidenceModel.owner_user_id == owner_user_id,
                DiagnosticEvidenceModel.task_id == task_id,
            )
            .order_by(DiagnosticEvidenceModel.created_at.asc(), DiagnosticEvidenceModel.id.asc())
        )
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            rows = list((await session.scalars(stmt)).all())
        return [_diagnostic_evidence_record(row) for row in rows]

    async def link_report_evidence(
        self,
        *,
        owner_user_id: str,
        link_id: str,
        task_id: str,
        report_id: str,
        evidence_id: str,
        created_at: datetime | None = None,
    ) -> ReportEvidenceLinkRecord:
        values = {
            "id": link_id,
            "owner_user_id": owner_user_id,
            "task_id": task_id,
            "report_id": report_id,
            "evidence_id": evidence_id,
            "created_at": created_at or utc_now(),
        }
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            await _require_diagnostic_report(session, owner_user_id, task_id, report_id)
            await _require_diagnostic_evidence(session, owner_user_id, task_id, evidence_id)
            await session.execute(
                postgresql_insert(ReportEvidenceLinkModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        async with self._session_factory() as session:
            row = await session.get(ReportEvidenceLinkModel, link_id)
        if row is None or row.owner_user_id != owner_user_id or row.task_id != task_id:
            raise TenantScopeError("Report evidence link identity is outside task scope.")
        return _report_evidence_link_record(row)

    async def list_report_evidence_links(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> list[ReportEvidenceLinkRecord]:
        stmt = (
            select(ReportEvidenceLinkModel)
            .where(
                ReportEvidenceLinkModel.owner_user_id == owner_user_id,
                ReportEvidenceLinkModel.task_id == task_id,
            )
            .order_by(ReportEvidenceLinkModel.created_at.asc(), ReportEvidenceLinkModel.id.asc())
        )
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            rows = list((await session.scalars(stmt)).all())
        return [_report_evidence_link_record(row) for row in rows]

    async def add_tool_call_audit(
        self,
        *,
        owner_user_id: str,
        audit_id: str,
        task_id: str,
        tool_name: str,
        status: str,
        arguments: JsonDict | None = None,
        result_payload: JsonDict | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ToolCallAuditRecord:
        created_at = started_at or utc_now()
        row = ToolCallAuditModel(
            id=audit_id,
            owner_user_id=owner_user_id,
            task_id=task_id,
            tool_name=tool_name,
            status=status,
            arguments=arguments or {},
            result_payload=result_payload or {},
            error_message=error_message,
            started_at=created_at,
            completed_at=completed_at,
            created_at=created_at,
        )
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            session.add(row)
            await session.commit()
        return _tool_call_audit_record(row)

    async def list_tool_call_audits(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> list[ToolCallAuditRecord]:
        stmt = (
            select(ToolCallAuditModel)
            .where(
                ToolCallAuditModel.owner_user_id == owner_user_id,
                ToolCallAuditModel.task_id == task_id,
            )
            .order_by(ToolCallAuditModel.created_at.asc(), ToolCallAuditModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_tool_call_audit_record(row) for row in rows]

    async def save_checkpoint(
        self,
        *,
        owner_user_id: str,
        checkpoint_record_id: str,
        task_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        checkpoint_payload: JsonDict | None = None,
        metadata: JsonDict | None = None,
        created_at: datetime | None = None,
    ) -> GraphCheckpointRecord:
        values = {
            "id": checkpoint_record_id,
            "owner_user_id": owner_user_id,
            "task_id": task_id,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
            "checkpoint_payload": checkpoint_payload or {},
            "metadata_json": metadata or {},
            "created_at": created_at or utc_now(),
        }
        async with self._session_factory() as session:
            await _require_task(session, owner_user_id, task_id)
            await session.execute(
                postgresql_insert(GraphCheckpointModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        async with self._session_factory() as session:
            row = await session.get(GraphCheckpointModel, checkpoint_record_id)
        if (
            row is None
            or row.owner_user_id != owner_user_id
            or row.task_id != task_id
            or row.thread_id != thread_id
            or row.checkpoint_ns != checkpoint_ns
            or row.checkpoint_id != checkpoint_id
        ):
            raise TenantScopeError("Graph checkpoint identity is outside task scope.")
        if (
            _json_dict(row.checkpoint_payload) != values["checkpoint_payload"]
            or _json_dict(row.metadata_json) != values["metadata_json"]
        ):
            raise ValueError("checkpoint_content_conflict")
        return _graph_checkpoint_record(row)

    async def list_checkpoints(
        self,
        *,
        owner_user_id: str,
        task_id: str,
    ) -> list[GraphCheckpointRecord]:
        stmt = (
            select(GraphCheckpointModel)
            .where(
                GraphCheckpointModel.owner_user_id == owner_user_id,
                GraphCheckpointModel.task_id == task_id,
            )
            .order_by(GraphCheckpointModel.created_at.asc(), GraphCheckpointModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt)).all())
        return [_graph_checkpoint_record(row) for row in rows]


class SQLAlchemyEvaluationRepository:
    """PostgreSQL implementation for deterministic benchmark records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start_envelope(
        self,
        *,
        run_id: str,
        evaluation_kind: str,
        artifact_schema_version: str,
        provenance: str,
        run_metadata: JsonDict,
        scenario_id: str,
        suite_version: str,
        created_at: datetime,
        started_at: datetime,
    ) -> EvaluationRunRecord:
        values = {
            "run_id": run_id,
            "evaluation_kind": evaluation_kind,
            "artifact_schema_version": artifact_schema_version,
            "provenance": provenance,
            "run_metadata": run_metadata,
            "scenario_id": scenario_id,
            "mode": evaluation_kind,
            "suite_version": suite_version,
            "agent_version": {},
            "model_configuration": {},
            "status": "running",
            "created_at": created_at,
            "started_at": started_at,
        }
        async with self._session_factory() as session:
            await session.execute(
                postgresql_insert(EvaluationRunModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[EvaluationRunModel.run_id])
            )
            await session.commit()
            row = await session.get(EvaluationRunModel, run_id)
        if row is None:
            raise ValueError(f"Evaluation run does not exist after creation: {run_id}")
        identity = (
            row.evaluation_kind,
            row.artifact_schema_version,
            row.provenance,
            row.run_metadata,
            row.scenario_id,
            row.suite_version,
            _ensure_utc(row.created_at),
            _ensure_utc_optional(row.started_at),
        )
        requested = (
            evaluation_kind,
            artifact_schema_version,
            provenance,
            run_metadata,
            scenario_id,
            suite_version,
            _ensure_utc(created_at),
            _ensure_utc(started_at),
        )
        if identity != requested:
            raise ValueError(f"Run {run_id} has a different evaluation identity.")
        return _evaluation_run_record(row)

    async def finalize_envelope(
        self,
        *,
        run_id: str,
        artifact_checksum: str,
        status: str,
        validity: str | None,
        passed: bool | None,
        metrics: JsonDict,
        result_payload: JsonDict,
        diagnostic_task_id: str | None,
        failure_category: str | None,
        completed_at: datetime,
    ) -> tuple[EvaluationRunRecord, EvaluationResultRecord | None]:
        async with self._session_factory() as session:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(EvaluationRunModel)
                        .where(EvaluationRunModel.run_id == run_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if run is None:
                    raise ValueError(f"Evaluation run does not exist: {run_id}")
                existing = (
                    await session.scalars(
                        select(EvaluationResultModel).where(EvaluationResultModel.run_id == run_id)
                    )
                ).one_or_none()
                is_same = (
                    run.status == status
                    and run.artifact_checksum == artifact_checksum
                    and run.diagnostic_task_id == diagnostic_task_id
                    and run.failure_category == failure_category
                    and _ensure_utc_optional(run.completed_at) == _ensure_utc(completed_at)
                    and (
                        existing is not None
                        and existing.validity == validity
                        and existing.passed is passed
                        and existing.metrics == metrics
                        and existing.result_payload == result_payload
                    )
                )
                if run.status != "running":
                    if is_same:
                        assert existing is not None
                        return _evaluation_run_record(run), _evaluation_result_record(existing)
                    raise ValueError(f"Evaluation run {run_id} has a different evaluation result.")
                if existing is not None:
                    raise ValueError(f"Evaluation run {run_id} has a different evaluation result.")
                result = EvaluationResultModel(
                    result_id=run_id,
                    run_id=run_id,
                    dimension_scores={},
                    total=None,
                    raw_total=None,
                    validity=validity or "not_applicable",
                    passed=passed,
                    failures=[],
                    score_reasons=[],
                    hard_gate=None,
                    metrics=metrics,
                    result_payload=result_payload,
                    created_at=completed_at,
                )
                session.add(result)
                run.status = status
                run.artifact_checksum = artifact_checksum
                run.diagnostic_task_id = diagnostic_task_id
                run.failure_category = failure_category
                run.completed_at = completed_at
                await session.flush()
            return _evaluation_run_record(run), _evaluation_result_record(result)

    async def attach_artifact_checksum(
        self, *, run_id: str, artifact_checksum: str
    ) -> EvaluationRunRecord:
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.scalars(
                        select(EvaluationRunModel)
                        .where(EvaluationRunModel.run_id == run_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    raise ValueError(f"Evaluation run does not exist: {run_id}")
                if row.artifact_checksum not in {None, artifact_checksum}:
                    raise ValueError(f"Run {run_id} has a different artifact checksum.")
                row.artifact_checksum = artifact_checksum
            return _evaluation_run_record(row)

    async def list_runs_with_results(
        self,
    ) -> list[tuple[EvaluationRunRecord, EvaluationResultRecord | None]]:
        statement = (
            select(EvaluationRunModel, EvaluationResultModel)
            .outerjoin(
                EvaluationResultModel,
                EvaluationResultModel.run_id == EvaluationRunModel.run_id,
            )
            .order_by(EvaluationRunModel.created_at.asc(), EvaluationRunModel.run_id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.execute(statement)).all())
        return [
            (
                _evaluation_run_record(run),
                _evaluation_result_record(result) if result is not None else None,
            )
            for run, result in rows
        ]

    async def list_benchmark_diagnostic_tasks(self) -> list[DiagnosticTaskRecord]:
        benchmark_mode = DiagnosticTaskModel.input_payload["benchmarkMode"].astext
        statement = (
            select(DiagnosticTaskModel)
            .where(benchmark_mode.in_(("snapshot", "live")))
            .order_by(DiagnosticTaskModel.created_at.asc(), DiagnosticTaskModel.id.asc())
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return [_diagnostic_task_record(row) for row in rows]

    async def create_run(
        self,
        *,
        run_id: str,
        scenario_id: str,
        mode: str,
        suite_version: str,
        agent_version: JsonDict,
        model_configuration: JsonDict,
        created_at: datetime | None = None,
    ) -> EvaluationRunRecord:
        async with self._session_factory() as session:
            statement = (
                postgresql_insert(EvaluationRunModel)
                .values(
                    run_id=run_id,
                    scenario_id=scenario_id,
                    mode=mode,
                    suite_version=suite_version,
                    agent_version=agent_version,
                    model_configuration=model_configuration,
                    status="pending",
                    created_at=created_at or utc_now(),
                )
                .on_conflict_do_nothing(index_elements=[EvaluationRunModel.run_id])
            )
            await session.execute(statement)
            await session.commit()
            row = await session.get(EvaluationRunModel, run_id)
        if row is None:
            raise ValueError(f"Evaluation run does not exist after creation: {run_id}")
        identity = (
            row.scenario_id,
            row.mode,
            row.suite_version,
            row.agent_version,
            row.model_configuration,
        )
        requested = (
            scenario_id,
            mode,
            suite_version,
            agent_version,
            model_configuration,
        )
        if identity != requested:
            raise ValueError(f"Run {run_id} has a different evaluation identity.")
        return _evaluation_run_record(row)

    async def fail_run(
        self,
        *,
        run_id: str,
        status: EvaluationFailureStatus,
        failure_category: str,
        completed_at: datetime | None = None,
    ) -> EvaluationRunRecord:
        if status not in {"agent_failed", "infra_failed"}:
            raise ValueError(f"Unsupported evaluation failure status: {status}")
        if failure_category not in EVALUATION_FAILURE_CATEGORIES:
            raise ValueError(f"Unsupported evaluation failure category: {failure_category}")
        timestamp = completed_at or utc_now()
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(EvaluationRunModel)
                    .where(EvaluationRunModel.run_id == run_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ValueError(f"Evaluation run does not exist: {run_id}")
            if row.status == status and row.failure_category == failure_category:
                return _evaluation_run_record(row)
            if row.status != "pending":
                raise ValueError(f"Evaluation run {run_id} already has a terminal state.")
            row.status = status
            row.failure_category = failure_category
            row.started_at = row.started_at or row.created_at
            row.completed_at = timestamp
            await session.commit()
        return _evaluation_run_record(row)

    async def complete_run(
        self,
        *,
        run_id: str,
        diagnostic_task_id: str | None,
        completed_at: datetime | None = None,
    ) -> EvaluationRunRecord:
        timestamp = completed_at or utc_now()
        async with self._session_factory() as session:
            row = await session.get(EvaluationRunModel, run_id)
            if row is None:
                raise ValueError(f"Evaluation run does not exist: {run_id}")
            if row.status == "pending":
                row.status = "completed"
                row.started_at = row.started_at or row.created_at
                row.completed_at = timestamp
                row.diagnostic_task_id = diagnostic_task_id
                await session.commit()
            elif row.status != "completed":
                raise ValueError(f"Evaluation run {run_id} already has a terminal state.")
        return _evaluation_run_record(row)

    async def save_result(
        self,
        *,
        result_id: str,
        run_id: str,
        dimension_scores: JsonDict,
        total: int,
        raw_total: int,
        validity: str,
        passed: bool,
        failures: list[str],
        score_reasons: list[JsonDict],
        hard_gate: str | None,
        created_at: datetime | None = None,
    ) -> EvaluationResultRecord:
        async with self._session_factory() as session:
            run = await session.get(EvaluationRunModel, run_id)
            if run is None or run.status != "completed":
                raise ValueError(f"Evaluation run {run_id} must be completed before scoring.")
            existing = (
                await session.scalars(
                    select(EvaluationResultModel).where(EvaluationResultModel.run_id == run_id)
                )
            ).one_or_none()
            if existing is not None:
                if existing.result_id != result_id:
                    raise ValueError(f"Evaluation run {run_id} already has a scorecard.")
                return _evaluation_result_record(existing)
            row = EvaluationResultModel(
                result_id=result_id,
                run_id=run_id,
                dimension_scores=dimension_scores,
                total=total,
                raw_total=raw_total,
                validity=validity,
                passed=passed,
                failures=failures,
                score_reasons=score_reasons,
                hard_gate=hard_gate,
                created_at=created_at or utc_now(),
            )
            session.add(row)
            await session.commit()
        return _evaluation_result_record(row)

    async def finalize_run(
        self,
        *,
        run_id: str,
        result_id: str,
        dimension_scores: JsonDict,
        total: int,
        raw_total: int,
        validity: str,
        passed: bool,
        failures: list[str],
        score_reasons: list[JsonDict],
        hard_gate: str | None,
        diagnostic_task_id: str | None,
        completed_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> tuple[EvaluationRunRecord, EvaluationResultRecord]:
        timestamp = completed_at or utc_now()
        async with self._session_factory() as session:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(EvaluationRunModel)
                        .where(EvaluationRunModel.run_id == run_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if run is None:
                    raise ValueError(f"Evaluation run does not exist: {run_id}")

                existing = (
                    await session.scalars(
                        select(EvaluationResultModel).where(
                            EvaluationResultModel.run_id == run_id
                        )
                    )
                ).one_or_none()
                if run.status == "completed":
                    if existing is None or not _evaluation_result_matches(
                        existing,
                        result_id=result_id,
                        dimension_scores=dimension_scores,
                        total=total,
                        raw_total=raw_total,
                        validity=validity,
                        passed=passed,
                        failures=failures,
                        score_reasons=score_reasons,
                        hard_gate=hard_gate,
                    ):
                        raise ValueError(f"Evaluation run {run_id} has a different scorecard.")
                    if run.diagnostic_task_id != diagnostic_task_id:
                        raise ValueError(f"Evaluation run {run_id} has a different scorecard.")
                    return _evaluation_run_record(run), _evaluation_result_record(existing)
                if run.status != "pending":
                    raise ValueError(f"Evaluation run {run_id} already has a terminal state.")
                if existing is not None:
                    raise ValueError(f"Evaluation run {run_id} has a different scorecard.")

                result = EvaluationResultModel(
                    result_id=result_id,
                    run_id=run_id,
                    dimension_scores=dimension_scores,
                    total=total,
                    raw_total=raw_total,
                    validity=validity,
                    passed=passed,
                    failures=failures,
                    score_reasons=score_reasons,
                    hard_gate=hard_gate,
                    created_at=created_at or utc_now(),
                )
                session.add(result)
                run.status = "completed"
                run.started_at = run.started_at or run.created_at
                run.completed_at = timestamp
                run.diagnostic_task_id = diagnostic_task_id
                await session.flush()
            return _evaluation_run_record(run), _evaluation_result_record(result)

    async def get_run_with_result(
        self, run_id: str
    ) -> tuple[EvaluationRunRecord, EvaluationResultRecord | None] | None:
        async with self._session_factory() as session:
            run = await session.get(EvaluationRunModel, run_id)
            if run is None:
                return None
            result = (
                await session.scalars(
                    select(EvaluationResultModel).where(EvaluationResultModel.run_id == run_id)
                )
            ).one_or_none()
        return _evaluation_run_record(run), (
            _evaluation_result_record(result) if result is not None else None
        )


def create_sqlalchemy_memory_repositories(
    session_factory: async_sessionmaker[AsyncSession],
) -> MemoryRepositories:
    """Create a repository bundle backed by SQLAlchemy sessions."""
    from super_ai.memory.aiops_execution_sqlalchemy import (
        SQLAlchemyAiopsRuntimeRepositoryProvider,
    )
    from super_ai.memory.extended_sqlalchemy import (
        SQLAlchemyBackgroundJobRepository,
        SQLAlchemyMcpConnectionRepository,
        SQLAlchemyOutboxEventRepository,
        SQLAlchemyUserFeedbackRepository,
    )

    return MemoryRepositories(
        chat=SQLAlchemyChatMemoryRepository(session_factory),
        chat_configurations=SQLAlchemyUserChatConfigurationRepository(session_factory),
        chat_prompts=SQLAlchemyUserChatPromptRepository(session_factory),
        chat_skills=SQLAlchemyUserChatSkillRepository(session_factory),
        documents=SQLAlchemyKnowledgeDocumentRepository(session_factory),
        document_index_tasks=SQLAlchemyDocumentIndexTaskRepository(session_factory),
        diagnostics=SQLAlchemyDiagnosticMemoryRepository(session_factory),
        tool_call_audits=SQLAlchemyToolCallAuditRepository(session_factory),
        background_jobs=SQLAlchemyBackgroundJobRepository(session_factory),
        outbox_events=SQLAlchemyOutboxEventRepository(session_factory),
        feedback=SQLAlchemyUserFeedbackRepository(session_factory),
        mcp_connections=SQLAlchemyMcpConnectionRepository(session_factory),
        evaluations=SQLAlchemyEvaluationRepository(session_factory),
        aiops_runtime=SQLAlchemyAiopsRuntimeRepositoryProvider(session_factory),
    )


def _user_chat_configuration_record(row: UserChatConfigurationModel) -> UserChatConfigurationRecord:
    return UserChatConfigurationRecord(
        owner_user_id=row.owner_user_id,
        system_prompt_id=row.system_prompt_id,
        skill_ids=list(row.skill_ids),
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


def _user_chat_prompt_record(row: UserChatPromptModel) -> UserChatPromptRecord:
    return UserChatPromptRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        label=row.label,
        content=row.content,
        is_default=row.is_default,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


def _user_chat_skill_record(row: UserChatSkillModel) -> UserChatSkillRecord:
    return UserChatSkillRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        filename=row.filename,
        name=row.name,
        description=row.description,
        content=row.content,
        size_bytes=row.size_bytes,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


def _uuid_hex() -> str:
    return uuid4().hex


async def _require_chat_session(
    session: AsyncSession,
    owner_user_id: str,
    session_id: str,
) -> ChatSessionModel:
    row = await _find_chat_session(session, owner_user_id, session_id)
    if row is None:
        raise TenantScopeError(f"Chat session is not accessible: {session_id}")
    return row


async def _find_chat_session(
    session: AsyncSession,
    owner_user_id: str,
    session_id: str,
) -> ChatSessionModel | None:
    stmt = select(ChatSessionModel).where(
        ChatSessionModel.id == session_id,
        ChatSessionModel.owner_user_id == owner_user_id,
    )
    return (await session.scalars(stmt)).one_or_none()


async def _require_task(session: AsyncSession, owner_user_id: str, task_id: str) -> None:
    if await _find_diagnostic_task(session, owner_user_id, task_id) is None:
        raise TenantScopeError(f"Diagnostic task is not accessible: {task_id}")


async def _find_diagnostic_task(
    session: AsyncSession,
    owner_user_id: str,
    task_id: str,
) -> DiagnosticTaskModel | None:
    stmt = select(DiagnosticTaskModel).where(
        DiagnosticTaskModel.id == task_id,
        DiagnosticTaskModel.owner_user_id == owner_user_id,
    )
    return (await session.scalars(stmt)).one_or_none()


async def _find_diagnostic_case_for_task(
    session: AsyncSession,
    owner_user_id: str,
    task_id: str,
) -> DiagnosticCaseModel | None:
    stmt = select(DiagnosticCaseModel).where(
        DiagnosticCaseModel.owner_user_id == owner_user_id,
        DiagnosticCaseModel.task_id == task_id,
    )
    return (await session.scalars(stmt)).one_or_none()


async def _find_diagnostic_case(
    session: AsyncSession,
    owner_user_id: str,
    case_id: str,
) -> DiagnosticCaseModel | None:
    stmt = select(DiagnosticCaseModel).where(
        DiagnosticCaseModel.id == case_id,
        DiagnosticCaseModel.owner_user_id == owner_user_id,
    )
    return (await session.scalars(stmt)).one_or_none()


async def _require_diagnostic_step(
    session: AsyncSession,
    owner_user_id: str,
    task_id: str,
    step_id: str,
) -> DiagnosticStepModel:
    stmt = select(DiagnosticStepModel).where(
        DiagnosticStepModel.id == step_id,
        DiagnosticStepModel.owner_user_id == owner_user_id,
        DiagnosticStepModel.task_id == task_id,
    )
    row = (await session.scalars(stmt)).one_or_none()
    if row is None:
        raise TenantScopeError(f"Diagnostic step is not accessible: {step_id}")
    return row


async def _require_diagnostic_report(
    session: AsyncSession,
    owner_user_id: str,
    task_id: str,
    report_id: str,
) -> DiagnosticReportModel:
    stmt = select(DiagnosticReportModel).where(
        DiagnosticReportModel.id == report_id,
        DiagnosticReportModel.owner_user_id == owner_user_id,
        DiagnosticReportModel.task_id == task_id,
    )
    row = (await session.scalars(stmt)).one_or_none()
    if row is None:
        raise TenantScopeError(f"Diagnostic report is not accessible: {report_id}")
    return row


async def _require_diagnostic_evidence(
    session: AsyncSession,
    owner_user_id: str,
    task_id: str,
    evidence_id: str,
) -> DiagnosticEvidenceModel:
    stmt = select(DiagnosticEvidenceModel).where(
        DiagnosticEvidenceModel.id == evidence_id,
        DiagnosticEvidenceModel.owner_user_id == owner_user_id,
        DiagnosticEvidenceModel.task_id == task_id,
    )
    row = (await session.scalars(stmt)).one_or_none()
    if row is None:
        raise TenantScopeError(f"Diagnostic evidence is not accessible: {evidence_id}")
    return row


async def _require_document(
    session: AsyncSession,
    owner_user_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> KnowledgeDocumentModel:
    stmt = select(KnowledgeDocumentModel).where(
        KnowledgeDocumentModel.id == document_id,
        KnowledgeDocumentModel.owner_user_id == owner_user_id,
        KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id,
        KnowledgeDocumentModel.deleted_at.is_(None),
        KnowledgeDocumentModel.status != "deleted",
    )
    row = (await session.scalars(stmt)).one_or_none()
    if row is None:
        raise TenantScopeError(f"Document is not accessible: {document_id}")
    return row


async def _require_document_index_task(
    session: AsyncSession,
    owner_user_id: str,
    task_id: str,
) -> DocumentIndexTaskModel:
    stmt = select(DocumentIndexTaskModel).where(
        DocumentIndexTaskModel.id == task_id,
        DocumentIndexTaskModel.owner_user_id == owner_user_id,
    )
    row = (await session.scalars(stmt)).one_or_none()
    if row is None:
        raise TenantScopeError(f"Document index task is not accessible: {task_id}")
    return row


def _apply_time_range(
    stmt: Select[tuple[ModelT]],
    column: Any,
    time_range: TimeRangeFilter | None,
) -> Select[tuple[ModelT]]:
    if time_range is None:
        return stmt
    if time_range.start_at is not None:
        stmt = stmt.where(column >= time_range.start_at)
    if time_range.end_at is not None:
        stmt = stmt.where(column <= time_range.end_at)
    return stmt


def _apply_deleted_filter(
    stmt: Select[tuple[KnowledgeDocumentModel]],
    include_deleted: bool,
) -> Select[tuple[KnowledgeDocumentModel]]:
    if include_deleted:
        return stmt
    return stmt.where(
        KnowledgeDocumentModel.deleted_at.is_(None),
        KnowledgeDocumentModel.status != "deleted",
    )


def _json_dict(value: object) -> JsonDict:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): item for key, item in mapping.items()}
    return {}


def _chat_session_record(row: ChatSessionModel) -> ChatSessionRecord:
    return ChatSessionRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        title=row.title,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
        memory_mode=row.memory_mode,
        memory_summary=row.memory_summary,
        compacted_message_count=row.compacted_message_count,
        context_tokens=row.context_tokens,
        last_compacted_at=(
            _ensure_utc(row.last_compacted_at) if row.last_compacted_at is not None else None
        ),
    )


def _chat_message_record(row: ChatMessageModel) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        session_id=row.session_id,
        role=row.role,
        content=row.content,
        metadata=_json_dict(row.metadata_json),
        created_at=_ensure_utc(row.created_at),
    )


def _knowledge_document_record(row: KnowledgeDocumentModel) -> KnowledgeDocumentRecord:
    return KnowledgeDocumentRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        knowledge_base_id=row.knowledge_base_id,
        filename=row.filename,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        content_hash=row.content_hash,
        status=row.status,
        index_status=row.index_status,
        metadata=_json_dict(row.metadata_json),
        uploaded_at=_ensure_utc(row.uploaded_at),
        updated_at=_ensure_utc(row.updated_at),
        source=row.source,
        deleted_at=_ensure_utc_optional(row.deleted_at),
    )


def _document_index_task_record(row: DocumentIndexTaskModel) -> DocumentIndexTaskRecord:
    return DocumentIndexTaskRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        status=row.status,
        failure_reason=row.failure_reason,
        retry_of_task_id=row.retry_of_task_id,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
        started_at=_ensure_utc_optional(row.started_at),
        completed_at=_ensure_utc_optional(row.completed_at),
    )


def _diagnostic_task_record(row: DiagnosticTaskModel) -> DiagnosticTaskRecord:
    return DiagnosticTaskRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        status=row.status,
        query=row.query,
        input_payload=_json_dict(row.input_payload),
        result_payload=_json_dict(row.result_payload),
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
        completed_at=_ensure_utc_optional(row.completed_at),
    )


def _diagnostic_report_record(row: DiagnosticReportModel) -> DiagnosticReportRecord:
    return DiagnosticReportRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        title=row.title,
        content=row.content,
        payload=_json_dict(row.payload),
        created_at=_ensure_utc(row.created_at),
    )


def _diagnostic_case_record(row: DiagnosticCaseModel) -> DiagnosticCaseRecord:
    return DiagnosticCaseRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        report_id=row.report_id,
        document_id=row.document_id,
        index_task_id=row.index_task_id,
        alert_name=row.alert_name,
        service=row.service,
        keywords=list(row.keywords),
        root_cause=row.root_cause,
        remediation=row.remediation,
        summary=row.summary,
        evidence_ids=list(row.evidence_ids),
        created_at=_ensure_utc(row.created_at),
    )


def _diagnostic_step_record(row: DiagnosticStepModel) -> DiagnosticStepRecord:
    return DiagnosticStepRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        sequence=row.sequence,
        phase=row.phase,
        status=row.status,
        payload=_json_dict(row.payload),
        created_at=_ensure_utc(row.created_at),
    )


def _diagnostic_evidence_record(row: DiagnosticEvidenceModel) -> DiagnosticEvidenceRecord:
    return DiagnosticEvidenceRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        step_id=row.step_id,
        tool_call_id=row.tool_call_id,
        kind=row.kind,
        source=row.source,
        summary=row.summary,
        payload=_json_dict(row.payload),
        created_at=_ensure_utc(row.created_at),
    )


def _report_evidence_link_record(row: ReportEvidenceLinkModel) -> ReportEvidenceLinkRecord:
    return ReportEvidenceLinkRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        report_id=row.report_id,
        evidence_id=row.evidence_id,
        created_at=_ensure_utc(row.created_at),
    )


def _tool_call_audit_record(row: ToolCallAuditModel) -> ToolCallAuditRecord:
    return ToolCallAuditRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        tool_name=row.tool_name,
        status=row.status,
        arguments=_json_dict(row.arguments),
        result_payload=_json_dict(row.result_payload),
        error_message=row.error_message,
        started_at=_ensure_utc(row.started_at),
        completed_at=_ensure_utc_optional(row.completed_at),
        created_at=_ensure_utc(row.created_at),
    )


def _agent_tool_call_audit_record(row: AgentToolCallAuditModel) -> AgentToolCallAuditRecord:
    return AgentToolCallAuditRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        chat_session_id=row.chat_session_id,
        diagnostic_task_id=row.diagnostic_task_id,
        tool_name=row.tool_name,
        status=row.status,
        arguments=_json_dict(row.arguments),
        result_summary=row.result_summary,
        error_message=row.error_message,
        started_at=_ensure_utc(row.started_at),
        completed_at=_ensure_utc_optional(row.completed_at),
        duration_ms=row.duration_ms,
        created_at=_ensure_utc(row.created_at),
    )


def _graph_checkpoint_record(row: GraphCheckpointModel) -> GraphCheckpointRecord:
    return GraphCheckpointRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        thread_id=row.thread_id,
        checkpoint_ns=row.checkpoint_ns,
        checkpoint_id=row.checkpoint_id,
        checkpoint_payload=_json_dict(row.checkpoint_payload),
        metadata=_json_dict(row.metadata_json),
        created_at=_ensure_utc(row.created_at),
    )


def _evaluation_run_record(row: EvaluationRunModel) -> EvaluationRunRecord:
    return EvaluationRunRecord(
        run_id=row.run_id,
        evaluation_kind=row.evaluation_kind,
        artifact_schema_version=row.artifact_schema_version,
        artifact_checksum=row.artifact_checksum,
        provenance=row.provenance,
        run_metadata=_json_dict(row.run_metadata),
        scenario_id=row.scenario_id,
        mode=row.mode,
        suite_version=row.suite_version,
        agent_version=_json_dict(row.agent_version),
        model_configuration=_json_dict(row.model_configuration),
        status=row.status,
        failure_category=row.failure_category,
        diagnostic_task_id=row.diagnostic_task_id,
        created_at=_ensure_utc(row.created_at),
        started_at=_ensure_utc_optional(row.started_at),
        completed_at=_ensure_utc_optional(row.completed_at),
    )


def _evaluation_result_record(row: EvaluationResultModel) -> EvaluationResultRecord:
    return EvaluationResultRecord(
        result_id=row.result_id,
        run_id=row.run_id,
        dimension_scores=_json_dict(row.dimension_scores),
        total=row.total,
        raw_total=row.raw_total,
        validity=row.validity,
        passed=row.passed,
        failures=list(row.failures),
        score_reasons=[_json_dict(item) for item in row.score_reasons],
        hard_gate=row.hard_gate,
        metrics=_json_dict(row.metrics),
        result_payload=_json_dict(row.result_payload),
        created_at=_ensure_utc(row.created_at),
    )


def _evaluation_result_matches(
    row: EvaluationResultModel,
    *,
    result_id: str,
    dimension_scores: JsonDict,
    total: int,
    raw_total: int,
    validity: str,
    passed: bool,
    failures: list[str],
    score_reasons: list[JsonDict],
    hard_gate: str | None,
) -> bool:
    return (
        row.result_id == result_id
        and row.dimension_scores == dimension_scores
        and row.total == total
        and row.raw_total == raw_total
        and row.validity == validity
        and row.passed is passed
        and row.failures == failures
        and row.score_reasons == score_reasons
        and row.hard_gate == hard_gate
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ensure_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)
