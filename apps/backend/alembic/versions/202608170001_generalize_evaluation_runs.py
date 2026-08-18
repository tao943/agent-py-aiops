"""generalize evaluation runs for durable history

Revision ID: 202608170001
Revises: 202608110001
Create Date: 2026-08-17 00:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202608170001"
down_revision: str | None = "202608110001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    empty_object = sa.text("'{}'::jsonb")
    op.add_column(
        "aiops_evaluation_runs",
        sa.Column(
            "evaluation_kind", sa.String(length=40), nullable=False, server_default="snapshot"
        ),
    )
    op.add_column(
        "aiops_evaluation_runs",
        sa.Column(
            "artifact_schema_version", sa.String(length=20), nullable=False, server_default="v1"
        ),
    )
    op.add_column(
        "aiops_evaluation_runs",
        sa.Column("artifact_checksum", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "aiops_evaluation_runs",
        sa.Column("provenance", sa.String(length=40), nullable=False, server_default="native"),
    )
    op.add_column(
        "aiops_evaluation_runs",
        sa.Column(
            "run_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_object,
        ),
    )
    op.add_column(
        "aiops_evaluation_results",
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_object,
        ),
    )
    op.add_column(
        "aiops_evaluation_results",
        sa.Column(
            "result_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_object,
        ),
    )
    op.alter_column("aiops_evaluation_results", "total", nullable=True)
    op.alter_column("aiops_evaluation_results", "raw_total", nullable=True)
    op.alter_column("aiops_evaluation_results", "passed", nullable=True)

    op.execute(
        """
        UPDATE aiops_evaluation_runs AS runs
        SET status = CASE
            WHEN runs.status = 'completed' AND results.passed IS TRUE THEN 'passed'
            WHEN runs.status = 'completed' AND results.passed IS FALSE THEN 'failed'
            WHEN runs.status = 'infra_failed' THEN 'infra_invalid'
            WHEN runs.status = 'pending' THEN 'running'
            ELSE runs.status
        END
        FROM aiops_evaluation_results AS results
        WHERE results.run_id = runs.run_id
        """
    )
    op.execute(
        """
        UPDATE aiops_evaluation_runs
        SET status = CASE
            WHEN status = 'infra_failed' THEN 'infra_invalid'
            WHEN status = 'pending' THEN 'running'
            ELSE status
        END
        """
    )
    op.create_index(
        "ix_aiops_evaluation_runs_kind_created_at",
        "aiops_evaluation_runs",
        ["evaluation_kind", "created_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    non_snapshot = connection.execute(
        sa.text(
            "SELECT count(*) FROM aiops_evaluation_runs "
            "WHERE evaluation_kind <> 'snapshot'"
        )
    ).scalar_one()
    if non_snapshot:
        raise RuntimeError("Cannot downgrade while non-Snapshot evaluation history exists.")

    op.execute(
        """
        UPDATE aiops_evaluation_runs
        SET status = CASE
            WHEN status IN ('passed', 'failed') THEN 'completed'
            WHEN status = 'infra_invalid' THEN 'infra_failed'
            WHEN status = 'running' THEN 'pending'
            ELSE status
        END
        """
    )
    op.drop_index("ix_aiops_evaluation_runs_kind_created_at", table_name="aiops_evaluation_runs")
    op.alter_column("aiops_evaluation_results", "passed", nullable=False)
    op.alter_column("aiops_evaluation_results", "raw_total", nullable=False)
    op.alter_column("aiops_evaluation_results", "total", nullable=False)
    op.drop_column("aiops_evaluation_results", "result_payload")
    op.drop_column("aiops_evaluation_results", "metrics")
    op.drop_column("aiops_evaluation_runs", "run_metadata")
    op.drop_column("aiops_evaluation_runs", "provenance")
    op.drop_column("aiops_evaluation_runs", "artifact_checksum")
    op.drop_column("aiops_evaluation_runs", "artifact_schema_version")
    op.drop_column("aiops_evaluation_runs", "evaluation_kind")
