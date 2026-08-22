"""Add Live Eval alert correlation and independent verification state.

Revision ID: 202608220002
Revises: 202608220001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608220002"
down_revision: str | None = "202608220001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("aiops_alert_incidents", sa.Column("run_id", sa.String(80), nullable=True))
    op.add_column("aiops_alert_incidents", sa.Column("scenario_id", sa.String(96), nullable=True))
    op.add_column(
        "aiops_alert_incidents",
        sa.Column(
            "verification_status",
            sa.String(24),
            nullable=False,
            server_default="not_applicable",
        ),
    )
    op.add_column(
        "aiops_alert_incidents",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "aiops_alert_incidents",
        sa.Column("verification_summary", sa.String(512), nullable=True),
    )
    op.create_check_constraint(
        "ck_alert_incidents_verification_status",
        "aiops_alert_incidents",
        "verification_status IN ('pending', 'passed', 'failed', 'not_applicable')",
    )
    op.create_index(
        "ix_aiops_alert_incidents_live_correlation",
        "aiops_alert_incidents",
        ["owner_user_id", "source_id", "scenario_id", "run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_aiops_alert_incidents_live_correlation",
        table_name="aiops_alert_incidents",
    )
    op.drop_constraint(
        "ck_alert_incidents_verification_status",
        "aiops_alert_incidents",
        type_="check",
    )
    op.drop_column("aiops_alert_incidents", "verification_summary")
    op.drop_column("aiops_alert_incidents", "verified_at")
    op.drop_column("aiops_alert_incidents", "verification_status")
    op.drop_column("aiops_alert_incidents", "scenario_id")
    op.drop_column("aiops_alert_incidents", "run_id")
