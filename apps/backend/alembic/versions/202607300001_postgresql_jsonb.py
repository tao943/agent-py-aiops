"""convert relational JSON columns to PostgreSQL JSONB

Revision ID: 202607300001
Revises: 202607110007
Create Date: 2026-07-30 00:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202607300001"
down_revision: str | None = "202607110007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("background_jobs", "payload"),
    ("background_job_events", "payload"),
    ("mcp_connections", "last_tools"),
    ("knowledge_documents", "metadata"),
    ("user_chat_configurations", "skill_ids"),
    ("chat_messages", "metadata"),
    ("tool_call_audits", "arguments"),
    ("aiops_diagnostic_tasks", "input_payload"),
    ("aiops_diagnostic_tasks", "result_payload"),
    ("aiops_diagnostic_reports", "payload"),
    ("aiops_diagnostic_cases", "keywords"),
    ("aiops_diagnostic_cases", "evidence_ids"),
    ("aiops_diagnostic_steps", "payload"),
    ("aiops_diagnostic_evidence", "payload"),
    ("aiops_tool_call_audits", "arguments"),
    ("aiops_tool_call_audits", "result_payload"),
    ("aiops_graph_checkpoints", "checkpoint_payload"),
    ("aiops_graph_checkpoints", "metadata"),
)


def upgrade() -> None:
    for table_name, column_name in JSON_COLUMNS:
        op.alter_column(
            table_name,
            column_name,
            type_=postgresql.JSONB(astext_type=sa.Text()),
            postgresql_using=f'"{column_name}"::jsonb',
        )


def downgrade() -> None:
    for table_name, column_name in JSON_COLUMNS:
        op.alter_column(
            table_name,
            column_name,
            type_=sa.JSON(),
            postgresql_using=f'"{column_name}"::json',
        )
