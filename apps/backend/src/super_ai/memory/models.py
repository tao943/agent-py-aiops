"""SQLAlchemy ORM models for backend memory persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base metadata for memory ORM models."""


class UserModel(Base):
    """Persisted authenticated user."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AuthSessionModel(Base):
    """Persisted revocable auth session."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackgroundJobModel(Base):
    """Durable owner-scoped background work item with a renewable lease."""

    __tablename__ = "background_jobs"
    __table_args__ = (
        Index("ix_background_jobs_status_available", "status", "available_at"),
        Index("ix_background_jobs_owner_created", "owner_user_id", "created_at"),
        Index("ix_background_jobs_resource", "owner_user_id", "resource_type", "resource_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_of_job_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackgroundJobEventModel(Base):
    """Durable ordered event emitted by a background job."""

    __tablename__ = "background_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_background_job_events_sequence"),
        Index("ix_background_job_events_owner_job_sequence", "owner_user_id", "job_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEventModel(Base):
    """A durable message waiting for publication from the canonical event log."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "sequence",
            "event_type",
            name="uq_outbox_events_aggregate_sequence_type",
        ),
        Index(
            "ix_outbox_events_unpublished_availability_lease",
            "published_at",
            "available_at",
            "claim_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserFeedbackModel(Base):
    """Owner-scoped feedback for a supported product artifact."""

    __tablename__ = "user_feedback"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "target_type",
            "target_id",
            "subject_key",
            name="uq_user_feedback_target",
        ),
        Index("ix_user_feedback_owner_target", "owner_user_id", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(160), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class McpConnectionModel(Base):
    """Owner-scoped MCP server connection configuration."""

    __tablename__ = "mcp_connections"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_mcp_connections_owner_name"),
        Index("ix_mcp_connections_owner_updated", "owner_user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    transport: Mapped[str] = mapped_column(String(40), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_tool_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeDocumentModel(Base):
    """Persisted knowledge base document metadata."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index(
            "ix_knowledge_documents_owner_kb_uploaded_at",
            "owner_user_id",
            "knowledge_base_id",
            "uploaded_at",
        ),
        Index(
            "ix_knowledge_documents_owner_kb_hash",
            "owner_user_id",
            "knowledge_base_id",
            "content_hash",
        ),
        Index(
            "ix_knowledge_documents_owner_kb_document",
            "owner_user_id",
            "knowledge_base_id",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(160), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    index_status: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentIndexTaskModel(Base):
    """Persisted document indexing task attempt."""

    __tablename__ = "document_index_tasks"
    __table_args__ = (
        Index(
            "ix_document_index_tasks_owner_document_created_at",
            "owner_user_id",
            "knowledge_base_id",
            "document_id",
            "created_at",
        ),
        Index("ix_document_index_tasks_owner_status", "owner_user_id", "status"),
        Index("ix_document_index_tasks_retry_of", "retry_of_task_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_of_task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatSessionModel(Base):
    """Persisted chat session."""

    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_owner_updated_at", "owner_user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(240), nullable=True)
    memory_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="every_30_turns")
    memory_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    compacted_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_compacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class UserChatConfigurationModel(Base):
    """Persisted owner-scoped chat assembly selection."""

    __tablename__ = "user_chat_configurations"

    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    system_prompt_id: Mapped[str] = mapped_column(String(120), nullable=False)
    skill_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class UserChatPromptModel(Base):
    """Persisted owner-scoped editable system prompt."""

    __tablename__ = "user_chat_prompts"
    __table_args__ = (
        Index("ix_user_chat_prompts_owner_updated_at", "owner_user_id", "updated_at"),
        Index("ix_user_chat_prompts_owner_default", "owner_user_id", "is_default"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class UserChatSkillModel(Base):
    """Persisted owner-scoped uploaded Skill markdown file."""

    __tablename__ = "user_chat_skills"
    __table_args__ = (
        Index("ix_user_chat_skills_owner_updated_at", "owner_user_id", "updated_at"),
        Index("ix_user_chat_skills_owner_filename", "owner_user_id", "filename"),
        UniqueConstraint("owner_user_id", "name", name="uq_user_chat_skills_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(240), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ChatMessageModel(Base):
    """Persisted chat message."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_created_at", "session_id", "created_at"),
        Index(
            "ix_chat_messages_owner_session_created_at",
            "owner_user_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class ChatAgentRunModel(Base):
    """Durable owner-scoped execution of one chat turn."""

    __tablename__ = "chat_agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "chat_session_id",
            "client_request_id",
            name="uq_chat_agent_runs_client_request",
        ),
        UniqueConstraint("user_message_id", name="uq_chat_agent_runs_user_message"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_chat_agent_runs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_chat_agent_runs_attempt_count"),
        CheckConstraint("last_event_sequence >= 0", name="ck_chat_agent_runs_last_event_sequence"),
        Index(
            "ix_chat_agent_runs_owner_session_status_updated",
            "owner_user_id",
            "chat_session_id",
            "status",
            "updated_at",
        ),
        Index(
            "uq_chat_agent_runs_assistant_message",
            "assistant_message_id",
            unique=True,
            postgresql_where=text("assistant_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chat_session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(120), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    user_message_id: Mapped[str] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    background_job_id: Mapped[str] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatRunEventModel(Base):
    """Public replayable event for a durable chat run."""

    __tablename__ = "chat_run_events"
    __table_args__ = (
        Index("ix_chat_run_events_owner_run_sequence", "owner_user_id", "run_id", "sequence"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    public_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatRunToolExecutionModel(Base):
    """Idempotency record for a logical tool call in a chat run."""

    __tablename__ = "chat_run_tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "chat_run_id",
            "logical_step",
            "tool_name",
            "arguments_fingerprint",
            name="uq_chat_run_tool_executions_logical_call",
        ),
        CheckConstraint(
            "status IN ('running','completed','failed','uncertain')",
            name="ck_chat_run_tool_executions_status",
        ),
        CheckConstraint("attempt_count >= 1", name="ck_chat_run_tool_executions_attempt_count"),
        Index("ix_chat_run_tool_executions_owner_run", "owner_user_id", "chat_run_id"),
    )

    tool_call_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chat_run_id: Mapped[str] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    logical_step: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    arguments_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    side_effecting: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    public_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecoveryApprovalRequestModel(Base):
    """Pending human approval request created by chat without execution authority."""

    __tablename__ = "aiops_recovery_approval_requests"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "diagnostic_task_id",
            "proposal_fingerprint",
            name="uq_aiops_recovery_approval_proposal",
        ),
        CheckConstraint("status = 'pending'", name="ck_aiops_recovery_approval_pending"),
        CheckConstraint(
            "execution_permitted = false",
            name="ck_aiops_recovery_approval_no_execution",
        ),
        CheckConstraint(
            "char_length(proposal_fingerprint) = 64",
            name="ck_aiops_recovery_approval_fingerprint",
        ),
        Index(
            "ix_aiops_recovery_approval_owner_created",
            "owner_user_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    diagnostic_task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"), nullable=False
    )
    proposal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    chat_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentToolCallAuditModel(Base):
    """Persisted tenant-scoped Agent tool invocation audit entry."""

    __tablename__ = "tool_call_audits"
    __table_args__ = (
        CheckConstraint(
            "(chat_session_id IS NOT NULL AND diagnostic_task_id IS NULL) OR "
            "(chat_session_id IS NULL AND diagnostic_task_id IS NOT NULL)",
            name="ck_tool_call_audits_one_parent",
        ),
        Index(
            "ix_tool_call_audits_owner_session_created_at",
            "owner_user_id",
            "chat_session_id",
            "created_at",
        ),
        Index(
            "ix_tool_call_audits_owner_diagnostic_created_at",
            "owner_user_id",
            "diagnostic_task_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    chat_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    diagnostic_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class DiagnosticTaskModel(Base):
    """Persisted AIOps diagnostic task."""

    __tablename__ = "aiops_diagnostic_tasks"
    __table_args__ = (
        Index("ix_aiops_diagnostic_tasks_created_at", "created_at"),
        Index("ix_aiops_diagnostic_tasks_owner_created_at", "owner_user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertIncidentModel(Base):
    """One owner-scoped Alertmanager group lifecycle."""

    __tablename__ = "aiops_alert_incidents"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'resolved')", name="ck_alert_incidents_status"),
        CheckConstraint("delivery_count >= 1", name="ck_alert_incidents_delivery_count"),
        CheckConstraint(
            "char_length(group_key_hash) = 64",
            name="ck_alert_incidents_group_key_hash",
        ),
        Index(
            "uq_aiops_alert_incidents_active_group",
            "owner_user_id",
            "source_id",
            "group_key_hash",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_aiops_alert_incidents_owner_status_updated",
            "owner_user_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    group_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    alert_name: Mapped[str] = mapped_column(String(256), nullable=False)
    service: Mapped[str] = mapped_column(String(256), nullable=False)
    severity: Mapped[str] = mapped_column(String(256), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    diagnostic_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlertEventModel(Base):
    """Deduplicated audit event for one authenticated Alertmanager delivery."""

    __tablename__ = "aiops_alert_events"
    __table_args__ = (
        CheckConstraint("status IN ('firing', 'resolved')", name="ck_alert_events_status"),
        CheckConstraint(
            "disposition IN ('incident_created', 'duplicate_updated', "
            "'incident_resolved', 'filtered', 'orphan_resolved')",
            name="ck_alert_events_disposition",
        ),
        CheckConstraint(
            "char_length(payload_sha256) = 64",
            name="ck_alert_events_payload_sha256",
        ),
        Index(
            "ix_aiops_alert_events_owner_source_received",
            "owner_user_id",
            "source_id",
            "received_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("aiops_alert_incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationRunModel(Base):
    """One immutable benchmark execution identity and its lifecycle."""

    __tablename__ = "aiops_evaluation_runs"
    __table_args__ = (
        Index("ix_aiops_evaluation_runs_scenario_created_at", "scenario_id", "created_at"),
        Index("ix_aiops_evaluation_runs_status_created_at", "status", "created_at"),
        Index("ix_aiops_evaluation_runs_kind_created_at", "evaluation_kind", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    evaluation_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="snapshot")
    artifact_schema_version: Mapped[str] = mapped_column(String(20), nullable=False, default="v1")
    artifact_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance: Mapped[str] = mapped_column(String(40), nullable=False, default="native")
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    scenario_id: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(80), nullable=False)
    agent_version: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    diagnostic_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationResultModel(Base):
    """Deterministic scorecard associated one-to-one with an evaluation run."""

    __tablename__ = "aiops_evaluation_results"

    result_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_evaluation_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    dimension_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validity: Mapped[str] = mapped_column(String(40), nullable=False)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failures: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    score_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    hard_gate: Mapped[str | None] = mapped_column(String(160), nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class DiagnosticReportModel(Base):
    """Persisted diagnostic report."""

    __tablename__ = "aiops_diagnostic_reports"
    __table_args__ = (
        Index("ix_aiops_diagnostic_reports_task_created_at", "task_id", "created_at"),
        Index(
            "ix_aiops_diagnostic_reports_owner_task_created_at",
            "owner_user_id",
            "task_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class DiagnosticCaseModel(Base):
    """Structured owner-scoped knowledge case created from a successful diagnosis."""

    __tablename__ = "aiops_diagnostic_cases"
    __table_args__ = (
        Index("ix_aiops_diagnostic_cases_owner_created_at", "owner_user_id", "created_at"),
        Index("ix_aiops_diagnostic_cases_owner_service", "owner_user_id", "service"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    report_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    index_task_id: Mapped[str] = mapped_column(
        ForeignKey("document_index_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    alert_name: Mapped[str] = mapped_column(String(240), nullable=False)
    service: Mapped[str] = mapped_column(String(240), nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    remediation: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class DiagnosticStepModel(Base):
    """Ordered plan and execution stage for an AIOps diagnostic."""

    __tablename__ = "aiops_diagnostic_steps"
    __table_args__ = (
        Index(
            "ix_aiops_diagnostic_steps_owner_task_sequence",
            "owner_user_id",
            "task_id",
            "sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class DiagnosticEvidenceModel(Base):
    """Typed, immutable evidence produced by an AIOps diagnostic."""

    __tablename__ = "aiops_diagnostic_evidence"
    __table_args__ = (
        Index(
            "ix_aiops_diagnostic_evidence_owner_task_created_at",
            "owner_user_id",
            "task_id",
            "created_at",
        ),
        Index(
            "ix_aiops_diagnostic_evidence_owner_task_kind",
            "owner_user_id",
            "task_id",
            "kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("aiops_diagnostic_steps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class ReportEvidenceLinkModel(Base):
    """Owner-scoped provenance edge from a diagnostic report to evidence."""

    __tablename__ = "aiops_report_evidence_links"
    __table_args__ = (
        Index(
            "ix_aiops_report_evidence_links_owner_report",
            "owner_user_id",
            "report_id",
        ),
        Index(
            "ix_aiops_report_evidence_links_owner_task_evidence",
            "owner_user_id",
            "task_id",
            "evidence_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_evidence.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class ToolCallAuditModel(Base):
    """Persisted tool call audit entry for a diagnostic task."""

    __tablename__ = "aiops_tool_call_audits"
    __table_args__ = (
        Index("ix_aiops_tool_call_audits_task_created_at", "task_id", "created_at"),
        Index(
            "ix_aiops_tool_call_audits_owner_task_created_at",
            "owner_user_id",
            "task_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class GraphCheckpointModel(Base):
    """Persisted LangGraph checkpoint for a diagnostic task."""

    __tablename__ = "aiops_graph_checkpoints"
    __table_args__ = (
        Index("ix_aiops_graph_checkpoints_task_thread", "task_id", "thread_id"),
        Index(
            "ix_aiops_graph_checkpoints_owner_task_thread",
            "owner_user_id",
            "task_id",
            "thread_id",
        ),
        Index(
            "ix_aiops_graph_checkpoints_identity",
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id: Mapped[str] = mapped_column(String(160), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(String(160), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(160), nullable=False)
    checkpoint_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class AiopsExecutionModel(Base):
    __tablename__ = "aiops_execution_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed','uncertain')",
            name="ck_aiops_execution_records_status",
        ),
        CheckConstraint(
            "execution_kind IN ('node','model','tool','recovery')",
            name="ck_aiops_execution_records_kind",
        ),
        Index(
            "ix_aiops_execution_records_scope",
            "owner_user_id",
            "task_id",
            "graph_version",
        ),
    )

    execution_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"), nullable=False
    )
    graph_version: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    node_name: Mapped[str] = mapped_column(String(120), nullable=False)
    logical_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    side_effecting: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AiopsLangGraphCheckpointModel(Base):
    __tablename__ = "aiops_langgraph_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_aiops_langgraph_checkpoints_identity",
        ),
        Index(
            "ix_aiops_langgraph_checkpoints_scope",
            "owner_user_id",
            "task_id",
            "graph_version",
            "thread_id",
        ),
    )
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(160), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"), nullable=False
    )
    graph_version: Mapped[str] = mapped_column(String(80), nullable=False)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    checkpoint_type: Mapped[str] = mapped_column(String(80), nullable=False)
    checkpoint_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_type: Mapped[str] = mapped_column(String(80), nullable=False)
    metadata_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AiopsLangGraphWriteModel(Base):
    __tablename__ = "aiops_langgraph_writes"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "write_task_id",
            "task_path",
            "write_index",
            name="uq_aiops_langgraph_writes_identity",
        ),
        Index(
            "ix_aiops_langgraph_writes_scope",
            "owner_user_id",
            "diagnostic_task_id",
            "graph_version",
            "thread_id",
        ),
    )
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(160), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    write_task_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    task_path: Mapped[str] = mapped_column(String(300), primary_key=True)
    write_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    diagnostic_task_id: Mapped[str] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(80), nullable=False)
    channel: Mapped[str] = mapped_column(String(160), nullable=False)
    value_type: Mapped[str] = mapped_column(String(80), nullable=False)
    value_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
