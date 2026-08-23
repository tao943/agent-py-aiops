"""Add durable pending chat actions.

Revision ID: 202608220005
Revises: 202608220004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608220005"
down_revision: str | None = "202608220004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_chat_actions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(80),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_run_id",
            sa.String(80),
            sa.ForeignKey("chat_agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action_type", sa.String(40), nullable=False),
        sa.Column("target_resource_id", sa.String(160), nullable=False),
        sa.Column(
            "public_arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("action_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_result_id", sa.String(160), nullable=True),
        sa.Column(
            "background_job_id",
            sa.String(80),
            sa.ForeignKey("background_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('start_diagnostic','create_recovery_approval')",
            name="ck_pending_chat_actions_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','executed','cancelled','expired','manual_review')",
            name="ck_pending_chat_actions_status",
        ),
        sa.CheckConstraint(
            "char_length(action_fingerprint) = 64",
            name="ck_pending_chat_actions_fingerprint",
        ),
    )
    op.create_index(
        "ix_pending_chat_actions_owner_session_status",
        "pending_chat_actions",
        ["owner_user_id", "session_id", "status", "created_at"],
    )
    op.create_index(
        "uq_pending_chat_actions_active_fingerprint",
        "pending_chat_actions",
        ["owner_user_id", "action_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','confirmed')"),
    )


def downgrade() -> None:
    op.drop_table("pending_chat_actions")
