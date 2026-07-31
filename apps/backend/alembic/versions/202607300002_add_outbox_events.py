"""add transactional outbox events

Revision ID: 202607300002
Revises: 202607300001
Create Date: 2026-07-30 00:02:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202607300002"
down_revision: str | None = "202607300001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=80), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", sa.String(length=80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=160), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "sequence",
            "event_type",
            name="uq_outbox_events_aggregate_sequence_type",
        ),
    )
    op.create_index("ix_outbox_events_owner_user_id", "outbox_events", ["owner_user_id"])
    op.create_index(
        "ix_outbox_events_unpublished_availability_lease",
        "outbox_events",
        ["published_at", "available_at", "claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
