"""Add resumable state for Live automatic incident closure.

Revision ID: 202608220003
Revises: 202608220002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608220003"
down_revision: str | None = "202608220002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aiops_live_auto_closure_states",
        sa.Column("owner_user_id", sa.String(80), nullable=False),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("scenario_id", sa.String(96), nullable=False),
        sa.Column("run_id", sa.String(96), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column(
            "state_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('baseline_ready','fault_injected','alert_detected',"
            "'diagnosis_completed','recovery_dispatched','recovery_completed',"
            "'verification_recorded','resolved')",
            name="ck_live_auto_closure_states_stage",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_live_auto_closure_states_version",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            "source_id",
            "scenario_id",
            "run_id",
            name="pk_aiops_live_auto_closure_states",
        ),
    )
    op.create_index(
        "ix_live_auto_closure_states_updated_at",
        "aiops_live_auto_closure_states",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_auto_closure_states_updated_at",
        table_name="aiops_live_auto_closure_states",
    )
    op.drop_table("aiops_live_auto_closure_states")
