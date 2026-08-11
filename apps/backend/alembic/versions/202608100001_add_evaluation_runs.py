"""add AgentPy benchmark evaluation runs and results

Revision ID: 202608100001
Revises: 202607300002
Create Date: 2026-08-10 00:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202608100001"
down_revision: str | None = "202607300002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aiops_evaluation_runs",
        sa.Column("run_id", sa.String(length=80), primary_key=True),
        sa.Column("scenario_id", sa.String(length=80), nullable=False),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("suite_version", sa.String(length=80), nullable=False),
        sa.Column(
            "agent_version",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "model_configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "diagnostic_task_id",
            sa.String(length=80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_aiops_evaluation_runs_scenario_created_at",
        "aiops_evaluation_runs",
        ["scenario_id", "created_at"],
    )
    op.create_index(
        "ix_aiops_evaluation_runs_status_created_at",
        "aiops_evaluation_runs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_aiops_evaluation_runs_diagnostic_task_id",
        "aiops_evaluation_runs",
        ["diagnostic_task_id"],
    )

    op.create_table(
        "aiops_evaluation_results",
        sa.Column("result_id", sa.String(length=80), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=80),
            sa.ForeignKey("aiops_evaluation_runs.run_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "dimension_scores",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("raw_total", sa.Integer(), nullable=False),
        sa.Column("validity", sa.String(length=40), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hard_gate", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("aiops_evaluation_results")
    op.drop_table("aiops_evaluation_runs")
