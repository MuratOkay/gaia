"""Conversations (Phase 4E): ``chat.conversation`` and ``chat.message``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "chat"
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                name=op.f("fk_conversation_data_source_id_data_sources"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation")),
        schema=S,
    )
    op.create_index("ix_conversation_source_updated", "conversation",
                    ["data_source_id", "updated_at"], unique=False, schema=S)
    op.create_table(
        "message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("query_run_id", sa.Uuid(), nullable=True),
        sa.Column("clarification_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_message_role")),
        sa.CheckConstraint(
            "kind IN ('question', 'clarification_answer', 'answer', 'clarification', "
            "'cannot_answer', 'failed')", name=op.f("ck_message_kind")),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat.conversation.id"],
                                name=op.f("fk_message_conversation_id_conversation"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["query_run_id"], ["chat.query_run.id"],
                                name=op.f("fk_message_query_run_id_query_run"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["clarification_id"], ["chat.clarification.id"],
                                name=op.f("fk_message_clarification_id_clarification"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message")),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("message", schema=S)
    op.drop_index("ix_conversation_source_updated", table_name="conversation", schema=S)
    op.drop_table("conversation", schema=S)
