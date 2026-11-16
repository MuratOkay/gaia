"""Query runs (Phase 4A): every guarded statement, pinned to snapshot and semantics.

Creates ``chat.query_run``. Conversations and messages follow with the API
(Phase 4E) in their own migration.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "chat"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS chat")
    op.create_table(
        "query_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("dialect", sa.Text(), nullable=False),
        sa.Column("submitted_sql", sa.Text(), nullable=False),
        sa.Column("executed_sql", sa.Text(), nullable=True),
        sa.Column("guard", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("row_cap_reached", sa.Boolean(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("status IN ('rejected', 'ok', 'timeout', 'lock_timeout', 'error')",
                           name=op.f("ck_query_run_status")),
        sa.CheckConstraint("origin IN ('cli', 'api', 'golden', 'test')",
                           name=op.f("ck_query_run_origin")),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                name=op.f("fk_query_run_data_source_id_data_sources"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["catalog.snapshots.id"],
                                name=op.f("fk_query_run_snapshot_id_snapshots"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["chat.query_run.id"],
                                name=op.f("fk_query_run_parent_id_query_run"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_query_run")),
        schema=S,
    )
    op.create_index("ix_query_run_source_created", "query_run",
                    ["data_source_id", "created_at"], unique=False, schema=S)
    op.create_index("ix_query_run_parent", "query_run", ["parent_id"], unique=False, schema=S)


def downgrade() -> None:
    op.drop_index("ix_query_run_parent", table_name="query_run", schema=S)
    op.drop_index("ix_query_run_source_created", table_name="query_run", schema=S)
    op.drop_table("query_run", schema=S)
