"""Allow LangGraph pending writes before their checkpoint.

Revision ID: 202608190002
Revises: 202608190001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202608190002"
down_revision: str | None = "202608190001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WRITES_CHECKPOINT_FK = (
    "aiops_langgraph_writes_thread_id_checkpoint_ns_checkpoint__fkey"
)


def upgrade() -> None:
    op.drop_constraint(
        _WRITES_CHECKPOINT_FK,
        "aiops_langgraph_writes",
        type_="foreignkey",
    )


def downgrade() -> None:
    # PostgreSQL rejects this safely if pending writes without checkpoints exist.
    # Downgrade never deletes or rewrites those records implicitly.
    op.create_foreign_key(
        _WRITES_CHECKPOINT_FK,
        "aiops_langgraph_writes",
        "aiops_langgraph_checkpoints",
        ["thread_id", "checkpoint_ns", "checkpoint_id"],
        ["thread_id", "checkpoint_ns", "checkpoint_id"],
        ondelete="CASCADE",
    )
