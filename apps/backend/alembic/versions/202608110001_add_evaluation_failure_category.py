"""add evaluation failure category

Revision ID: 202608110001
Revises: 202608100001
Create Date: 2026-08-11 00:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "202608110001"
down_revision: str | None = "202608100001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aiops_evaluation_runs",
        sa.Column("failure_category", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("aiops_evaluation_runs", "failure_category")
