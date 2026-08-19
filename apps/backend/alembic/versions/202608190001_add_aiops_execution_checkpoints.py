"""Add idempotent AIOps execution and LangGraph checkpoint storage.

Revision ID: 202608190001
Revises: 202608170001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608190001"
down_revision: str | None = "202608170001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aiops_execution_records",
        sa.Column("execution_key", sa.String(200), primary_key=True),
        sa.Column("owner_user_id", sa.String(80), nullable=False),
        sa.Column(
            "task_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("execution_kind", sa.String(20), nullable=False),
        sa.Column("node_name", sa.String(120), nullable=False),
        sa.Column("logical_iteration", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("side_effecting", sa.Boolean(), nullable=False),
        sa.Column("outcome_known", sa.Boolean(), nullable=False),
        sa.Column(
            "output_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("safe_error_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','uncertain')",
            name="ck_aiops_execution_records_status",
        ),
        sa.CheckConstraint(
            "execution_kind IN ('node','model','tool','recovery')",
            name="ck_aiops_execution_records_kind",
        ),
    )
    op.create_index(
        "ix_aiops_execution_records_scope",
        "aiops_execution_records",
        ["owner_user_id", "task_id", "graph_version"],
    )
    op.create_table(
        "aiops_langgraph_checkpoints",
        sa.Column("thread_id", sa.String(200), primary_key=True),
        sa.Column("checkpoint_ns", sa.String(160), primary_key=True),
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column("owner_user_id", sa.String(80), nullable=False),
        sa.Column(
            "task_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(160), nullable=True),
        sa.Column("checkpoint_type", sa.String(80), nullable=False),
        sa.Column("checkpoint_blob", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_type", sa.String(80), nullable=False),
        sa.Column("metadata_blob", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_aiops_langgraph_checkpoints_identity",
        ),
    )
    op.create_index(
        "ix_aiops_langgraph_checkpoints_scope",
        "aiops_langgraph_checkpoints",
        ["owner_user_id", "task_id", "graph_version", "thread_id"],
    )
    op.create_table(
        "aiops_langgraph_writes",
        sa.Column("thread_id", sa.String(200), primary_key=True),
        sa.Column("checkpoint_ns", sa.String(160), primary_key=True),
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column("write_task_id", sa.String(160), primary_key=True),
        sa.Column("task_path", sa.String(300), primary_key=True),
        sa.Column("write_index", sa.Integer(), primary_key=True),
        sa.Column(
            "diagnostic_task_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.String(80), nullable=False),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("channel", sa.String(160), nullable=False),
        sa.Column("value_type", sa.String(80), nullable=False),
        sa.Column("value_blob", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id", "checkpoint_ns", "checkpoint_id"],
            [
                "aiops_langgraph_checkpoints.thread_id",
                "aiops_langgraph_checkpoints.checkpoint_ns",
                "aiops_langgraph_checkpoints.checkpoint_id",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "write_task_id",
            "task_path",
            "write_index",
            name="uq_aiops_langgraph_writes_identity",
        ),
    )
    op.create_index(
        "ix_aiops_langgraph_writes_scope",
        "aiops_langgraph_writes",
        ["owner_user_id", "diagnostic_task_id", "graph_version", "thread_id"],
    )


def downgrade() -> None:
    op.drop_table("aiops_langgraph_writes")
    op.drop_table("aiops_langgraph_checkpoints")
    op.drop_table("aiops_execution_records")
