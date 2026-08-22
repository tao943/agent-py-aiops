"""Persist chat memory compaction lifecycle status.

Revision ID: 202608220005
Revises: 202608220004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608220005"
down_revision: str | None = "202608220004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "memory_compaction_status",
            sa.String(24),
            nullable=False,
            server_default="idle",
        ),
    )
    op.create_check_constraint(
        "ck_chat_sessions_memory_compaction_status",
        "chat_sessions",
        "memory_compaction_status IN ('idle','queued','running','degraded')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_chat_sessions_memory_compaction_status",
        "chat_sessions",
        type_="check",
    )
    op.drop_column("chat_sessions", "memory_compaction_status")
