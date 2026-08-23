"""Add durable chat agent runs, events, tool executions, and approvals.

Revision ID: 202608220004
Revises: 202608220003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608220004"
down_revision: str | None = "202608220003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_agent_runs",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_session_id",
            sa.String(80),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_request_id", sa.String(120), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "user_message_id",
            sa.String(80),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_message_id",
            sa.String(80),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "background_job_id",
            sa.String(80),
            sa.ForeignKey("background_jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "owner_user_id",
            "chat_session_id",
            "client_request_id",
            name="uq_chat_agent_runs_client_request",
        ),
        sa.UniqueConstraint("user_message_id", name="uq_chat_agent_runs_user_message"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_chat_agent_runs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_chat_agent_runs_attempt_count"),
        sa.CheckConstraint(
            "last_event_sequence >= 0", name="ck_chat_agent_runs_last_event_sequence"
        ),
    )
    op.create_index(
        "ix_chat_agent_runs_owner_session_status_updated",
        "chat_agent_runs",
        ["owner_user_id", "chat_session_id", "status", "updated_at"],
    )
    op.create_index(
        "uq_chat_agent_runs_assistant_message",
        "chat_agent_runs",
        ["assistant_message_id"],
        unique=True,
        postgresql_where=sa.text("assistant_message_id IS NOT NULL"),
    )
    op.create_table(
        "chat_run_events",
        sa.Column(
            "run_id",
            sa.String(80),
            sa.ForeignKey("chat_agent_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("public_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_chat_run_events_owner_run_sequence",
        "chat_run_events",
        ["owner_user_id", "run_id", "sequence"],
    )
    op.create_table(
        "chat_run_tool_executions",
        sa.Column("tool_call_key", sa.String(64), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_run_id",
            sa.String(80),
            sa.ForeignKey("chat_agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("logical_step", sa.String(120), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("arguments_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("side_effecting", sa.Boolean(), nullable=False),
        sa.Column("outcome_known", sa.Boolean(), nullable=False),
        sa.Column("public_result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("safe_error_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "chat_run_id",
            "logical_step",
            "tool_name",
            "arguments_fingerprint",
            name="uq_chat_run_tool_executions_logical_call",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','uncertain')",
            name="ck_chat_run_tool_executions_status",
        ),
        sa.CheckConstraint("attempt_count >= 1", name="ck_chat_run_tool_executions_attempt_count"),
    )
    op.create_index(
        "ix_chat_run_tool_executions_owner_run",
        "chat_run_tool_executions",
        ["owner_user_id", "chat_run_id"],
    )
    op.create_table(
        "aiops_recovery_approval_requests",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "diagnostic_task_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposal_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_reason", sa.String(1000), nullable=False),
        sa.Column(
            "chat_run_id",
            sa.String(80),
            sa.ForeignKey("chat_agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("execution_permitted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id",
            "diagnostic_task_id",
            "proposal_fingerprint",
            name="uq_aiops_recovery_approval_proposal",
        ),
        sa.CheckConstraint("status = 'pending'", name="ck_aiops_recovery_approval_pending"),
        sa.CheckConstraint(
            "execution_permitted = false", name="ck_aiops_recovery_approval_no_execution"
        ),
        sa.CheckConstraint(
            "char_length(proposal_fingerprint) = 64", name="ck_aiops_recovery_approval_fingerprint"
        ),
    )
    op.create_index(
        "ix_aiops_recovery_approval_owner_created",
        "aiops_recovery_approval_requests",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("aiops_recovery_approval_requests")
    op.drop_table("chat_run_tool_executions")
    op.drop_table("chat_run_events")
    op.drop_table("chat_agent_runs")
