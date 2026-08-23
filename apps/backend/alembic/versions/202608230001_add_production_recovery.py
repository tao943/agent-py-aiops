"""Add governed production recovery state.

Revision ID: 202608230001
Revises: 202608220007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608230001"
down_revision: str | None = "202608220007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "production_recovery_intents",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "incident_id",
            sa.String(80),
            sa.ForeignKey("aiops_alert_incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "diagnostic_task_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            sa.String(80),
            sa.ForeignKey("aiops_diagnostic_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("target_key", sa.String(96), nullable=False),
        sa.Column("canonical_arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("proposal_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validator_origin", sa.String(80), nullable=False),
        sa.Column("policy_authorization_code", sa.String(120), nullable=False),
        sa.Column("risk_tier", sa.String(16), nullable=False),
        sa.Column("automatic_eligible", sa.Boolean(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("execution_key", sa.String(64), nullable=True),
        sa.Column(
            "background_job_id",
            sa.String(80),
            sa.ForeignKey("background_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trusted_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execution_summary", sa.String(512), nullable=True),
        sa.Column("verification_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("safe_reason_code", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('restart_compose_service','terminate_postgres_blocker')",
            name="ck_production_recovery_intents_action",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('low','high')", name="ck_production_recovery_intents_risk"
        ),
        sa.CheckConstraint(
            "status IN ('proposed','awaiting_approval','queued','revalidating',"
            "'executing','verifying','recovered','denied','rejected','expired',"
            "'cancelled','verification_failed','manual_intervention')",
            name="ck_production_recovery_intents_status",
        ),
        sa.CheckConstraint(
            "char_length(proposal_fingerprint) = 64",
            name="ck_production_recovery_intents_proposal_fingerprint",
        ),
        sa.CheckConstraint(
            "execution_key IS NULL OR char_length(execution_key) = 64",
            name="ck_production_recovery_intents_execution_key",
        ),
    )
    op.create_index(
        "ix_production_recovery_intents_owner_incident_created",
        "production_recovery_intents",
        ["owner_user_id", "incident_id", "created_at"],
    )
    op.create_index(
        "ix_production_recovery_intents_background_job_id",
        "production_recovery_intents",
        ["background_job_id"],
    )
    op.create_index(
        "uq_production_recovery_intents_active_proposal",
        "production_recovery_intents",
        ["owner_user_id", "proposal_fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('proposed','awaiting_approval','queued','revalidating',"
            "'executing','verifying')"
        ),
    )

    op.create_table(
        "production_recovery_approvals",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "intent_id",
            sa.String(80),
            sa.ForeignKey("production_recovery_intents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approver_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("incident_id", sa.String(80), nullable=False),
        sa.Column("proposal_fingerprint", sa.String(64), nullable=False),
        sa.Column("confirmation_fingerprint", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')", name="ck_production_recovery_approvals_decision"
        ),
        sa.CheckConstraint(
            "char_length(proposal_fingerprint) = 64",
            name="ck_production_recovery_approvals_proposal_fingerprint",
        ),
        sa.UniqueConstraint(
            "intent_id", "proposal_fingerprint", name="uq_production_recovery_approval_proposal"
        ),
    )
    op.create_index(
        "ix_production_recovery_approvals_owner_user_id",
        "production_recovery_approvals",
        ["owner_user_id"],
    )

    op.create_table(
        "production_recovery_audit_events",
        sa.Column("event_id", sa.String(80), primary_key=True),
        sa.Column(
            "intent_id",
            sa.String(80),
            sa.ForeignKey("production_recovery_intents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("safe_reason_code", sa.String(120), nullable=True),
        sa.Column("safe_summary", sa.String(512), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intent_id", "sequence", name="uq_production_recovery_audit_sequence"),
    )
    op.create_index(
        "ix_production_recovery_audit_owner_intent_sequence",
        "production_recovery_audit_events",
        ["owner_user_id", "intent_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_table("production_recovery_audit_events")
    op.drop_table("production_recovery_approvals")
    op.drop_table("production_recovery_intents")
