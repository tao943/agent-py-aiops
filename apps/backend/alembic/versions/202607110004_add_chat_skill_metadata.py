"""add standard chat skill metadata

Revision ID: 202607110004
Revises: 202607110003
Create Date: 2026-07-11 22:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "202607110004"
down_revision: str | None = "202607110003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_SKILL_DESCRIPTION = (
    "由旧版聊天配置迁移的 Skill；请重新上传标准 SKILL.md 以补充准确描述。"
)


def upgrade() -> None:
    with op.batch_alter_table("user_chat_skills") as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("description", sa.String(length=1024), nullable=True))

    op.execute(
        sa.text(
            "UPDATE user_chat_skills "
            "SET name = 'legacy-skill-' || lower(right(id::text, 12)), "
            "description = :description, "
            "content = CASE "
            "WHEN regexp_replace(content, '^[[:space:]]*', '') LIKE '---%' "
            "THEN content "
            "ELSE '---' || E'\\nname: legacy-skill-' || lower(right(id::text, 12)) "
            "|| E'\\ndescription: ' || :description || E'\\n---\\n\\n' || content "
            "END"
        ).bindparams(sa.bindparam("description", LEGACY_SKILL_DESCRIPTION))
    )

    with op.batch_alter_table("user_chat_skills") as batch_op:
        batch_op.alter_column("name", existing_type=sa.String(length=64), nullable=False)
        batch_op.alter_column(
            "description", existing_type=sa.String(length=1024), nullable=False
        )
        batch_op.create_unique_constraint(
            "uq_user_chat_skills_owner_name", ["owner_user_id", "name"]
        )


def downgrade() -> None:
    with op.batch_alter_table("user_chat_skills") as batch_op:
        batch_op.drop_constraint("uq_user_chat_skills_owner_name", type_="unique")
        batch_op.drop_column("description")
        batch_op.drop_column("name")
