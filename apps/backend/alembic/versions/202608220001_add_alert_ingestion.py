"""Add Alertmanager incident and event ingestion storage.

Revision ID: 202608220001
Revises: 202608190002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608220001"
down_revision: str | None = "202608190002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aiops_alert_incidents",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("group_key_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("alert_name", sa.String(256), nullable=False),
        sa.Column("service", sa.String(256), nullable=False),
        sa.Column("severity", sa.String(256), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_count", sa.Integer(), nullable=False),
        sa.Column(
            "diagnostic_task_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'resolved')", name="ck_alert_incidents_status"),
        sa.CheckConstraint("delivery_count >= 1", name="ck_alert_incidents_delivery_count"),
        sa.CheckConstraint(
            "char_length(group_key_hash) = 64",
            name="ck_alert_incidents_group_key_hash",
        ),
    )
    op.create_index(
        "uq_aiops_alert_incidents_active_group",
        "aiops_alert_incidents",
        ["owner_user_id", "source_id", "group_key_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_aiops_alert_incidents_owner_status_updated",
        "aiops_alert_incidents",
        ["owner_user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_aiops_alert_incidents_diagnostic_task_id",
        "aiops_alert_incidents",
        ["diagnostic_task_id"],
    )
    op.create_table(
        "aiops_alert_events",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "incident_id",
            sa.String(80),
            sa.ForeignKey("aiops_alert_incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column(
            "normalized_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('firing', 'resolved')", name="ck_alert_events_status"),
        sa.CheckConstraint(
            "disposition IN ('incident_created', 'duplicate_updated', "
            "'incident_resolved', 'filtered', 'orphan_resolved')",
            name="ck_alert_events_disposition",
        ),
        sa.CheckConstraint(
            "char_length(payload_sha256) = 64",
            name="ck_alert_events_payload_sha256",
        ),
    )
    op.create_index(
        "ix_aiops_alert_events_owner_source_received",
        "aiops_alert_events",
        ["owner_user_id", "source_id", "received_at"],
    )
    op.create_index(
        "ix_aiops_alert_events_incident_id",
        "aiops_alert_events",
        ["incident_id"],
    )


def downgrade() -> None:
    op.drop_table("aiops_alert_events")
    op.drop_table("aiops_alert_incidents")

