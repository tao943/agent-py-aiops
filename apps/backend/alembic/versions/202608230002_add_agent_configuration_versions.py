"""Add versioned Agent configuration and immutable run snapshots.

Revision ID: 202608230002
Revises: 202608230001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608230002"
down_revision: str | None = "202608230001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_config_resources",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(1024), nullable=False, server_default=""),
        sa.Column("legacy_resource_id", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('prompt','skill')", name="ck_agent_config_resources_kind"),
        sa.UniqueConstraint(
            "owner_user_id", "kind", "name", name="uq_agent_config_resources_owner_kind_name"
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "kind",
            "legacy_resource_id",
            name="uq_agent_config_resources_legacy",
        ),
    )
    op.create_index(
        "ix_agent_config_resources_owner_kind_updated",
        "agent_config_resources",
        ["owner_user_id", "kind", "updated_at"],
    )
    op.create_index(
        "ix_agent_config_resources_owner_user_id",
        "agent_config_resources",
        ["owner_user_id"],
    )

    op.create_table(
        "agent_config_versions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "resource_id",
            sa.String(80),
            sa.ForeignKey("agent_config_resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "validation_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','published','deprecated')",
            name="ck_agent_config_versions_status",
        ),
        sa.UniqueConstraint("resource_id", "version", name="uq_agent_config_versions_number"),
    )
    op.create_index(
        "ix_agent_config_versions_owner_resource",
        "agent_config_versions",
        ["owner_user_id", "resource_id", "version"],
    )
    op.create_index(
        "ix_agent_config_versions_owner_user_id",
        "agent_config_versions",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_agent_config_versions_resource_id",
        "agent_config_versions",
        ["resource_id"],
    )
    op.create_index(
        "uq_agent_config_versions_one_draft",
        "agent_config_versions",
        ["resource_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )

    op.create_table(
        "agent_config_bindings",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node", sa.String(48), nullable=False),
        sa.Column(
            "prompt_version_id",
            sa.String(80),
            sa.ForeignKey("agent_config_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("skill_version_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "node IN ('conversation','planner','replanner','investigator_runtime',"
            "'investigator_log','investigator_change','adjudicator','validator',"
            "'recovery_planner','report')",
            name="ck_agent_config_bindings_node",
        ),
        sa.UniqueConstraint("owner_user_id", "node", name="uq_agent_config_bindings_owner_node"),
    )
    op.create_index(
        "ix_agent_config_bindings_owner_updated",
        "agent_config_bindings",
        ["owner_user_id", "updated_at"],
    )
    op.create_index(
        "ix_agent_config_bindings_owner_user_id",
        "agent_config_bindings",
        ["owner_user_id"],
    )

    op.create_table(
        "agent_config_audit_events",
        sa.Column("event_id", sa.String(80), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(80),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.String(80), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("resource_id", sa.String(80), nullable=True),
        sa.Column("version_id", sa.String(80), nullable=True),
        sa.Column("node", sa.String(48), nullable=True),
        sa.Column("safe_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_config_audit_owner_created",
        "agent_config_audit_events",
        ["owner_user_id", "created_at", "event_id"],
    )
    op.create_index(
        "ix_agent_config_audit_events_owner_user_id",
        "agent_config_audit_events",
        ["owner_user_id"],
    )

    empty_object = sa.text("'{}'::jsonb")
    op.add_column(
        "chat_agent_runs",
        sa.Column(
            "agent_configuration_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_object,
        ),
    )
    op.add_column(
        "aiops_diagnostic_tasks",
        sa.Column(
            "agent_configuration_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_object,
        ),
    )
    _migrate_legacy_chat_configuration()


def _migrate_legacy_chat_configuration() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO agent_config_resources
                (id, owner_user_id, kind, name, description, legacy_resource_id,
                 created_at, updated_at)
            SELECT 'acres_prompt_' || md5(owner_user_id || ':' || id), owner_user_id,
                   'prompt', label, '兼容迁移的 Chat Prompt', id, created_at, updated_at
            FROM user_chat_prompts
            ON CONFLICT (owner_user_id, kind, legacy_resource_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO agent_config_resources
                (id, owner_user_id, kind, name, description, legacy_resource_id,
                 created_at, updated_at)
            SELECT 'acres_skill_' || md5(owner_user_id || ':' || id), owner_user_id,
                   'skill', name, description, id, created_at, updated_at
            FROM user_chat_skills
            ON CONFLICT (owner_user_id, kind, legacy_resource_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO agent_config_versions
                (id, resource_id, owner_user_id, version, status, content, spec,
                 content_sha256, validation_warnings, created_at, updated_at, published_at)
            SELECT 'acver_prompt_' || md5(p.owner_user_id || ':' || p.id), r.id,
                   p.owner_user_id, 1, 'published', p.content,
                   jsonb_build_object(
                     'bindableNodes', jsonb_build_array('conversation'),
                     'imported', true
                   ),
                   md5(p.content), '[]'::jsonb, p.created_at, p.updated_at, p.updated_at
            FROM user_chat_prompts p
            JOIN agent_config_resources r
              ON r.owner_user_id = p.owner_user_id AND r.kind = 'prompt'
             AND r.legacy_resource_id = p.id
            ON CONFLICT (resource_id, version) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO agent_config_versions
                (id, resource_id, owner_user_id, version, status, content, spec,
                 content_sha256, validation_warnings, created_at, updated_at, published_at)
            SELECT 'acver_skill_' || md5(s.owner_user_id || ':' || s.id), r.id,
                   s.owner_user_id, 1, 'published', s.content,
                   jsonb_build_object(
                     'bindableNodes', jsonb_build_array('conversation'),
                     'allowedTools', '[]'::jsonb,
                     'risk', 'read_only',
                     'inputSchema', '{}'::jsonb,
                     'outputSchema', '{}'::jsonb,
                     'timeoutMs', 30000,
                     'maxToolCalls', 8,
                     'retryPolicy', 'none',
                     'requiresApprovalFor', '[]'::jsonb,
                     'completionCriteria', '[]'::jsonb,
                     'imported', true
                   ),
                   md5(s.content), '[]'::jsonb, s.created_at, s.updated_at, s.updated_at
            FROM user_chat_skills s
            JOIN agent_config_resources r
              ON r.owner_user_id = s.owner_user_id AND r.kind = 'skill'
             AND r.legacy_resource_id = s.id
            ON CONFLICT (resource_id, version) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO agent_config_bindings
                (id, owner_user_id, node, prompt_version_id, skill_version_ids,
                 created_at, updated_at)
            SELECT 'acbind_' || md5(c.owner_user_id || chr(58) || 'conversation'),
                   c.owner_user_id,
                   'conversation', pv.id,
                   COALESCE((
                     SELECT jsonb_agg(sv.id ORDER BY selected.ordinality)
                     FROM jsonb_array_elements_text(c.skill_ids)
                          WITH ORDINALITY AS selected(legacy_id, ordinality)
                     JOIN agent_config_resources sr
                       ON sr.owner_user_id = c.owner_user_id AND sr.kind = 'skill'
                      AND sr.legacy_resource_id = selected.legacy_id
                     JOIN agent_config_versions sv
                       ON sv.resource_id = sr.id AND sv.version = 1
                   ), '[]'::jsonb), c.created_at, c.updated_at
            FROM user_chat_configurations c
            JOIN agent_config_resources pr
              ON pr.owner_user_id = c.owner_user_id AND pr.kind = 'prompt'
             AND pr.legacy_resource_id = c.system_prompt_id
            JOIN agent_config_versions pv ON pv.resource_id = pr.id AND pv.version = 1
            ON CONFLICT (owner_user_id, node) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_column("aiops_diagnostic_tasks", "agent_configuration_snapshot")
    op.drop_column("chat_agent_runs", "agent_configuration_snapshot")
    op.drop_table("agent_config_audit_events")
    op.drop_table("agent_config_bindings")
    op.drop_table("agent_config_versions")
    op.drop_table("agent_config_resources")
