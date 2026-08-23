"""Add versioned structured chat memory.

Revision ID: 202608220006
Revises: 202608220005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202608220006"
down_revision: str | None = "202608220005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "structured_memory",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "memory_summary_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("memory_through_message_id", sa.String(80), nullable=True),
    )
    op.execute(
        "UPDATE chat_sessions SET memory_mode='adaptive' "
        "WHERE memory_mode IN ('every_30_turns','context_70_percent')"
    )
    op.alter_column(
        "chat_sessions",
        "memory_mode",
        existing_type=sa.String(40),
        existing_nullable=False,
        server_default="adaptive",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE chat_sessions SET memory_mode='every_30_turns' "
        "WHERE memory_mode='adaptive'"
    )
    op.alter_column(
        "chat_sessions",
        "memory_mode",
        existing_type=sa.String(40),
        existing_nullable=False,
        server_default="every_30_turns",
    )
    op.drop_column("chat_sessions", "memory_through_message_id")
    op.drop_column("chat_sessions", "memory_summary_version")
    op.drop_column("chat_sessions", "structured_memory")
